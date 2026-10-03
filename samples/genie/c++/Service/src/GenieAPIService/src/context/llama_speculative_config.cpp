//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_speculative_config.h"

#include <filesystem>

#include "gguf.h"
#include "utils.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/sysinfo.h>
#endif

namespace llama_speculative
{

namespace
{

// 固定安全余量：llama-completion.exe 实测（CPU 路径，-ngl 0，ctx=8192）单独加载本模型主模型时，
// PeakPagedMemorySize64 为 17856913408 字节（约 16.627GiB），比"权重文件字节数(14.636GiB) +
// 本文件 KV 估算(2.031GiB) = 16.666GiB"仅低约 38MiB——不含安全余量的公式本身已与实测几乎重合，
// 2GiB 用于覆盖未直接实测到的部分：GPU/OpenCL 路径下额外的 host 侧暂存/计算缓冲区开销（该机器
// 当前驱动的 OpenCL 编译器拒绝大缓冲区扩展，-ngl 99 全量 GPU offload 本身会在加载阶段崩溃，
// 与 ctx/内存门槛无关，见 llama_cpp.cpp.notes.md，故无法在该机器上直接实测 GPU 路径峰值）。
constexpr uint64_t kSpeculativeMemoryEstimateMarginBytes = 2ULL * 1024 * 1024 * 1024;

struct GgufModelShape
{
    bool ok = false;
    uint32_t n_layer = 0;
    uint32_t n_head_kv = 0;
    uint32_t key_length = 0;
};

// 只读 GGUF 文件头的 KV 元数据（no_alloc=true，不创建 ggml_context、不加载任何张量数据），
// 代价仅为一次文件头解析。字段缺失/类型不匹配/文件无法解析时返回 ok=false。
GgufModelShape ReadGgufModelShape(const std::string &gguf_path)
{
    GgufModelShape shape;
    if (gguf_path.empty())
    {
        return shape;
    }

    gguf_init_params params{true, nullptr};
    gguf_context *ctx = gguf_init_from_file(gguf_path.c_str(), params);
    if (!ctx)
    {
        return shape;
    }

    int64_t arch_key = gguf_find_key(ctx, "general.architecture");
    if (arch_key < 0)
    {
        gguf_free(ctx);
        return shape;
    }
    std::string arch = gguf_get_val_str(ctx, arch_key);

    auto read_u32 = [&](const std::string &suffix) -> uint32_t
    {
        int64_t key_id = gguf_find_key(ctx, (arch + "." + suffix).c_str());
        return key_id >= 0 ? gguf_get_val_u32(ctx, key_id) : 0;
    };

    shape.n_layer = read_u32("block_count");
    shape.n_head_kv = read_u32("attention.head_count_kv");
    shape.key_length = read_u32("attention.key_length");
    shape.ok = shape.n_layer > 0 && shape.n_head_kv > 0 && shape.key_length > 0;

    gguf_free(ctx);
    return shape;
}

// K+V 两份、f16（2 字节/元素）：2 * n_layer * ctx * n_head_kv * key_length * 2。
// n_layer 直接取 GGUF 的 block_count，不识别混合架构（如 qwen35 的 full_attention_interval），
// 对本项目目前唯一的混合架构模型（Qwen3.8-27B）是明显高估（真实 KV 远小于此），但更简单、
// 更保守，调研阶段已确认该简化方案依然有效。
uint64_t EstimateKvCacheBytes(const GgufModelShape &shape, uint64_t context_size)
{
    if (!shape.ok)
    {
        return 0;
    }
    return 2ULL * shape.n_layer * context_size * shape.n_head_kv * shape.key_length * 2ULL;
}

} // namespace

DraftModelConfig DraftModelConfig::ParseFrom(const json &model_config_json)
{
    DraftModelConfig result;

    if (!model_config_json.is_object() || !model_config_json.contains("draft_model"))
    {
        return result;
    }

    const json &block = model_config_json.at("draft_model");
    if (!block.is_object())
    {
        return result;
    }

    result.present = true;

    if (block.contains("path") && block["path"].is_string())
    {
        result.path = block["path"].get<std::string>();
    }

    if (block.contains("spec_type") && block["spec_type"].is_string())
    {
        result.spec_type = block["spec_type"].get<std::string>();
    }

    if (block.contains("spec_draft_n_max") && block["spec_draft_n_max"].is_number())
    {
        result.spec_draft_n_max = block["spec_draft_n_max"].get<int>();
    }

    if (block.contains("spec_draft_p_min") && block["spec_draft_p_min"].is_number())
    {
        result.spec_draft_p_min = block["spec_draft_p_min"].get<double>();
    }

    // required 默认 true：drafter 加载失败则整个模型加载失败，不静默降级为纯 target 解码。
    if (block.contains("required") && block["required"].is_boolean())
    {
        result.required = block["required"].get<bool>();
    }

    return result;
}

uint64_t EstimateSpeculativeMemoryRequirement(const std::string &model_dir,
                                               const std::string &draft_relative_path,
                                               uint64_t context_size)
{
    std::vector<std::string> main_gguf_files;
    File::MatchFileInDir(model_dir, ".gguf", &main_gguf_files);
    std::string main_gguf_path = main_gguf_files.empty() ? "" : main_gguf_files.front();
    std::string draft_gguf_path = (std::filesystem::path(model_dir) / draft_relative_path).string();

    uint64_t total = 0;
    if (!main_gguf_path.empty())
    {
        total += File::get_file_size(main_gguf_path, std::ios::binary);
    }
    if (File::IsFileExist(draft_gguf_path))
    {
        total += File::get_file_size(draft_gguf_path, std::ios::binary);
    }

    total += EstimateKvCacheBytes(ReadGgufModelShape(main_gguf_path), context_size);
    total += EstimateKvCacheBytes(ReadGgufModelShape(draft_gguf_path), context_size);
    total += kSpeculativeMemoryEstimateMarginBytes;
    return total;
}

uint64_t GetAvailablePhysicalMemoryBytes()
{
#ifdef _WIN32
    MEMORYSTATUSEX statex;
    statex.dwLength = sizeof(statex);
    if (GlobalMemoryStatusEx(&statex))
    {
        return statex.ullAvailPhys;
    }
#else
    struct sysinfo info{};
    if (sysinfo(&info) == 0)
    {
        return static_cast<uint64_t>(info.freeram) * info.mem_unit;
    }
#endif
    return UINT64_MAX;
}

} // namespace llama_speculative
