//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_speculative_config.h"

#include <filesystem>

#ifdef USE_GGUF
#include "gguf.h"
#include "ggml-backend.h"
#endif
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

constexpr uint64_t kSpeculativeMemoryEstimateMarginBytes = 2ULL * 1024 * 1024 * 1024;

#ifdef USE_GGUF
struct GgufModelShape
{
    bool ok = false;
    uint32_t n_layer = 0;
    uint32_t n_head_kv = 0;
    uint32_t key_length = 0;
    uint32_t full_attention_interval = 0;
};

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
    shape.full_attention_interval = read_u32("full_attention_interval");
    shape.ok = shape.n_layer > 0 && shape.n_head_kv > 0 && shape.key_length > 0;

    gguf_free(ctx);
    return shape;
}

uint64_t EstimateKvCacheBytes(const GgufModelShape &shape, uint64_t context_size)
{
    if (!shape.ok)
    {
        return 0;
    }
    return 2ULL * shape.n_layer * context_size * shape.n_head_kv * shape.key_length * 2ULL;
}

uint64_t EstimateAttentionKvCacheBytes(const GgufModelShape &shape, uint64_t context_size)
{
    const uint64_t full = EstimateKvCacheBytes(shape, context_size);
    return shape.full_attention_interval > 1 ? full / shape.full_attention_interval : full;
}
#endif

constexpr uint64_t kFullGpuComputeReserveBytes = 1024ULL * 1024 * 1024;

}

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

    if (block.contains("flash_attn") && block["flash_attn"].is_string())
    {
        result.flash_attn = block["flash_attn"].get<std::string>();
    }

    if (block.contains("draft_flash_attn") && block["draft_flash_attn"].is_string())
    {
        result.draft_flash_attn = block["draft_flash_attn"].get<std::string>();
    }

    if (block.contains("samplers") && block["samplers"].is_string())
    {
        result.samplers = block["samplers"].get<std::string>();
    }

    if (block.contains("temp") && block["temp"].is_number())
    {
        result.temp = block["temp"].get<float>();
    }

    if (block.contains("top_k") && block["top_k"].is_number_integer())
    {
        result.top_k = block["top_k"].get<int>();
    }

    if (block.contains("top_p") && block["top_p"].is_number())
    {
        result.top_p = block["top_p"].get<float>();
    }

    if (block.contains("ngram_mod_n_min") && block["ngram_mod_n_min"].is_number_integer())
    {
        result.ngram_mod_n_min = block["ngram_mod_n_min"].get<int>();
    }

    if (block.contains("ngram_mod_n_match") && block["ngram_mod_n_match"].is_number_integer())
    {
        result.ngram_mod_n_match = block["ngram_mod_n_match"].get<int>();
    }

    if (block.contains("ngram_mod") && block["ngram_mod"].is_boolean())
    {
        result.ngram_mod = block["ngram_mod"].get<bool>();
    }

    if (block.contains("n_gpu_layers") && block["n_gpu_layers"].is_number_integer())
    {
        result.n_gpu_layers = block["n_gpu_layers"].get<int>();
    }

    if (block.contains("fit_target_mib") && block["fit_target_mib"].is_number_integer())
    {
        result.fit_target_mib = block["fit_target_mib"].get<int>();
    }

    if (block.contains("n_threads") && block["n_threads"].is_number_integer())
    {
        result.n_threads = block["n_threads"].get<int>();
    }

    if (block.contains("n_threads_batch") && block["n_threads_batch"].is_number_integer())
    {
        result.n_threads_batch = block["n_threads_batch"].get<int>();
    }

    if (block.contains("ffn_cpu_layers") && block["ffn_cpu_layers"].is_number_integer())
    {
        result.ffn_cpu_layers = block["ffn_cpu_layers"].get<int>();
    }

    if (block.contains("max_alloc_mib") && block["max_alloc_mib"].is_number_integer())
    {
        result.max_alloc_mib = block["max_alloc_mib"].get<int>();
    }

    if (block.contains("n_ubatch") && block["n_ubatch"].is_number_integer())
    {
        result.n_ubatch = block["n_ubatch"].get<int>();
    }

    if (block.contains("n_batch") && block["n_batch"].is_number_integer())
    {
        result.n_batch = block["n_batch"].get<int>();
    }

    if (block.contains("planner") && block["planner"].is_string())
    {
        result.planner = block["planner"].get<std::string>();
    }

    if (block.contains("required") && block["required"].is_boolean())
    {
        result.required = block["required"].get<bool>();
    }

    return result;
}

std::string FindMainGguf(const std::string &model_dir, const std::string &draft_gguf_path)
{
    std::vector<std::string> gguf_files;
    File::MatchFileInDir(model_dir, ".gguf", &gguf_files);
    std::error_code ec;
    std::string main_gguf_path;
    uint64_t main_size = 0;
    for (const auto &f: gguf_files)
    {
        if (std::filesystem::equivalent(f, draft_gguf_path, ec))
        {
            continue;
        }
        const uint64_t size = File::get_file_size(f, std::ios::binary);
        if (size > main_size)
        {
            main_size = size;
            main_gguf_path = f;
        }
    }
    return main_gguf_path;
}

uint64_t EstimateSpeculativeMemoryRequirement(const std::string &model_dir,
                                               const std::string &draft_relative_path,
                                               uint64_t context_size)
{
    std::string draft_gguf_path = (std::filesystem::path(model_dir) / draft_relative_path).string();
    std::string main_gguf_path = FindMainGguf(model_dir, draft_gguf_path);

    uint64_t total = 0;
    if (!main_gguf_path.empty())
    {
        total += File::get_file_size(main_gguf_path, std::ios::binary);
    }
    if (File::IsFileExist(draft_gguf_path))
    {
        total += File::get_file_size(draft_gguf_path, std::ios::binary);
    }

#ifdef USE_GGUF
    total += EstimateAttentionKvCacheBytes(ReadGgufModelShape(main_gguf_path), context_size);
    total += EstimateAttentionKvCacheBytes(ReadGgufModelShape(draft_gguf_path), context_size);
#endif
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

uint64_t GetTotalPhysicalMemoryBytes()
{
#ifdef _WIN32
    MEMORYSTATUSEX statex;
    statex.dwLength = sizeof(statex);
    if (GlobalMemoryStatusEx(&statex))
    {
        return statex.ullTotalPhys;
    }
#else
    struct sysinfo info{};
    if (sysinfo(&info) == 0)
    {
        return static_cast<uint64_t>(info.totalram) * info.mem_unit;
    }
#endif
    return UINT64_MAX;
}

uint64_t ResolveContextSizeByPhysicalMemory()
{

    constexpr uint64_t kTierThresholdBytes = 32ULL * 1024 * 1024 * 1024;
    constexpr uint64_t kContextSize16K = 16384ULL;
    constexpr uint64_t kContextSize32K = 32768ULL;
    const uint64_t total = GetTotalPhysicalMemoryBytes();
    return total > kTierThresholdBytes ? kContextSize32K : kContextSize16K;
}

bool SpeculativeFitsFullGpu(const std::string &model_dir, const std::string &draft_relative_path,
                            uint64_t context_size, uint64_t *required_out, uint64_t *available_out)
{
#ifdef USE_GGUF
    const std::string draft_gguf_path = (std::filesystem::path(model_dir) / draft_relative_path).string();
    const std::string main_gguf_path = FindMainGguf(model_dir, draft_gguf_path);
    if (main_gguf_path.empty())
    {
        return false;
    }

    ggml_backend_load_all();
    ggml_backend_dev_t gpu = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_GPU);
    if (!gpu)
    {
        return false;
    }
    size_t free = 0;
    size_t total = 0;
    ggml_backend_dev_memory(gpu, &free, &total);

    uint64_t required = File::get_file_size(main_gguf_path, std::ios::binary);
    if (File::IsFileExist(draft_gguf_path))
    {
        required += File::get_file_size(draft_gguf_path, std::ios::binary);
    }
    required += EstimateAttentionKvCacheBytes(ReadGgufModelShape(main_gguf_path), context_size);
    required += EstimateAttentionKvCacheBytes(ReadGgufModelShape(draft_gguf_path), context_size);
    required += kFullGpuComputeReserveBytes;

    if (required_out) *required_out = required;
    if (available_out) *available_out = free;
    return free > 0 && required <= free;
#else
    (void) model_dir;
    (void) draft_relative_path;
    (void) context_size;
    (void) required_out;
    (void) available_out;
    return false;
#endif
}

uint32_t ReadGgufBlockCount(const std::string &gguf_path)
{
#ifdef USE_GGUF
    return ReadGgufModelShape(gguf_path).n_layer;
#else
    (void) gguf_path;
    return 0;
#endif
}

std::string BuildDflashFfnCpuOverride(uint32_t block_count, uint32_t cpu_layers)
{
    if (block_count == 0 || cpu_layers == 0 || cpu_layers > block_count)
    {
        return {};
    }
    std::string layers;
    for (uint32_t i = block_count - cpu_layers; i < block_count; ++i)
    {
        layers += (layers.empty() ? "" : "|") + std::to_string(i);
    }
    return "blk\\.(" + layers + ")\\.ffn_(up|gate|down)\\.weight=CPU";
}

llama_runtime::RuntimePlan PlanSpeculativeRuntime(const std::string &model_dir,
                                                  const DraftModelConfig &cfg,
                                                  uint64_t context_size)
{
    llama_runtime::PlanInputs in;
    in.ctx_requested = static_cast<uint32_t>(context_size);
    in.n_seq_max = 1;

    in.enable_qwen38_dflash_policy = cfg.spec_type.find("dflash") != std::string::npos;
    if (cfg.flash_attn == "on") in.explicit_fa_target = 1;
    else if (cfg.flash_attn == "off") in.explicit_fa_target = 0;
    if (cfg.draft_flash_attn == "on") in.explicit_fa_draft = 1;
    else if (cfg.draft_flash_attn == "off") in.explicit_fa_draft = 0;
    if (cfg.spec_draft_n_max > 0) in.explicit_n_max = cfg.spec_draft_n_max;
    in.explicit_ffn_cpu_layers = cfg.ffn_cpu_layers;
    if (cfg.n_ubatch > 0) in.explicit_n_ubatch = cfg.n_ubatch;
    if (cfg.n_batch > 0) in.explicit_n_batch = cfg.n_batch;
    in.max_alloc_explicit_mib = cfg.max_alloc_mib;

    const std::string draft_gguf_path = (std::filesystem::path(model_dir) / cfg.path).string();
    const std::string main_gguf_path = FindMainGguf(model_dir, draft_gguf_path);

#ifdef USE_GGUF
    in.tgt = llama_runtime::ReadGgufModelShape(main_gguf_path);
    in.dft = llama_runtime::ReadGgufModelShape(draft_gguf_path);
#endif
    if (!main_gguf_path.empty())
    {
        in.tgt.weight_bytes = File::get_file_size(main_gguf_path, std::ios::binary);
    }
    if (File::IsFileExist(draft_gguf_path))
    {
        in.dft.weight_bytes = File::get_file_size(draft_gguf_path, std::ios::binary);
    }

    in.host_avail = GetAvailablePhysicalMemoryBytes();
#ifdef USE_GGUF
    ggml_backend_load_all();
    ggml_backend_dev_t gpu = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_GPU);
    if (gpu)
    {
        size_t free = 0;
        size_t total = 0;
        ggml_backend_dev_memory(gpu, &free, &total);
        in.gpu_free = free;
    }

    in.max_alloc_device = llama_runtime::QueryDeviceMaxAllocBytes();
#endif

    return llama_runtime::PlanRuntime(in);
}

}
