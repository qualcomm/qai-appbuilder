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

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace llama_runtime
{

namespace
{

constexpr uint64_t kKvTypeSizeBytes = 2;

constexpr uint64_t kRecurrentTypeSizeBytes = 4;

}

uint32_t ModelShape::n_layer() const
{

    return n_layer_all > n_layer_nextn ? n_layer_all - n_layer_nextn : 0;
}

uint64_t ModelShape::n_embd_r() const
{

    if (wkv_head_size != 0)
    {
        return (uint64_t) token_shift_count * n_embd;
    }
    if (n_shortconv_l_cache != 0)
    {
        return n_shortconv_l_cache > 1 ? (uint64_t) n_embd * (n_shortconv_l_cache - 1) : 0;
    }
    if (n_embd_head_kda != 0)
    {
        const uint64_t d_inner = (uint64_t) n_head * n_embd_head_kda;
        return 3ULL * (ssm_d_conv > 0 ? ssm_d_conv - 1 : 3) * d_inner;
    }

    return (uint64_t) (ssm_d_conv > 0 ? ssm_d_conv - 1 : 0) *
           ((uint64_t) ssm_d_inner + 2ULL * ssm_n_group * ssm_d_state);
}

uint64_t ModelShape::n_embd_s() const
{

    if (wkv_head_size != 0)
    {
        return (uint64_t) n_embd * wkv_head_size;
    }
    if (n_embd_head_kda != 0)
    {
        return (uint64_t) n_embd_head_kda * n_embd_head_kda * n_head;
    }
    if (n_embd_head_la != 0)
    {
        return (uint64_t) n_embd_head_la * n_embd_head_la * n_head;
    }

    return (uint64_t) ssm_d_state * ssm_d_inner;
}

uint64_t MemoryLedger::gpu_required() const
{
    return weights_tgt + weights_dft + kv_attn_tgt + kv_attn_dft + recurrent_tgt + recurrent_dft +
           compute_const + compute_kq + runtime_peak + lazy_scratch + safety;
}

uint64_t MemoryLedger::gpu_required_min() const
{
    return weights_tgt + weights_dft + kv_attn_tgt + kv_attn_dft + recurrent_tgt + recurrent_dft +
           compute_const + runtime_peak + lazy_scratch;
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

    return per_copy * n_seq_max * (1ULL + n_rs_seq);
}

uint64_t ComputeKqBytes(const ModelShape &shape, uint64_t n_ctx, uint32_t n_ubatch)
{
    if (!shape.ok || shape.n_head == 0 || shape.n_layer_attn == 0 || n_ubatch == 0)
    {
        return 0;
    }

    const uint64_t n_tokens = std::min<uint64_t>(n_ctx, n_ubatch);
    return n_ctx * n_tokens * shape.n_head * 4ULL;
}

uint64_t CriticalContextForFlashAttn(const ModelShape &shape, uint32_t n_ubatch, uint64_t max_alloc)
{
    if (!shape.ok || shape.n_head == 0 || shape.n_layer_attn == 0 || n_ubatch == 0)
    {
        return 0;
    }

    const uint64_t per_ctx = (uint64_t) n_ubatch * shape.n_head * 4ULL;
    return per_ctx == 0 ? 0 : max_alloc / per_ctx;
}

#ifdef USE_GGUF
namespace
{

void ReadTensorBreakdown(gguf_context *ctx, ModelShape &shape);
}
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

}
#endif

void DeriveLayerSplit(ModelShape &shape)
{
    const uint32_t n_eff = shape.n_layer();
    const bool has_recurrent = (shape.n_embd_r() + shape.n_embd_s()) > 0;

    if (!has_recurrent)
    {

        shape.n_layer_attn = n_eff;
        shape.n_layer_recr = 0;
        return;
    }

    if (shape.full_attention_interval > 1)
    {

        shape.n_layer_attn = n_eff / shape.full_attention_interval;
        shape.n_layer_recr = n_eff - shape.n_layer_attn;
    }
    else
    {

        shape.n_layer_attn = 0;
        shape.n_layer_recr = n_eff;
    }
}

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

    FreeLibrary(lib);
    return best;
#else
    return 0;
#endif
}

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

bool IsQwen38DflashShape(const ModelShape &tgt, const ModelShape &dft)
{
    return tgt.ok && tgt.n_layer_all == 65 && tgt.n_layer_nextn == 1 && tgt.n_layer_attn == 16 &&
           tgt.n_layer_recr == 48 && tgt.full_attention_interval == 4 && tgt.ssm_d_conv == 4 &&
           tgt.ssm_d_inner == 6144 && tgt.ssm_d_state == 128 && tgt.ssm_n_group == 16 &&
           dft.ok && dft.n_layer_nextn == 0 && dft.n_layer_attn > 0 && dft.n_layer_recr == 0;
}

bool SplitHasHeadroom(const MemoryLedger &ledger, uint64_t gpu_free)
{
    const uint64_t required = ledger.gpu_required_min();
    return required <= gpu_free && kSplitHeadroomBytes <= gpu_free - required;
}

std::string Mib(uint64_t bytes)
{
    std::ostringstream os;
    os << (bytes / (1024 * 1024)) << "MiB";
    return os.str();
}

}

MemoryLedger BuildLedger(const ModelShape &tgt, const ModelShape &dft, uint32_t n_ctx,
                         uint32_t n_ubatch, uint32_t n_seq_max, uint32_t n_max, bool fa_target,
                         uint64_t max_alloc, uint32_t ffn_cpu_layers,
                         bool enable_qwen38_dflash_policy)
{
    MemoryLedger led;

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

    led.compute_const = kComputeConstBytes + (dft.ok ? kComputeConstDraftBytes : 0);

    const uint64_t kq = ComputeKqBytes(tgt, n_ctx, n_ubatch);
    led.compute_kq = (!fa_target && kq > 0 && kq <= max_alloc) ? kq / 2 : 0;

    led.runtime_peak = RecurrentStateBytesPerCopy(tgt) + RecurrentStateBytesPerCopy(dft);
    if (enable_qwen38_dflash_policy && IsQwen38DflashShape(tgt, dft) && tgt.ssm_d_state != 0)
    {
        const uint64_t h_v = tgt.ssm_d_inner / tgt.ssm_d_state;

        led.lazy_scratch = kGdnScratchBytesPerHeadUbatch * h_v * n_ubatch;
    }
    led.safety = kSafetyBytes;

    led.ckpt_bytes = RecurrentStateBytesPerCopy(tgt) + RecurrentStateBytesPerCopy(dft);
    return led;
}

namespace
{

struct Evaluation
{
    bool fa_target_on = false;
    bool kq_over_limit = false;
    uint64_t kq_bytes = 0;
    bool full_gpu = false;
    uint32_t ffn_cpu_layers = 0;
    bool ffn_cpu_escalated = false;
    MemoryLedger ledger;
};

Evaluation Evaluate(const PlanInputs &in, const RuntimePlan &plan, uint32_t n_ctx, uint32_t n_max,
                    uint64_t max_alloc)
{
    Evaluation ev;
    ev.kq_bytes = ComputeKqBytes(in.tgt, n_ctx, plan.n_ubatch);
    ev.kq_over_limit = ev.kq_bytes > max_alloc;
    ev.fa_target_on = in.explicit_fa_target >= 0 ? (in.explicit_fa_target > 0) : ev.kq_over_limit;

    const bool qwen38_dflash =
        in.enable_qwen38_dflash_policy && IsQwen38DflashShape(in.tgt, in.dft);
    const MemoryLedger full = BuildLedger(in.tgt, in.dft, n_ctx, plan.n_ubatch, in.n_seq_max, n_max,
                                          ev.fa_target_on, max_alloc, 0, qwen38_dflash);
    ev.full_gpu = in.gpu_free > 0 && full.gpu_required() <= in.gpu_free;
    ev.ffn_cpu_layers = ev.full_gpu ? 0 : plan.ffn_cpu_layers;
    if (ev.full_gpu)
    {
        ev.ledger = full;
        return ev;
    }

    ev.ledger = BuildLedger(in.tgt, in.dft, n_ctx, plan.n_ubatch, in.n_seq_max, n_max,
                            ev.fa_target_on, max_alloc, ev.ffn_cpu_layers, qwen38_dflash);
    if (in.gpu_free > 0 && in.explicit_ffn_cpu_layers < 0 && qwen38_dflash)
    {
        const uint32_t max_layers = std::min(kFfnCpuLayersEscalationMax, in.tgt.n_layer());
        while (ev.ffn_cpu_layers < max_layers && !SplitHasHeadroom(ev.ledger, in.gpu_free))
        {
            ++ev.ffn_cpu_layers;
            ev.ledger = BuildLedger(in.tgt, in.dft, n_ctx, plan.n_ubatch, in.n_seq_max, n_max,
                                    ev.fa_target_on, max_alloc, ev.ffn_cpu_layers, qwen38_dflash);
        }
        ev.ffn_cpu_escalated = ev.ffn_cpu_layers > plan.ffn_cpu_layers;
    }
    return ev;
}

bool Feasible(const Evaluation &ev, uint64_t gpu_free)
{

    return gpu_free == 0 || ev.ledger.gpu_required_min() <= gpu_free;
}

}

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
    plan.ffn_cpu_layers = ev.ffn_cpu_layers;
    plan.ffn_cpu_escalated = ev.ffn_cpu_escalated;
    plan.ledger = ev.ledger;
    const bool kq_over_limit = ev.kq_over_limit;

    if (in.explicit_fa_target >= 0)
    {
        plan.fa_target = in.explicit_fa_target > 0 ? FlashAttnChoice::kExplicitOn
                                                   : FlashAttnChoice::kExplicitOff;
    }
    else
    {
        plan.fa_target = kq_over_limit ? FlashAttnChoice::kDerivedOn : FlashAttnChoice::kDerivedOff;
    }

    if (in.explicit_fa_draft >= 0)
    {
        plan.fa_draft = in.explicit_fa_draft > 0 ? FlashAttnChoice::kExplicitOn
                                                 : FlashAttnChoice::kExplicitOff;
    }
    else
    {
        plan.fa_draft = FlashAttnChoice::kDerivedOff;
    }

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
       << "; lazy_scratch=" << Mib(plan.ledger.lazy_scratch)
       << "; ffn_cpu=" << plan.ffn_cpu_layers
       << (plan.ffn_cpu_escalated ? " (escalated from " + std::to_string(in.explicit_ffn_cpu_layers >= 0
                                                                              ? in.explicit_ffn_cpu_layers
                                                                              : (int) kFfnCpuLayersDefault) + ")"
                                  : "")
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

        os << "; WARNING: explicit fa=off with kq over device max_alloc"
              " => attention falls back to CPU";
    }
    plan.rationale = os.str();

    return plan;
}

}
