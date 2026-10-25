//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_runtime_planner.h"

#include <algorithm>
#include <sstream>

#ifdef USE_GGUF
#include "gguf.h"
#endif

#ifdef _WIN32
// windows.h 会定义 min/max 宏，把 std::min/std::max 吃掉（实测报 C2589 "'(': illegal token on
// right side of '::'"）。本文件只用 windows.h 的 LoadLibraryA/GetProcAddress。
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace llama_runtime
{

namespace
{

// KV cache 元素字节数：恒 f16。-ctk/-ctv 量化在任何机型上都不碰（r19 实测 q8_0 在 CPU
// get_rows_q 触发 GGML_ASSERT(i01 >= 0 && i01 < ne01) 崩溃），所以这里不做成参数。
constexpr uint64_t kKvTypeSizeBytes = 2;

// recurrent 状态的 r/s 两个张量按 f32 算。⚠️ 源码里它们的类型来自构造参数 type_r/type_s
// （`llama-memory-recurrent.cpp:102-103`），**并没有任何一行源码证明本服务这条路径上恒为 f32**；
// 它是从实测反推的：按 f32 代入得 1,255,145,472 B，与启动日志的 1197 MiB 逐字吻合（按 f16
// 会差一倍）。同理 n_seq_max 当 mem_size 也是实测反推、非源码逐字确认。
constexpr uint64_t kRecurrentTypeSizeBytes = 4;

} // namespace

uint32_t ModelShape::n_layer() const
{
    // llama-hparams.cpp:348  `return n_layer_all - n_layer_nextn;`
    return n_layer_all > n_layer_nextn ? n_layer_all - n_layer_nextn : 0;
}

uint64_t ModelShape::n_embd_r() const
{
    // 分支顺序逐字对应 llama_hparams::n_embd_r()（llama-hparams.cpp:208-234）。
    if (wkv_head_size != 0)
    {
        return (uint64_t) token_shift_count * n_embd;              // RWKV
    }
    if (n_shortconv_l_cache != 0)
    {
        return n_shortconv_l_cache > 1 ? (uint64_t) n_embd * (n_shortconv_l_cache - 1) : 0; // LFM2
    }
    if (n_embd_head_kda != 0)
    {
        const uint64_t d_inner = (uint64_t) n_head * n_embd_head_kda;                       // Kimi KDA
        return 3ULL * (ssm_d_conv > 0 ? ssm_d_conv - 1 : 3) * d_inner;
    }
    // Mamba 的 conv_states：(d_conv - 1) * (d_inner + 2 * n_group * d_state)
    return (uint64_t) (ssm_d_conv > 0 ? ssm_d_conv - 1 : 0) *
           ((uint64_t) ssm_d_inner + 2ULL * ssm_n_group * ssm_d_state);
}

uint64_t ModelShape::n_embd_s() const
{
    // 分支顺序逐字对应 llama_hparams::n_embd_s()（llama-hparams.cpp:236-258）。
    if (wkv_head_size != 0)
    {
        return (uint64_t) n_embd * wkv_head_size;                                           // RWKV
    }
    if (n_embd_head_kda != 0)
    {
        return (uint64_t) n_embd_head_kda * n_embd_head_kda * n_head;                        // Kimi KDA
    }
    if (n_embd_head_la != 0)
    {
        return (uint64_t) n_embd_head_la * n_embd_head_la * n_head;                           // MiniMax
    }
    // Mamba 的 ssm_states
    return (uint64_t) ssm_d_state * ssm_d_inner;
}

uint64_t MemoryLedger::gpu_required() const
{
    return weights_tgt + weights_dft + kv_attn_tgt + kv_attn_dft + recurrent_tgt + recurrent_dft +
           compute_const + compute_kq + runtime_peak + safety;
}

uint64_t MemoryLedger::gpu_required_min() const
{
    return weights_tgt + weights_dft + kv_attn_tgt + kv_attn_dft + recurrent_tgt + recurrent_dft +
           compute_const + runtime_peak;
}

bool FlashAttnEnabled(FlashAttnChoice c)
{
    return c == FlashAttnChoice::kDerivedOn || c == FlashAttnChoice::kExplicitOn;
}

bool FlashAttnExplicit(FlashAttnChoice c)
{
    return c == FlashAttnChoice::kExplicitOn || c == FlashAttnChoice::kExplicitOff;
}

uint64_t AttentionKvCacheBytes(const ModelShape &shape, uint64_t n_ctx)
{
    if (!shape.ok || shape.n_layer_attn == 0 || shape.n_head_kv == 0 || shape.key_length == 0)
    {
        return 0;
    }
    // K 和 V 各一份。按完整 n_ctx 分配：n_swa 不缩小它（实测 16 层 / 32K / f16 恰为 2048 MiB，
    // 与启动日志逐字吻合，而该模型 n_swa=2048）。
    return 2ULL * shape.n_layer_attn * n_ctx * shape.n_head_kv * shape.key_length * kKvTypeSizeBytes;
}

uint64_t RecurrentStateBytesPerCopy(const ModelShape &shape)
{
    if (!shape.ok || shape.n_layer_recr == 0)
    {
        return 0;
    }
    const uint64_t per_layer = (shape.n_embd_r() + shape.n_embd_s()) * kRecurrentTypeSizeBytes;
    return per_layer * shape.n_layer_recr;
}

uint64_t RecurrentStateBytes(const ModelShape &shape, uint32_t n_seq_max, uint32_t n_rs_seq)
{
    const uint64_t per_copy = RecurrentStateBytesPerCopy(shape);
    if (per_copy == 0 || n_seq_max == 0)
    {
        return 0;
    }
    // llama-memory-recurrent.cpp:101  `const uint32_t n_rows = mem_size * (1 + n_rs_seq);`
    // mem_size 对 recurrent memory 就是 n_seq_max；n_rs_seq 来自 common.h:399-405
    // `return needs_rs_seq ? draft.n_max : 0u;`（dflash 在白名单内）。
    return per_copy * n_seq_max * (1ULL + n_rs_seq);
}

uint64_t ComputeKqBytes(const ModelShape &shape, uint64_t n_ctx, uint32_t n_ubatch)
{
    if (!shape.ok || shape.n_head == 0 || shape.n_layer_attn == 0 || n_ubatch == 0)
    {
        return 0;
    }
    // llama-graph.cpp:2810-2816  `ggml_tensor * kq = ggml_mul_mat(ctx0, k, q);`（非 flash 分支；
    // flash 分支在 2783-2784 调 ggml_flash_attn_ext）。⚠️ 源码那里只有
    // `ggml_prec_set_acc(kq, GGML_PREC_F32)`（设的是**累加精度**），**没有一行写明
    // `kq->type == F32`**；这里按 4 B/元素算是实测佐证的：48K 关 FA 实测 CPU compute buffer
    // 2616 MiB，与 f32 的 2304 MiB 同量级，而 f16 只有 1152 MiB、解释不了。
    // reserve 的最坏情况图取 n_tokens = min(n_ctx, n_ubatch)（llama-context.cpp:937-942 逐字）。
    // 这是**单个张量**的字节数，判据比的就是它与设备单次分配上限。
    const uint64_t n_tokens = std::min<uint64_t>(n_ctx, n_ubatch);
    return n_ctx * n_tokens * shape.n_head * 4ULL;
}

uint64_t CriticalContextForFlashAttn(const ModelShape &shape, uint32_t n_ubatch, uint64_t max_alloc)
{
    if (!shape.ok || shape.n_head == 0 || shape.n_layer_attn == 0 || n_ubatch == 0)
    {
        return 0;
    }
    // n_ctx > n_ubatch 的区间里 kq 对 n_ctx 是线性的：kq = n_ctx × n_ubatch × n_head × 4。
    const uint64_t per_ctx = (uint64_t) n_ubatch * shape.n_head * 4ULL;
    return per_ctx == 0 ? 0 : max_alloc / per_ctx;
}

#ifdef USE_GGUF
namespace
{
// 定义在下方（紧跟 ReadGgufModelShape），这里只前置声明以便在解析流程里调用。
void ReadTensorBreakdown(gguf_context *ctx, ModelShape &shape);
} // namespace
#endif

ModelShape ReadGgufModelShape(const std::string &gguf_path)
{
    ModelShape shape;
#ifdef USE_GGUF
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

    const int64_t arch_key = gguf_find_key(ctx, "general.architecture");
    if (arch_key < 0)
    {
        gguf_free(ctx);
        return shape;
    }
    const std::string arch = gguf_get_val_str(ctx, arch_key);

    auto read_u32 = [&](const std::string &suffix) -> uint32_t
    {
        const int64_t key_id = gguf_find_key(ctx, (arch + "." + suffix).c_str());
        return key_id >= 0 ? gguf_get_val_u32(ctx, key_id) : 0;
    };

    shape.n_layer_all = read_u32("block_count");
    shape.n_layer_nextn = read_u32("nextn_predict_layers");
    shape.n_head = read_u32("attention.head_count");
    shape.n_head_kv = read_u32("attention.head_count_kv");
    shape.key_length = read_u32("attention.key_length");
    shape.n_embd = read_u32("embedding_length");
    shape.n_swa = read_u32("attention.sliding_window");
    shape.full_attention_interval = read_u32("full_attention_interval");

    shape.ssm_d_conv = read_u32("ssm.conv_kernel");
    shape.ssm_d_inner = read_u32("ssm.inner_size");
    shape.ssm_d_state = read_u32("ssm.state_size");
    shape.ssm_n_group = read_u32("ssm.group_count");

    shape.wkv_head_size = read_u32("wkv.head_size");
    const uint32_t token_shift = read_u32("token_shift_count");
    if (token_shift > 0)
    {
        shape.token_shift_count = token_shift;
    }
    shape.n_shortconv_l_cache = read_u32("shortconv.l_cache");
    shape.n_embd_head_kda = read_u32("attention.key_length_kda");
    shape.n_embd_head_la = read_u32("attention.key_length_la");

    ReadTensorBreakdown(ctx, shape);

    gguf_free(ctx);

    // key_length 缺失时退回 n_embd / n_head（与 llama.cpp 的 n_embd_head_k 默认一致）。
    if (shape.key_length == 0 && shape.n_head > 0 && shape.n_embd > 0)
    {
        shape.key_length = shape.n_embd / shape.n_head;
    }

    shape.ok = shape.n_layer_all > 0 && shape.n_head > 0 && shape.n_head_kv > 0 &&
               shape.key_length > 0;
    if (shape.ok)
    {
        DeriveLayerSplit(shape);
    }
#else
    (void) gguf_path;
#endif
    return shape;
}

#ifdef USE_GGUF
namespace
{

// 按张量粒度统计两类「不在 GPU 上」的权重：
//   - token_embd.weight：llama.cpp 默认把输入层 embedding 留在 CPU，即使 -ngl 99 全 GPU
//     也是如此（日志里那 682 MiB 的 `CPU model buffer` 就是它）。
//   - blk.N.ffn_(up|gate|down).weight：拆分加载时被内置 -ot 挂到 CPU（`CPU_KLEIDIAI`）。
// 为什么不按 n_embd × n_ff 自己算：每个张量的量化类型可能不同（q4_0/q6_K 混用），只有
// gguf_get_tensor_size() 给的是真值。
void ReadTensorBreakdown(gguf_context *ctx, ModelShape &shape)
{
    shape.ffn_bytes_per_layer.assign(shape.n_layer_all, 0);
    const int64_t n_tensors = gguf_get_n_tensors(ctx);
    for (int64_t i = 0; i < n_tensors; ++i)
    {
        const char *name = gguf_get_tensor_name(ctx, i);
        if (!name)
        {
            continue;
        }
        const size_t bytes = gguf_get_tensor_size(ctx, i);
        const std::string tensor_name = name;
        if (tensor_name.rfind("token_embd", 0) == 0)
        {
            shape.embedding_bytes += bytes;
            continue;
        }
        if (tensor_name.rfind("blk.", 0) != 0 || tensor_name.find(".ffn_") == std::string::npos)
        {
            continue;
        }
        if (tensor_name.find(".ffn_up") == std::string::npos &&
            tensor_name.find(".ffn_gate") == std::string::npos &&
            tensor_name.find(".ffn_down") == std::string::npos)
        {
            continue;
        }
        // 只认 .weight：内置 -ot 的正则也只覆盖 ffn_(up|gate|down).weight。
        if (tensor_name.size() < 7 || tensor_name.compare(tensor_name.size() - 7, 7, ".weight") != 0)
        {
            continue;
        }
        const size_t dot = tensor_name.find('.', 4);
        if (dot == std::string::npos)
        {
            continue;
        }
        const std::string idx = tensor_name.substr(4, dot - 4);
        char *end = nullptr;
        const unsigned long layer = std::strtoul(idx.c_str(), &end, 10);
        if (end == idx.c_str() || layer >= shape.ffn_bytes_per_layer.size())
        {
            continue;
        }
        shape.ffn_bytes_per_layer[layer] += bytes;
    }
}

} // namespace
#endif

void DeriveLayerSplit(ModelShape &shape)
{
    const uint32_t n_eff = shape.n_layer();
    const bool has_recurrent = (shape.n_embd_r() + shape.n_embd_s()) > 0;

    if (!has_recurrent)
    {
        // 纯 transformer：全部层都带 attention。
        shape.n_layer_attn = n_eff;
        shape.n_layer_recr = 0;
        return;
    }

    if (shape.full_attention_interval > 1)
    {
        // llama-hparams.cpp:24-37 set_recr_pattern() 的两种 pattern（`il % n_pattern != 0` 与
        // `il % n_pattern < n_pattern - 1`）在 n_eff 能被 interval 整除时**数量上**等价：每
        // interval 层恰有 1 层是 attention。⚠️ 两者的 attention 层**位置不同**（段首 vs 段末），
        // 本账本只靠层数、不靠位置，所以这条成立；但不要拿它当成「逐层等价」引用。
        // 本模型 n_eff=64、interval=4 ⇒ attention 16 层、recurrent 48 层，与启动日志的
        // KV 2048 MiB / RS 1197 MiB 两项同时对账成立。
        shape.n_layer_attn = n_eff / shape.full_attention_interval;
        shape.n_layer_recr = n_eff - shape.n_layer_attn;
    }
    else
    {
        // 纯 recurrent（Mamba 等）：没有 full attention 层。
        shape.n_layer_attn = 0;
        shape.n_layer_recr = n_eff;
    }
}

// ---------------------------------------------------------------------------
// 设备单次分配上限
// ---------------------------------------------------------------------------
//
// 为什么自己动态加载 OpenCL.dll，而不调 ggml 的接口：
//   - `ggml_backend_buft_get_max_size()` 对 OpenCL 返回 `SIZE_MAX` —— 该后端没有实现
//     `get_max_size` 回调，ggml-backend.cpp 的默认实现就是返回 SIZE_MAX。真值只存在于
//     后端内部的 `backend_ctx->max_alloc_size`（由 clGetDeviceInfo 填充），没有导出。
//   - 本仓库不直接依赖 OpenCL 头（llama.cpp 的 OpenCL 后端是构建期拉取的子项目），所以
//     这里自带最小声明 + LoadLibrary，零构建依赖。失败一律返回 0，由调用方退回常量。
uint64_t QueryDeviceMaxAllocBytes()
{
#if defined(_WIN32) && defined(USE_GGUF)
    using cl_int = int32_t;
    using cl_uint = uint32_t;
    using cl_ulong = uint64_t;
    using cl_device_type = uint64_t;
    using cl_platform_id = void *;
    using cl_device_id = void *;

    constexpr cl_device_type kClDeviceTypeGpu = 1 << 2;
    constexpr cl_uint kClDeviceMaxMemAllocSize = 0x1010;

    using FnGetPlatformIDs = cl_int (*)(cl_uint, cl_platform_id *, cl_uint *);
    using FnGetDeviceIDs = cl_int (*)(cl_platform_id, cl_device_type, cl_uint, cl_device_id *, cl_uint *);
    using FnGetDeviceInfo = cl_int (*)(cl_device_id, cl_uint, size_t, void *, size_t *);

    HMODULE lib = LoadLibraryA("OpenCL.dll");
    if (!lib)
    {
        return 0;
    }

    auto get_platforms = (FnGetPlatformIDs) GetProcAddress(lib, "clGetPlatformIDs");
    auto get_devices = (FnGetDeviceIDs) GetProcAddress(lib, "clGetDeviceIDs");
    auto get_device_info = (FnGetDeviceInfo) GetProcAddress(lib, "clGetDeviceInfo");
    if (!get_platforms || !get_devices || !get_device_info)
    {
        FreeLibrary(lib);
        return 0;
    }

    cl_platform_id platforms[8] = {};
    cl_uint n_platforms = 0;
    uint64_t best = 0;
    if (get_platforms(8, platforms, &n_platforms) == 0)
    {
        for (cl_uint p = 0; p < n_platforms; ++p)
        {
            cl_device_id devices[8] = {};
            cl_uint n_devices = 0;
            if (get_devices(platforms[p], kClDeviceTypeGpu, 8, devices, &n_devices) != 0)
            {
                continue;
            }
            for (cl_uint d = 0; d < n_devices; ++d)
            {
                cl_ulong value = 0;
                if (get_device_info(devices[d], kClDeviceMaxMemAllocSize, sizeof(value), &value,
                                    nullptr) == 0)
                {
                    best = std::max<uint64_t>(best, value);
                }
            }
        }
    }

    // 不卸载 OpenCL.dll：llama.cpp 的 OpenCL 后端随后还会用它，FreeLibrary 只减引用计数，
    // 但本进程里没有必要反复加载/卸载，留着更安全（与 ggml 自己的加载顺序无耦合）。
    FreeLibrary(lib);
    return best;
#else
    return 0;
#endif
}

// ---------------------------------------------------------------------------
// 规划
// ---------------------------------------------------------------------------

namespace
{

uint64_t ResolveMaxAlloc(const PlanInputs &in, RuntimePlan &plan)
{
    if (in.max_alloc_explicit_mib > 0)
    {
        plan.max_alloc = (uint64_t) in.max_alloc_explicit_mib * 1024 * 1024;
        plan.max_alloc_source = RuntimePlan::MaxAllocSource::kExplicit;
    }
    else if (in.max_alloc_device > 0)
    {
        plan.max_alloc = in.max_alloc_device;
        plan.max_alloc_source = RuntimePlan::MaxAllocSource::kDevice;
    }
    else
    {
        plan.max_alloc = kMaxAllocFallbackBytes;
        plan.max_alloc_source = RuntimePlan::MaxAllocSource::kFallback;
    }
    return plan.max_alloc;
}

const char *MaxAllocSourceName(RuntimePlan::MaxAllocSource s)
{
    switch (s)
    {
        case RuntimePlan::MaxAllocSource::kDevice: return "device";
        case RuntimePlan::MaxAllocSource::kExplicit: return "explicit";
        default: return "fallback";
    }
}

std::string Mib(uint64_t bytes)
{
    std::ostringstream os;
    os << (bytes / (1024 * 1024)) << "MiB";
    return os.str();
}

} // namespace

MemoryLedger BuildLedger(const ModelShape &tgt, const ModelShape &dft, uint32_t n_ctx,
                         uint32_t n_ubatch, uint32_t n_seq_max, uint32_t n_max, bool fa_target,
                         uint64_t max_alloc, uint32_t ffn_cpu_layers)
{
    MemoryLedger led;

    // 权重：从 GPU 账本里扣掉两类确定不在 GPU 上的部分（见 ModelShape 的字段注释）。
    // 扣不掉（GGUF 里读不到张量明细）时退化为「全部算在 GPU 上」，方向偏保守。
    uint64_t host_weights = tgt.embedding_bytes;
    if (ffn_cpu_layers > 0 && !tgt.ffn_bytes_per_layer.empty())
    {
        const size_t n = tgt.ffn_bytes_per_layer.size();
        const size_t first = ffn_cpu_layers >= n ? 0 : n - ffn_cpu_layers;
        for (size_t i = first; i < n; ++i)
        {
            host_weights += tgt.ffn_bytes_per_layer[i];
        }
    }
    led.weights_host = std::min(host_weights, tgt.weight_bytes);
    led.weights_tgt = tgt.weight_bytes - led.weights_host;
    led.weights_dft = dft.weight_bytes;

    led.kv_attn_tgt = AttentionKvCacheBytes(tgt, n_ctx);
    led.kv_attn_dft = AttentionKvCacheBytes(dft, n_ctx);
    led.recurrent_tgt = RecurrentStateBytes(tgt, n_seq_max, n_max);
    led.recurrent_dft = RecurrentStateBytes(dft, n_seq_max, n_max);
    // compute buffer 两个模型各一份：实测 32G/16K 是主 571 + 草稿 538 ≈ 1109 MiB，只计一份
    // （650 MiB）是建模对象搞错，会让 ctx 钳制放过实际装不下的档位。
    led.compute_const = kComputeConstBytes + (dft.ok ? kComputeConstDraftBytes : 0);

    // compute buffer 分两块记：
    //   compute_const 是 FA 开启时的实测上界（与 n_ctx 几乎无关：16K 541 → 48K 603 MiB）。
    //   compute_kq 只在关 FA **且该张量真的落在 GPU 上**时才计入：
    //     - kq > max_alloc → supports_op 返回 false、算子整段落 CPU（实测 48K 关 FA 时
    //       CPU compute buffer 2616 MiB、splits 34）→ 它**不占 GPU**，计入就是错的。
    //     - kq ≤ max_alloc → 落在 GPU，但 ggml-alloc 有张量复用：16K 两档实测的 GPU
    //       compute buffer 是 880.02 vs 541.02 MiB，只差 339 MiB，而 kq 自身有 768 MiB
    //       （实测增量 / kq ≈ 44%）。因此取 **kq / 2** 作为略偏保守的经验系数。
    //       ⚠️ 这个 1/2 是实测拟合，不是源码推导；把 kq 整份计入会把 48G/32K 这个
    //       **已实测可用**的档位算成 fits=0（差约 0.1 GB），即过度保守会直接造成误判。
    const uint64_t kq = ComputeKqBytes(tgt, n_ctx, n_ubatch);
    led.compute_kq = (!fa_target && kq > 0 && kq <= max_alloc) ? kq / 2 : 0;
    // 运行期瞬时峰值：存/读回一个前缀检查点要在 GPU 侧过一份 recurrent 状态。
    // r26 的教训就是旧公式完全没建模这一项 —— 加载探测全绿、第 5 个请求存检查点时 err=-5。
    led.runtime_peak = RecurrentStateBytesPerCopy(tgt) + RecurrentStateBytesPerCopy(dft);
    led.safety = kSafetyBytes;

    // 前缀检查点常驻在 **host**（llama_cpp.cpp 里是 std::vector<uint8_t>），GPU 只承担读回的
    // 瞬时峰值（上面那项）。所以 r26 笔记里「每个检查点要额外约 199 MiB GPU 显存」要修正为
    // 「host 常驻 + GPU 瞬时」。一个检查点就是一份 recurrent 状态（实测 tgt_bytes 与之吻合）。
    led.ckpt_bytes = RecurrentStateBytesPerCopy(tgt) + RecurrentStateBytesPerCopy(dft);
    return led;
}

namespace
{

// 一次完整的「给定 ctx 与 n_max」的推导：FA 判据 → 放置（全 GPU / 拆分）→ 账本。
// 抽成函数是因为 ctx 钳制与 n_max 兜底都要反复重算整条链，不能只改其中一项。
struct Evaluation
{
    bool fa_target_on = false;
    bool kq_over_limit = false;
    uint64_t kq_bytes = 0;
    bool full_gpu = false;
    uint32_t ffn_cpu_layers = 0;
    MemoryLedger ledger;
};

Evaluation Evaluate(const PlanInputs &in, const RuntimePlan &plan, uint32_t n_ctx, uint32_t n_max,
                    uint64_t max_alloc)
{
    Evaluation ev;
    ev.kq_bytes = ComputeKqBytes(in.tgt, n_ctx, plan.n_ubatch);
    ev.kq_over_limit = ev.kq_bytes > max_alloc;
    ev.fa_target_on = in.explicit_fa_target >= 0 ? (in.explicit_fa_target > 0) : ev.kq_over_limit;

    // 先按「全 GPU」算一遍决定放置；用保守的 gpu_required()（多算了 kq 份额、运行期峰值与
    // 1 GiB safety），因为算得偏大的代价只是退回已有实测背书的拆分配置，属于安全降级。
    const MemoryLedger full = BuildLedger(in.tgt, in.dft, n_ctx, plan.n_ubatch, in.n_seq_max, n_max,
                                          ev.fa_target_on, max_alloc, 0);
    ev.full_gpu = in.gpu_free > 0 && full.gpu_required() <= in.gpu_free;
    ev.ffn_cpu_layers = ev.full_gpu ? 0 : plan.ffn_cpu_layers;
    ev.ledger = ev.full_gpu ? full
                            : BuildLedger(in.tgt, in.dft, n_ctx, plan.n_ubatch, in.n_seq_max, n_max,
                                          ev.fa_target_on, max_alloc, ev.ffn_cpu_layers);
    return ev;
}

bool Feasible(const Evaluation &ev, uint64_t gpu_free)
{
    // 钳制用硬下界，不用 gpu_required()：理由见 MemoryLedger::gpu_required_min() 的注释。
    return gpu_free == 0 || ev.ledger.gpu_required_min() <= gpu_free;
}

} // namespace

RuntimePlan PlanRuntime(const PlanInputs &in)
{
    RuntimePlan plan;

    plan.n_ubatch = in.explicit_n_ubatch > 0 ? (uint32_t) in.explicit_n_ubatch : kNUbatchDefault;
    plan.n_batch = in.explicit_n_batch > 0 ? (uint32_t) in.explicit_n_batch : kNBatchDefault;
    plan.spec_draft_n_max = in.explicit_n_max > 0 ? (uint32_t) in.explicit_n_max : kNMaxDefault;
    plan.ffn_cpu_layers = in.explicit_ffn_cpu_layers >= 0 ? (uint32_t) in.explicit_ffn_cpu_layers
                                                          : kFfnCpuLayersDefault;
    plan.head_checkpoints = in.explicit_head_checkpoints >= 0 ? in.explicit_head_checkpoints
                                                              : kHeadCheckpointsMax;
    plan.ctx_effective = in.ctx_requested;
    plan.gpu_free_observed = in.gpu_free;
    plan.host_avail_observed = in.host_avail;

    const uint64_t max_alloc = ResolveMaxAlloc(in, plan);

    // ---- ctx 可行上限钳制 + n_max 显存兜底 ----
    // 判据是硬下界（gpu_required_min）：装不下就一定加载不了。顺序刻意是「先降 ctx，再降
    // n_max」—— n_max 默认恒 7 有实测背书（r21 的 llama-bench 扫 1–12 行证明 8 行正好在 verify
    // 悬崖前，r15 证明降到 4 会让短请求慢约 35%），所以它只做最后的兜底，且必须写进日志。
    Evaluation ev = Evaluate(in, plan, plan.ctx_effective, plan.spec_draft_n_max, max_alloc);
    while (!Feasible(ev, in.gpu_free) && plan.ctx_effective > kCtxFloor)
    {
        const uint32_t next = plan.ctx_effective > kCtxStep ? plan.ctx_effective - kCtxStep : kCtxFloor;
        plan.ctx_effective = next < kCtxFloor ? kCtxFloor : next;
        plan.ctx_clamped = true;
        ev = Evaluate(in, plan, plan.ctx_effective, plan.spec_draft_n_max, max_alloc);
    }
    while (!Feasible(ev, in.gpu_free) && plan.spec_draft_n_max > kNMaxFloor &&
           in.explicit_n_max <= 0)
    {
        --plan.spec_draft_n_max;
        plan.n_max_reduced = true;
        ev = Evaluate(in, plan, plan.ctx_effective, plan.spec_draft_n_max, max_alloc);
    }

    plan.kq_bytes = ev.kq_bytes;
    plan.full_gpu = ev.full_gpu;
    plan.ffn_cpu_layers = ev.ffn_cpu_layers > 0 ? ev.ffn_cpu_layers : plan.ffn_cpu_layers;
    plan.ledger = ev.ledger;
    const bool kq_over_limit = ev.kq_over_limit;

    // ---- FA 取值 ----
    // 判据：关 FA 时每层都要建一个完整的 f32 注意力矩阵 kq；单个张量一旦超过设备单次分配
    // 上限，ggml-opencl 的 supports_op 对该算子返回 false ⇒ 调度器把它整段分给 CPU
    // （48K 关 FA 实测 CPU compute buffer 2616 MiB、splits 34）。所以判据就是这一条比较。
    // ⚠️ 那段尺寸检查是有门控的（`ggml-opencl.cpp:19288-19301`）：只在
    // `!adreno_use_large_buffer && (!adreno_has_large_buffer || g_cl_programs_built != 0)` 时生效。
    // 本机虽设了 GGML_OPENCL_ADRENO_USE_LARGE_BUFFER=1 但驱动不支持 ⇒ 两个标志都是 false
    // ⇒ 检查生效，判据成立。**如果将来驱动支持了 large buffer，这条判据就不再成立**，
    // 到时要重测、而不是继续沿用。
    if (in.explicit_fa_target >= 0)
    {
        plan.fa_target = in.explicit_fa_target > 0 ? FlashAttnChoice::kExplicitOn
                                                   : FlashAttnChoice::kExplicitOff;
    }
    else
    {
        plan.fa_target = kq_over_limit ? FlashAttnChoice::kDerivedOn : FlashAttnChoice::kDerivedOff;
    }

    // 草稿侧恒 off：草稿只占每轮 7.5% 的时间，而 r11/r12/r14 实测草稿开 FA 每条慢 24–33%。
    if (in.explicit_fa_draft >= 0)
    {
        plan.fa_draft = in.explicit_fa_draft > 0 ? FlashAttnChoice::kExplicitOn
                                                 : FlashAttnChoice::kExplicitOff;
    }
    else
    {
        plan.fa_draft = FlashAttnChoice::kDerivedOff;
    }

    // ---- head_checkpoints 由 host 内存推导，不由显存推导 ----
    // 检查点常驻在 host（llama_cpp.cpp 的 std::vector<uint8_t>），常驻上限是
    // head_checkpoints + 3 份（llama_cpp.cpp 里 prefix_ckpts_ 超过 n_keep_head + 3 就淘汰）。
    if (in.explicit_head_checkpoints < 0)
    {
        const uint64_t budget = in.host_avail == 0 || in.host_avail == UINT64_MAX
                                    ? UINT64_MAX
                                    : (uint64_t) ((double) in.host_avail *
                                                  kHostCheckpointBudgetFraction);
        int chosen = 0;
        for (int n = kHeadCheckpointsMax; n >= 0; --n)
        {
            if (plan.ledger.ckpt_bytes == 0 ||
                (uint64_t) (n + 3) * plan.ledger.ckpt_bytes <= budget)
            {
                chosen = n;
                break;
            }
        }
        plan.head_checkpoints = chosen;
    }
    plan.ledger.host_checkpoints =
        (uint64_t) (plan.head_checkpoints + 3) * plan.ledger.ckpt_bytes;

    std::ostringstream os;
    os << "ctx=" << plan.ctx_effective << " (requested " << in.ctx_requested << ")"
       << "; max_alloc=" << Mib(max_alloc) << " (" << MaxAllocSourceName(plan.max_alloc_source) << ")"
       << "; kq=" << Mib(plan.kq_bytes) << (kq_over_limit ? " OVER" : " fits")
       << "; fa_target=" << (FlashAttnEnabled(plan.fa_target) ? "on" : "off")
       << (FlashAttnExplicit(plan.fa_target) ? " (explicit)" : " (derived)")
       << "; fa_draft=" << (FlashAttnEnabled(plan.fa_draft) ? "on" : "off")
       << (FlashAttnExplicit(plan.fa_draft) ? " (explicit)" : " (derived)")
       << "; ub=" << plan.n_ubatch << (in.explicit_n_ubatch > 0 ? " (explicit)" : " (derived)")
       << "; b=" << plan.n_batch << (in.explicit_n_batch > 0 ? " (explicit)" : " (derived)")
       << "; n_max=" << plan.spec_draft_n_max
       << (in.explicit_n_max > 0 ? " (explicit)" : " (derived)")
       << "; head_ckpt=" << plan.head_checkpoints
       << (in.explicit_head_checkpoints >= 0 ? " (explicit)" : " (derived)")
       << "; ckpt_bytes=" << Mib(plan.ledger.ckpt_bytes)
       << "; host_ckpt=" << Mib(plan.ledger.host_checkpoints)
       << "; weights_host=" << Mib(plan.ledger.weights_host)
       << "; full_gpu=" << (plan.full_gpu ? 1 : 0)
       << "; required_min=" << Mib(plan.ledger.gpu_required_min())
       << "; required=" << Mib(plan.ledger.gpu_required())
       << "; gpu_free=" << Mib(in.gpu_free);
    if (plan.ctx_clamped)
    {
        os << "; WARNING: context clamped from " << in.ctx_requested << " to " << plan.ctx_effective
           << " (hard minimum would not fit in GPU memory)";
    }
    if (plan.n_max_reduced)
    {
        os << "; WARNING: spec_draft_n_max reduced to " << plan.spec_draft_n_max
           << " to free recurrent state copies (each copy ~"
           << Mib(RecurrentStateBytesPerCopy(in.tgt)) << ")";
    }
    if (!Feasible(ev, in.gpu_free))
    {
        os << "; WARNING: still over the hard minimum after clamping; load may fail";
    }
    if (FlashAttnExplicit(plan.fa_target) && !FlashAttnEnabled(plan.fa_target) && kq_over_limit)
    {
        // 显式关 FA 却越界：argv 会照配置走，但注意力会整段回退 CPU，必须在日志里喊出来。
        os << "; WARNING: explicit fa=off with kq over device max_alloc"
              " => attention falls back to CPU";
    }
    plan.rationale = os.str();

    return plan;
}

} // namespace llama_runtime
