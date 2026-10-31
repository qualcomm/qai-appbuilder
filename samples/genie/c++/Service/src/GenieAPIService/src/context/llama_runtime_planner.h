//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_RUNTIME_PLANNER_H
#define LLAMA_RUNTIME_PLANNER_H

#include <cstdint>
#include <string>
#include <vector>

namespace llama_runtime
{

struct ModelShape
{
    bool ok = false;
    uint64_t weight_bytes = 0;

    uint64_t embedding_bytes = 0;
    std::vector<uint64_t> ffn_bytes_per_layer;

    uint32_t n_layer_all = 0;
    uint32_t n_layer_nextn = 0;
    uint32_t n_layer_attn = 0;
    uint32_t n_layer_recr = 0;

    uint32_t n_head = 0;
    uint32_t n_head_kv = 0;
    uint32_t key_length = 0;
    uint32_t n_embd = 0;
    uint32_t n_swa = 0;
    uint32_t full_attention_interval = 0;

    uint32_t ssm_d_conv = 0;
    uint32_t ssm_d_inner = 0;
    uint32_t ssm_d_state = 0;
    uint32_t ssm_n_group = 0;

    uint32_t wkv_head_size = 0;
    uint32_t token_shift_count = 2;
    uint32_t n_shortconv_l_cache = 0;
    uint32_t n_embd_head_kda = 0;
    uint32_t n_embd_head_la = 0;

    uint32_t n_layer() const;

    uint64_t n_embd_r() const;

    uint64_t n_embd_s() const;
};

struct MemoryLedger
{
    uint64_t weights_tgt = 0;
    uint64_t weights_dft = 0;
    uint64_t weights_host = 0;
    uint64_t kv_attn_tgt = 0;
    uint64_t kv_attn_dft = 0;
    uint64_t recurrent_tgt = 0;
    uint64_t recurrent_dft = 0;
    uint64_t compute_const = 0;
    uint64_t compute_kq = 0;
    uint64_t runtime_peak = 0;
    uint64_t lazy_scratch = 0;
    uint64_t safety = 0;

    uint64_t host_checkpoints = 0;
    uint64_t ckpt_bytes = 0;

    uint64_t gpu_required() const;

    uint64_t gpu_required_min() const;
};

enum class FlashAttnChoice
{
    kDerivedOff = 0,
    kDerivedOn,
    kExplicitOff,
    kExplicitOn,
};

bool FlashAttnEnabled(FlashAttnChoice c);
bool FlashAttnExplicit(FlashAttnChoice c);

struct PlanInputs
{
    ModelShape tgt;
    ModelShape dft;

    uint64_t gpu_free = 0;
    uint64_t host_avail = 0;
    uint32_t ctx_requested = 0;

    uint64_t max_alloc_device = 0;
    int max_alloc_explicit_mib = -1;

    uint32_t n_seq_max = 1;
    bool enable_qwen38_dflash_policy = false;

    int explicit_fa_target = -1;
    int explicit_fa_draft = -1;
    int explicit_n_ubatch = -1;
    int explicit_n_batch = -1;
    int explicit_n_max = -1;
    int explicit_head_checkpoints = -1;
    int explicit_ffn_cpu_layers = -1;
};

struct RuntimePlan
{
    bool full_gpu = false;
    uint32_t ffn_cpu_layers = 0;
    bool ffn_cpu_escalated = false;
    FlashAttnChoice fa_target = FlashAttnChoice::kDerivedOff;
    FlashAttnChoice fa_draft = FlashAttnChoice::kDerivedOff;
    uint32_t n_batch = 0;
    uint32_t n_ubatch = 0;
    uint32_t spec_draft_n_max = 0;
    int head_checkpoints = 0;
    uint32_t ctx_effective = 0;
    bool ctx_clamped = false;
    bool n_max_reduced = false;

    uint64_t max_alloc = 0;
    enum class MaxAllocSource
    {
        kDevice = 0,
        kExplicit,
        kFallback,
    } max_alloc_source = MaxAllocSource::kFallback;

    uint64_t kq_bytes = 0;
    uint64_t gpu_free_observed = 0;
    uint64_t host_avail_observed = 0;

    MemoryLedger ledger;
    std::string rationale;
};

uint64_t AttentionKvCacheBytes(const ModelShape &shape, uint64_t n_ctx);

uint64_t RecurrentStateBytes(const ModelShape &shape, uint32_t n_seq_max, uint32_t n_rs_seq);

uint64_t RecurrentStateBytesPerCopy(const ModelShape &shape);

uint64_t ComputeKqBytes(const ModelShape &shape, uint64_t n_ctx, uint32_t n_ubatch);

uint64_t CriticalContextForFlashAttn(const ModelShape &shape, uint32_t n_ubatch, uint64_t max_alloc);

constexpr uint64_t kComputeConstBytes = 650ULL * 1024 * 1024;

constexpr uint64_t kComputeConstDraftBytes = 550ULL * 1024 * 1024;

constexpr uint64_t kSafetyBytes = 1024ULL * 1024 * 1024;

constexpr uint64_t kMaxAllocFallbackBytes = 2048ULL * 1024 * 1024;

constexpr uint32_t kFfnCpuLayersDefault = 30;

constexpr uint64_t kGdnScratchBytesPerHeadUbatch = 3072;

constexpr uint64_t kSplitHeadroomBytes = 192ULL * 1024 * 1024;
constexpr uint32_t kFfnCpuLayersEscalationMax = 46;
constexpr uint32_t kNBatchDefault = 2048;
constexpr uint32_t kNUbatchDefault = 512;
constexpr uint32_t kNMaxDefault = 7;
constexpr uint32_t kNMaxFloor = 3;
constexpr uint32_t kCtxStep = 4096;
constexpr uint32_t kCtxFloor = 8192;
constexpr int kHeadCheckpointsMax = 2;

constexpr double kHostCheckpointBudgetFraction = 0.1;

ModelShape ReadGgufModelShape(const std::string &gguf_path);

void DeriveLayerSplit(ModelShape &shape);

uint64_t QueryDeviceMaxAllocBytes();

MemoryLedger BuildLedger(const ModelShape &tgt, const ModelShape &dft, uint32_t n_ctx,
                         uint32_t n_ubatch, uint32_t n_seq_max, uint32_t n_max, bool fa_target,
                         uint64_t max_alloc, uint32_t ffn_cpu_layers,
                         bool enable_qwen38_dflash_policy = false);

RuntimePlan PlanRuntime(const PlanInputs &in);

}

#endif
