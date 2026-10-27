//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_SPECULATIVE_CONFIG_H
#define LLAMA_SPECULATIVE_CONFIG_H

#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

#include "llama_runtime_planner.h"

using json = nlohmann::ordered_json;

// GGUF/llama.cpp 专属：投机解码（speculative decoding，Qwen3.8-27B + DFlash2）相关的 config.json
// 字段解析、硬件门槛判定与系统内存查询。原先误放在通用目录 src/model/（DraftModelConfig）与
// MNNContext（GetTotalPhysicalMemoryBytes），但这些概念只对 llama.cpp fork 的投机解码 API 有
// 意义，且只被 LLAMACppBuilder（llama_cpp.cpp）与 GGUFVerify（model_manager.cpp）使用，QNN/MNN
// 后端完全不涉及，故整体归入本目录（与 llama_cpp.h/.cpp 同级），用命名空间显式标注其专属归属。
namespace llama_speculative
{

// GGUF 模型 config.json 中可选的 draft_model 字段块：声明该模型自带投机解码（speculative
// decoding）drafter 模型。只有该字段块存在时才触发投机解码路径（GGUFVerify/LLAMACppBuilder），
// 不含该字段的模型（现有全部 GGUF 模型）完全不受影响。
//
// 字段解析被设计为不依赖 Windows API/真实硬件查询的纯函数（ParseFrom 只接受一个已解析好的
// json 对象），便于脱离 MSVC/远程 ARM64 环境、用任意本地 C++ 编译器独立单元验证。
struct DraftModelConfig
{
    bool present = false;              // config.json 是否声明了 draft_model 字段块
    std::string path;                  // drafter 权重文件路径（相对模型目录，如 "Qwen3.8-27B-DFlash2-Q4_K_M.gguf"）
    std::string spec_type;             // 投机解码类型，如 "draft-dflash"
    int spec_draft_n_max = 0;          // 每轮投机验证最多产出的 draft token 数（对应 --spec-draft-n-max）
    double spec_draft_p_min = 0.0;     // 投机接受概率下限（对应 --spec-draft-p-min）
    bool required = true;              // drafter 加载失败时是否导致整体模型加载失败（默认 true，不静默降级）
    std::string flash_attn;            // 主模型 -fa："on"/"off"；空 = 不拼 -fa，由 llama.cpp 自己 AUTO 探测
    std::string draft_flash_attn;      // 草稿模型专属 FA："on"/"off"；空/其余值 = 不覆盖，跟随 AUTO
    std::string samplers;              // 对应 --samplers；空 = 未配置，不拼进 argv
    float temp = 0.0f;                 // 对应 --temp；默认 0（greedy）；<0 = 不拼进 argv
    int top_k = 1;                     // 对应 --top-k；默认 1（greedy）；<0 = 不拼进 argv
    float top_p = -1.0f;               // 对应 --top-p；<0 = 未配置，不拼进 argv
    int ngram_mod_n_min = -1;          // 对应 --spec-ngram-mod-n-min；<0 = 未配置，不拼进 argv
    int ngram_mod_n_match = 16;        // 对应 --spec-ngram-mod-n-match；<0 = 不拼进 argv
    bool ngram_mod = true;             // true = 自动给 spec_type 追加 ",ngram-mod"（已含时不重复追加）
    int n_gpu_layers = 99;             // 对应 -ngld；0 = drafter 权重全部留在 CPU；<0 = 不拼进 argv
    int fit_target_mib = -1;           // 对应 -fitt（每设备显存预留）；<0 = 未配置，不拼进 argv
    int n_threads = -1;                // 对应 -t（decode 线程数）；<=0 = 未配置，保持 hardware_concurrency()
    int n_threads_batch = -1;          // 对应 -tb（prefill 线程数）；<=0 = 未配置，跟随 n_threads 的最终值
    int ffn_cpu_layers = -1;           // 内置 -ot 下放到 CPU 的后段 FFN 层数；<0 = 用 kDflashFfnCpuLayersDefault
    int max_alloc_mib = -1;            // 设备单次分配上限覆盖值；<0 = 先问设备，问不到再退回 2048 MiB
    int n_ubatch = -1;                 // 对应 -ub；<=0 = 由规划器推导（默认 512）。显式值会进规划器，FA 判据按它重算 kq
    int n_batch = -1;                  // 对应 -b；<=0 = 由规划器推导（默认 2048）
    std::string planner;               // "off" = 回退到旧的 SpeculativeFitsFullGpu 门槛；其余值沉默忽略（默认走规划器）

    // 从模型 config.json 解析出的 json 对象中提取 draft_model 字段块。
    // 不含该字段块、字段类型不匹配、或传入的 json 本身为空/非 object 时，均返回 present=false
    // 的默认实例，不抛异常——与 GGUFVerify 现有对 "backend" 字段的宽松解析风格一致。
    static DraftModelConfig ParseFrom(const json &model_config_json);
};

// 投机解码模型（Qwen3.8-27B + DFlash2）加载所需内存的动态估算。
// ⚠️ 这是 **`draft_model.planner: "off"` 的回退路径**，默认路径已改走
// `PlanSpeculativeRuntime()`（单一真相源）。它的口径是：主/草稿模型权重文件字节数之和
// + 按 context_size 估算的 attention KV（经 `EstimateAttentionKvCacheBytes()`，**该函数确实
// 识别 `full_attention_interval`**）+ 固定 2 GiB 余量。它漏了 recurrent 状态与 compute buffer。
// model_dir 为模型所在目录，draft_relative_path 为 draft_model.path 字段原值（相对 model_dir）。
// 比较对象为可用物理内存（GetAvailablePhysicalMemoryBytes()）而非总物理内存，见
// GGUFVerify::CreateIfVerifiedImpl()（model_manager.cpp）。
uint64_t EstimateSpeculativeMemoryRequirement(const std::string &model_dir,
                                               const std::string &draft_relative_path,
                                               uint64_t context_size);

// 可用物理内存字节数（ullAvailPhys，与 MNNContext::GetAvailablePhysicalMemoryBytes 同一
// GlobalMemoryStatusEx 模式）。用于投机解码（draft_model）模型加载前的动态内存门槛检查。
uint64_t GetAvailablePhysicalMemoryBytes();

// 总物理内存字节数（ullTotalPhys），与上面的 GetAvailablePhysicalMemoryBytes 同一
// GlobalMemoryStatusEx 调用模式。仅用于 ResolveContextSizeByPhysicalMemory()。
uint64_t GetTotalPhysicalMemoryBytes();

// Qwen3.8-27B + DFlash2 专属：上下文长度暂不支持通过 config.json/service_config.json 配置，
// 固定按整机物理内存总量在两档之间选择——标称 32G 档用 16K（这是当前唯一有完整长任务实测
// 背书的档位），标称 32G 以上（如 48G 机）用 32K。必须被 GGUFVerify（model_manager.cpp）与
// LLAMACppBuilder（llama_cpp.cpp）同时复用同一个值，否则门槛检查依据的 ctx 会与真实加载时
// 的 n_ctx 不一致。
uint64_t ResolveContextSizeByPhysicalMemory();

// 主模型 + 草稿权重 + attention 层 KV + 计算余量是否装得进 GPU 设备报告的可用显存。
// ⚠️ 同上：这是 `draft_model.planner: "off"` 的回退路径，默认路径走 PlanSpeculativeRuntime()。
bool SpeculativeFitsFullGpu(const std::string &model_dir, const std::string &draft_relative_path,
                            uint64_t context_size, uint64_t *required_out, uint64_t *available_out);

// 单一真相源：把「磁盘上的 GGUF 形状 + 设备显存/主机内存/单次分配上限 + 显式配置」汇成
// 一次 PlanRuntime() 调用。GGUFVerify 的加载门槛与 LLAMACppBuilder 的 argv 拼装必须消费
// **同一份** RuntimePlan，不得各算一遍（旧代码两处口径已经漂移，这正是本模块存在的理由）。
// 内部做两件有副作用的事：读 GGUF 文件头与文件大小；调 ggml_backend_load_all() +
// ggml_backend_dev_memory() + QueryDeviceMaxAllocBytes() 各一次。其余全是纯函数。
llama_runtime::RuntimePlan PlanSpeculativeRuntime(const std::string &model_dir,
                                                  const DraftModelConfig &cfg,
                                                  uint64_t context_size);

// 主模型 GGUF 的 block_count；读不到返回 0。
uint32_t ReadGgufBlockCount(const std::string &gguf_path);

// 内置 -ot 下放到 CPU 的后段 FFN 层数的默认值：本模型（65/66 层）长期使用、唯一有完整长任务实测
// 背书的值，等价于历史的 blk 35-64。不要凭显存估算去调小它，原因见同目录 llama_cpp.cpp.notes.md
// 「-ot 层数按显存反算（B1）已判死」一节。
constexpr uint32_t kDflashFfnCpuLayersDefault = 30;

// DFlash 投机模型内置的 -ot 规则：最后 cpu_layers 层 FFN 权重留 CPU，attention 上 GPU。
// block_count 为 0、cpu_layers 为 0 或超过 block_count 时返回空串。
std::string BuildDflashFfnCpuOverride(uint32_t block_count, uint32_t cpu_layers);

} // namespace llama_speculative

#endif //LLAMA_SPECULATIVE_CONFIG_H
