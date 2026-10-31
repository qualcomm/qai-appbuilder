//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// 离线单元测试：GGUF 运行参数规划器（llama_runtime_planner）。
//
// 刻意不依赖 USE_GGUF / llama.cpp / Windows API / 真实硬件，因此可以用任意本地 C++17
// 编译器单独编译运行，不必上远程 ARM64 真机 —— 这正是把规划器做成纯函数的目的：
// 64G/64K 这种没有真机的档位只能靠公式推导，而公式必须能在本机被反复核对。
//
// 本机（x86-64，VS 2022）编译运行方式见 test/test_runtime_planner.md。
//
//==============================================================================

#include "../src/GenieAPIService/src/context/llama_runtime_planner.h"

#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>

namespace
{

int g_failed = 0;
int g_total = 0;

constexpr uint64_t kMiB = 1024ULL * 1024;
constexpr uint64_t kGiB = 1024ULL * 1024 * 1024;

void CheckEq(const char *what, uint64_t actual, uint64_t expected)
{
    ++g_total;
    if (actual == expected)
    {
        std::printf("  [ ok ] %-52s = %llu\n", what, (unsigned long long) actual);
        return;
    }
    ++g_failed;
    std::printf("  [FAIL] %-52s = %llu, expected %llu (delta %lld)\n", what,
                (unsigned long long) actual, (unsigned long long) expected,
                (long long) actual - (long long) expected);
}

// 账本对账用的宽松判据：计划的验收标准是「与启动日志误差 ≤ 5%」。
void CheckNear(const char *what, uint64_t actual, uint64_t expected, double tol)
{
    ++g_total;
    const double diff = (double) ((actual > expected) ? (actual - expected) : (expected - actual));
    const double rel = expected == 0 ? (actual == 0 ? 0.0 : 1.0) : diff / (double) expected;
    if (rel <= tol)
    {
        std::printf("  [ ok ] %-52s = %llu (within %.2f%% of %llu)\n", what,
                    (unsigned long long) actual, rel * 100.0, (unsigned long long) expected);
        return;
    }
    ++g_failed;
    std::printf("  [FAIL] %-52s = %llu, expected %llu (off by %.2f%%)\n", what,
                (unsigned long long) actual, (unsigned long long) expected, rel * 100.0);
}

void CheckTrue(const char *what, bool cond)
{
    ++g_total;
    if (cond)
    {
        std::printf("  [ ok ] %s\n", what);
        return;
    }
    ++g_failed;
    std::printf("  [FAIL] %s\n", what);
}

// ---------------------------------------------------------------------------
// 真实模型形状：Qwen3.8-27B-Q4_0（target）与 Qwen3.8-27B-DFlash2-Q4_K_M（draft）。
//
// 这些数字不是猜的，每一项都能被启动日志或源码钉住：
//   block_count = 65、nextn_predict_layers = 1  ⇒ n_layer() = 64
//   full_attention_interval = 4                 ⇒ attention 16 层、recurrent 48 层
//   n_head = 24、n_head_kv = 4、key_length = 256（日志 DK=DV=256，gqa=6）
//   ssm: d_conv=4、d_inner=6144、d_state=128、n_group=16
// 这组 SSM 参数由「单份 recurrent = 156,893,184 B」反解并与检查点
// `tgt_bytes=156894364` 吻合（差 1180 B 是检查点自身的头部）。
// ---------------------------------------------------------------------------
llama_runtime::ModelShape TargetShape()
{
    llama_runtime::ModelShape s;
    s.ok = true;
    s.weight_bytes = 15716411392ULL;   // 约 14.64 GiB，日志 OpenCL 9907.90 + CPU_KLEIDIAI 4159.69 + CPU 682.03 MiB
    s.n_layer_all = 65;
    s.n_layer_nextn = 1;
    s.n_head = 24;
    s.n_head_kv = 4;
    s.key_length = 256;
    s.n_embd = 4096;
    s.n_swa = 2048;
    s.full_attention_interval = 4;
    s.ssm_d_conv = 4;
    s.ssm_d_inner = 6144;
    s.ssm_d_state = 128;
    s.ssm_n_group = 16;
    // 张量粒度的权重拆分。**按真机校准，不按启动日志的 buffer 大小直接填**：
    //   启动日志 load_tensors 段是 CPU 682.03 + CPU_KLEIDIAI 4159.69 = 4841.72 MiB，
    //   而真机规划器从 GGUF 按张量统计出的 weights_host 是 4985 MiB（多约 143 MiB）。
    //   差值来自 GGUF 里被前缀匹配进来的额外张量/对齐，不是公式错 —— 这里用真机值，
    //   离线断言才能和真机的 required_min 对得上（实测偏差 < 1.5%）。
    s.embedding_bytes = (uint64_t) (682.03 * 1024 * 1024);
    s.ffn_bytes_per_layer.assign(65, (uint64_t) ((4985.0 - 682.03) * 1024 * 1024) / 30);
    llama_runtime::DeriveLayerSplit(s);
    return s;
}

// draft 侧（DFlash2，6 层、全 GPU）。它只有 attention、没有 recurrent 状态
// （检查点里 dft_bytes=41992344 是 draft 自己的 KV/状态快照，不参与本测试的 recurrent 对账）。
llama_runtime::ModelShape DraftShape()
{
    llama_runtime::ModelShape s;
    s.ok = true;
    s.weight_bytes = 1143900160ULL;    // 约 1091 MiB，与 `-fitt auto: 1024 + draft weights 1091 MiB` 吻合
    s.n_layer_all = 6;
    s.n_head = 24;
    s.n_head_kv = 4;
    s.key_length = 256;
    s.n_embd = 4096;
    llama_runtime::DeriveLayerSplit(s);
    return s;
}

void TestLayerSplit()
{
    std::printf("\n== 层数划分（A5：必须让 KV 与 recurrent 两项同时对上启动日志） ==\n");
    const llama_runtime::ModelShape tgt = TargetShape();
    CheckEq("n_layer() = block_count - nextn_predict_layers", tgt.n_layer(), 64);
    CheckEq("n_layer_attn", tgt.n_layer_attn, 16);
    CheckEq("n_layer_recr", tgt.n_layer_recr, 48);
    CheckEq("n_embd_r (mamba 分支)", tgt.n_embd_r(), 30720);
    CheckEq("n_embd_s (mamba 分支)", tgt.n_embd_s(), 786432);

    const llama_runtime::ModelShape dft = DraftShape();
    CheckEq("draft n_layer_attn (无 ssm ⇒ 全是 attention)", dft.n_layer_attn, 6);
    CheckEq("draft n_layer_recr", dft.n_layer_recr, 0);
}

void TestAttentionKv()
{
    std::printf("\n== attention KV（锚点：启动日志 32K / f16 = 2048 MiB） ==\n");
    const llama_runtime::ModelShape tgt = TargetShape();
    CheckEq("kv_attn @ 32768", llama_runtime::AttentionKvCacheBytes(tgt, 32768), 2048 * kMiB);
    CheckEq("kv_attn @ 16384", llama_runtime::AttentionKvCacheBytes(tgt, 16384), 1024 * kMiB);
    // 「每 16K 约 1 GiB」这条实测口述由此成立。
    CheckEq("kv_attn @ 49152", llama_runtime::AttentionKvCacheBytes(tgt, 49152), 3072 * kMiB);
    CheckEq("kv_attn @ 65536", llama_runtime::AttentionKvCacheBytes(tgt, 65536), 4096 * kMiB);
    CheckEq("kv_attn(48K) - kv_attn(32K) = 1 GiB",
            llama_runtime::AttentionKvCacheBytes(tgt, 49152) -
                llama_runtime::AttentionKvCacheBytes(tgt, 32768), kGiB);
}

void TestRecurrent()
{
    std::printf("\n== recurrent 状态（锚点：日志 1197 MiB / n_rs_seq=7；单份 = 检查点 tgt_bytes） ==\n");
    const llama_runtime::ModelShape tgt = TargetShape();
    const uint64_t per_copy = llama_runtime::RecurrentStateBytesPerCopy(tgt);
    CheckEq("单份 recurrent 字节数", per_copy, 156893184ULL);
    // 检查点 tgt_bytes=156894364 B（149.63 MiB），差 1180 B 是状态序列化的头部。
    CheckNear("单份 recurrent vs 检查点 tgt_bytes=156894364", per_copy, 156894364ULL, 0.05);

    const uint64_t total = llama_runtime::RecurrentStateBytes(tgt, 1, 7);
    CheckEq("recurrent 总量 (n_seq_max=1, n_rs_seq=7)", total, 1255145472ULL);
    CheckNear("recurrent 总量 vs 日志 1197 MiB", total, 1197 * kMiB, 0.05);
    CheckEq("recurrent 与 n_ctx 无关：按 n_rs_seq 线性", total, per_copy * 8);

    // D4：降 n_max 每档省一份约 150 MiB 显存。
    CheckEq("n_rs_seq 7 -> 3 省下的字节数",
            total - llama_runtime::RecurrentStateBytes(tgt, 1, 3), per_copy * 4);

    const llama_runtime::ModelShape dft = DraftShape();
    CheckEq("draft 无 recurrent", llama_runtime::RecurrentStateBytes(dft, 1, 7), 0);
}

void TestComputeKq()
{
    std::printf("\n== 非 FA 的注意力矩阵 kq（锚点：48K / ub512 = 2304 MiB，超 2048 MiB 上限） ==\n");
    const llama_runtime::ModelShape tgt = TargetShape();
    CheckEq("kq @ 16384, ub 512", llama_runtime::ComputeKqBytes(tgt, 16384, 512), 768 * kMiB);
    CheckEq("kq @ 32768, ub 512", llama_runtime::ComputeKqBytes(tgt, 32768, 512), 1536 * kMiB);
    CheckEq("kq @ 49152, ub 512", llama_runtime::ComputeKqBytes(tgt, 49152, 512), 2304 * kMiB);
    CheckEq("kq @ 65536, ub 512", llama_runtime::ComputeKqBytes(tgt, 65536, 512), 3072 * kMiB);
    // D3 的两个方向相反的候选：降 ub 把 kq 压回上限内；抬 ub 只增线性项。
    CheckEq("kq @ 49152, ub 256", llama_runtime::ComputeKqBytes(tgt, 49152, 256), 1152 * kMiB);
    CheckEq("kq @ 16384, ub 1024", llama_runtime::ComputeKqBytes(tgt, 16384, 1024), 1536 * kMiB);
    // n_ctx < n_ubatch 时 reserve 取 n_tokens = n_ctx（llama-context.cpp 的最坏情况图）。
    CheckEq("kq @ 256, ub 512 (n_tokens 被 n_ctx 夹住)",
            llama_runtime::ComputeKqBytes(tgt, 256, 512), 256ULL * 256 * 24 * 4);
}

// ---------------------------------------------------------------------------
// FA 自适应判据与三档回归
// ---------------------------------------------------------------------------

// 各机 GPU 可见池的可用值（日志 `gpu_available=` 原值口径）。
constexpr uint64_t kGpuFree32G = 15810478080ULL;   // 32G 机，日志逐字
constexpr uint64_t kGpuFree48G = 24490000000ULL;   // 48G 机，日志 24.49 GB
constexpr uint64_t kGpuFree64G = 32000000000ULL;   // 64G 机，按「池约为系统内存一半」外推（未实测）
constexpr uint64_t kMaxAlloc = 2048ULL * kMiB;     // CL_DEVICE_MAX_MEM_ALLOC_SIZE，本机实测

llama_runtime::PlanInputs MakeInputs(uint32_t ctx, uint64_t gpu_free, uint64_t max_alloc_device)
{
    llama_runtime::PlanInputs in;
    in.tgt = TargetShape();
    in.dft = DraftShape();
    in.gpu_free = gpu_free;
    in.host_avail = 8ULL * kGiB;
    in.enable_qwen38_dflash_policy = true;
    in.max_alloc_device = max_alloc_device;
    in.ctx_requested = ctx;
    return in;
}

void TestFlashAttnCriterion()
{
    std::printf("\n== FA 判据：kq 是否超设备单次分配上限（D3） ==\n");
    const llama_runtime::ModelShape tgt = TargetShape();
    CheckEq("临界 ctx @ ub 512", llama_runtime::CriticalContextForFlashAttn(tgt, 512, kMaxAlloc),
            43690);
    CheckEq("临界 ctx @ ub 256", llama_runtime::CriticalContextForFlashAttn(tgt, 256, kMaxAlloc),
            87381);
    CheckEq("临界 ctx @ ub 1024", llama_runtime::CriticalContextForFlashAttn(tgt, 1024, kMaxAlloc),
            21845);

    struct Case
    {
        uint32_t ctx;
        bool expect_on;
    };
    const Case cases[] = {{16384, false}, {24576, false}, {32768, false}, {40960, false},
                          {49152, true},  {65536, true}};
    for (const Case &c: cases)
    {
        const llama_runtime::RuntimePlan plan =
            llama_runtime::PlanRuntime(MakeInputs(c.ctx, kGpuFree64G, kMaxAlloc));
        char label[128];
        std::snprintf(label, sizeof(label), "ctx %u => fa_target %s", c.ctx,
                      c.expect_on ? "on" : "off");
        CheckTrue(label, llama_runtime::FlashAttnEnabled(plan.fa_target) == c.expect_on);
        CheckTrue("  fa_target 标记为 derived（未显式配置）",
                  !llama_runtime::FlashAttnExplicit(plan.fa_target));
        CheckTrue("  fa_draft 恒 off（草稿只占每轮 7.5%，开 FA 实测慢 24-33%）",
                  !llama_runtime::FlashAttnEnabled(plan.fa_draft));
    }

    // -ub 256 让 48K 也能关 FA —— D3 列出的待验证候选，公式必须能表达它。
    llama_runtime::PlanInputs in = MakeInputs(49152, kGpuFree64G, kMaxAlloc);
    in.explicit_n_ubatch = 256;
    const llama_runtime::RuntimePlan plan = llama_runtime::PlanRuntime(in);
    CheckTrue("48K + 显式 ub 256 => kq 回到上限内 => 自动关 FA",
              !llama_runtime::FlashAttnEnabled(plan.fa_target));
    CheckEq("  kq @ 48K/ub256", plan.kq_bytes, 1152 * kMiB);
}

void TestMeasuredTiers()
{
    std::printf("\n== 三档回归断言（D2：公式代入必须复现已实测的参数选择） ==\n");
    struct Tier
    {
        const char *name;
        uint32_t ctx;
        uint64_t gpu_free;
        bool expect_full_gpu;
        bool expect_fa;
    };
    const Tier tiers[] = {
        {"32G / 16K", 16384, kGpuFree32G, false, false},
        {"48G / 32K", 32768, kGpuFree48G, true, false},
        {"48G / 48K", 49152, kGpuFree48G, true, true},
        {"64G / 64K", 65536, kGpuFree64G, true, true},
    };
    for (const Tier &t: tiers)
    {
        const llama_runtime::RuntimePlan plan =
            llama_runtime::PlanRuntime(MakeInputs(t.ctx, t.gpu_free, kMaxAlloc));
        const uint64_t req = plan.ledger.gpu_required();
        std::printf("  -- %s: required=%.2f GiB free=%.2f GiB margin=%+.2f GiB\n", t.name,
                    (double) req / (double) kGiB, (double) t.gpu_free / (double) kGiB,
                    ((double) t.gpu_free - (double) req) / (double) kGiB);
        char label[160];
        std::snprintf(label, sizeof(label), "%s => full_gpu=%d", t.name, t.expect_full_gpu ? 1 : 0);
        CheckTrue(label, plan.full_gpu == t.expect_full_gpu);
        std::snprintf(label, sizeof(label), "%s => fa_target=%s", t.name, t.expect_fa ? "on" : "off");
        CheckTrue(label, llama_runtime::FlashAttnEnabled(plan.fa_target) == t.expect_fa);
        std::snprintf(label, sizeof(label), "%s => ub/b/n_max 保持默认", t.name);
        CheckTrue(label, plan.n_ubatch == 512 && plan.n_batch == 2048 && plan.spec_draft_n_max == 7);
        const uint32_t expected_cpu_layers = t.expect_full_gpu ? 0 : 32;
        std::snprintf(label, sizeof(label), "%s => ffn_cpu_layers=%u", t.name, expected_cpu_layers);
        CheckEq(label, plan.ffn_cpu_layers, expected_cpu_layers);
        std::snprintf(label, sizeof(label), "%s => 升档标记与拆分状态一致", t.name);
        CheckTrue(label, plan.ffn_cpu_escalated == !t.expect_full_gpu);
    }
}

void TestLazyScratchAndEscalation()
{
    std::printf("\n== Qwen3.8-27B+DFlash2 懒分配 scratch 与 FFN 逐层溢出 ==\n");
    const llama_runtime::RuntimePlan split =
        llama_runtime::PlanRuntime(MakeInputs(16384, kGpuFree32G, kMaxAlloc));
    CheckEq("Qwen3.8-27B ub512 GDN scratch = 72 MiB", split.ledger.lazy_scratch, 72 * kMiB);
    const llama_runtime::MemoryLedger without_scratch = [&]() {
        llama_runtime::MemoryLedger ledger = split.ledger;
        ledger.lazy_scratch = 0;
        return ledger;
    }();
    CheckEq("gpu_required 纳入 lazy_scratch",
            split.ledger.gpu_required() - without_scratch.gpu_required(), split.ledger.lazy_scratch);
    CheckEq("gpu_required_min 纳入 lazy_scratch",
            split.ledger.gpu_required_min() - without_scratch.gpu_required_min(), split.ledger.lazy_scratch);
    CheckTrue("32G 从默认 30 层升到满足 headroom 的层数",
              split.ffn_cpu_layers > llama_runtime::kFfnCpuLayersDefault && split.ffn_cpu_escalated);
    CheckTrue("32G 升档后 required_min + split headroom <= gpu_free",
              split.ledger.gpu_required_min() + llama_runtime::kSplitHeadroomBytes <= kGpuFree32G);
    CheckTrue("rationale 记录 scratch 与升档来源",
              split.rationale.find("lazy_scratch=72MiB") != std::string::npos &&
                  split.rationale.find("escalated from 30") != std::string::npos);

    llama_runtime::PlanInputs explicit_layers = MakeInputs(16384, kGpuFree32G, kMaxAlloc);
    explicit_layers.explicit_ffn_cpu_layers = 30;
    const llama_runtime::RuntimePlan explicit_plan = llama_runtime::PlanRuntime(explicit_layers);
    CheckEq("显式 ffn_cpu_layers 不升档", explicit_plan.ffn_cpu_layers, 30);
    CheckTrue("显式 ffn_cpu_layers 不标记 escalated", !explicit_plan.ffn_cpu_escalated);

    llama_runtime::ModelShape no_recurrent = TargetShape();
    no_recurrent.n_layer_recr = 0;
    const llama_runtime::MemoryLedger no_recurrent_ledger = llama_runtime::BuildLedger(
        no_recurrent, DraftShape(), 16384, 512, 1, 7, false, kMaxAlloc, 30, true);
    CheckEq("无 recurrent/GDN 形状的 lazy_scratch = 0", no_recurrent_ledger.lazy_scratch, 0);

    // 真机 GGUF 的 `dflash.block_count` 实测是 5（不是 6）：门控不能写死 draft 层数。
    llama_runtime::PlanInputs real_draft = MakeInputs(16384, kGpuFree32G, kMaxAlloc);
    real_draft.dft.n_layer_all = 5;
    llama_runtime::DeriveLayerSplit(real_draft.dft);
    const llama_runtime::RuntimePlan real_draft_plan = llama_runtime::PlanRuntime(real_draft);
    CheckEq("5 层 draft（真机实测形状）同样计入 lazy_scratch", real_draft_plan.ledger.lazy_scratch,
            72 * kMiB);
    CheckTrue("5 层 draft 同样触发升档", real_draft_plan.ffn_cpu_escalated);

    llama_runtime::PlanInputs other_speculative = MakeInputs(16384, kGpuFree32G, kMaxAlloc);
    other_speculative.enable_qwen38_dflash_policy = false;
    const llama_runtime::RuntimePlan other_plan = llama_runtime::PlanRuntime(other_speculative);
    CheckEq("非 DFlash speculative 形状不计入 lazy_scratch", other_plan.ledger.lazy_scratch, 0);
    CheckEq("非 DFlash speculative 不自动增加 CPU FFN 层数", other_plan.ffn_cpu_layers, 30);
    CheckTrue("非 DFlash speculative 不标记升档", !other_plan.ffn_cpu_escalated);
}

void TestExplicitWins()
{
    std::printf("\n== 显式配置永远胜出，且 ledger 按实际生效值重算 ==\n");
    llama_runtime::PlanInputs in = MakeInputs(49152, kGpuFree48G, kMaxAlloc);
    in.explicit_fa_target = 0;   // 48K 手工关 FA：规划器不覆盖，但必须告警
    const llama_runtime::RuntimePlan plan = llama_runtime::PlanRuntime(in);
    CheckTrue("显式 fa=off 未被覆盖", !llama_runtime::FlashAttnEnabled(plan.fa_target));
    CheckTrue("标记为 explicit", llama_runtime::FlashAttnExplicit(plan.fa_target));
    CheckTrue("rationale 含超限告警", plan.rationale.find("WARNING") != std::string::npos);
    // kq 超限 => 该张量落 CPU、不占 GPU => ledger 的 compute_kq 必须是 0，否则账本与实际不符。
    CheckEq("超限时 compute_kq 不计入 GPU 账本", plan.ledger.compute_kq, 0);

    llama_runtime::PlanInputs in2 = MakeInputs(16384, kGpuFree32G, kMaxAlloc);
    in2.explicit_fa_target = 1;
    in2.explicit_n_max = 4;
    const llama_runtime::RuntimePlan plan2 = llama_runtime::PlanRuntime(in2);
    CheckTrue("显式 fa=on 在 16K 也不被改回 off",
              llama_runtime::FlashAttnEnabled(plan2.fa_target));
    CheckEq("显式 n_max 生效", plan2.spec_draft_n_max, 4);
    CheckEq("ledger 的 recurrent 按生效的 n_max=4 重算", plan2.ledger.recurrent_tgt,
            llama_runtime::RecurrentStateBytesPerCopy(TargetShape()) * 5);
    CheckEq("FA on => compute_kq 为 0", plan2.ledger.compute_kq, 0);
}

void TestMaxAllocFallback()
{
    std::printf("\n== 设备上限查询失败的降级路径（A6） ==\n");
    struct Tier
    {
        uint32_t ctx;
        uint64_t gpu_free;
        bool expect_fa;
    };
    const Tier tiers[] = {{16384, kGpuFree32G, false}, {32768, kGpuFree48G, false},
                          {49152, kGpuFree48G, true}};
    for (const Tier &t: tiers)
    {
        const llama_runtime::RuntimePlan ref =
            llama_runtime::PlanRuntime(MakeInputs(t.ctx, t.gpu_free, kMaxAlloc));
        const llama_runtime::RuntimePlan fb =
            llama_runtime::PlanRuntime(MakeInputs(t.ctx, t.gpu_free, 0));
        char label[160];
        std::snprintf(label, sizeof(label), "ctx %u：fallback 与 device 的判定一致", t.ctx);
        CheckTrue(label, llama_runtime::FlashAttnEnabled(fb.fa_target) ==
                                 llama_runtime::FlashAttnEnabled(ref.fa_target) &&
                             llama_runtime::FlashAttnEnabled(fb.fa_target) == t.expect_fa &&
                             fb.full_gpu == ref.full_gpu);
    }
    const llama_runtime::RuntimePlan fb =
        llama_runtime::PlanRuntime(MakeInputs(16384, kGpuFree32G, 0));
    CheckEq("fallback 值 = 2048 MiB", fb.max_alloc, 2048 * kMiB);
    CheckTrue("来源标注为 fallback",
              fb.max_alloc_source == llama_runtime::RuntimePlan::MaxAllocSource::kFallback);
    CheckTrue("rationale 里写明来源", fb.rationale.find("(fallback)") != std::string::npos);

    llama_runtime::PlanInputs in = MakeInputs(49152, kGpuFree48G, kMaxAlloc);
    in.max_alloc_explicit_mib = 4096;
    const llama_runtime::RuntimePlan ex = llama_runtime::PlanRuntime(in);
    CheckEq("显式 max_alloc_mib 生效", ex.max_alloc, 4096 * kMiB);
    CheckTrue("上限抬高后 48K 的 kq 不再超限 => 自动关 FA",
              !llama_runtime::FlashAttnEnabled(ex.fa_target));
    CheckTrue("来源标注为 explicit",
              ex.max_alloc_source == llama_runtime::RuntimePlan::MaxAllocSource::kExplicit);
}

void TestHostCheckpoints()
{
    std::printf("\n== head_checkpoints 由 host 内存推导（D5），不由显存推导 ==\n");
    const uint64_t ckpt = llama_runtime::RecurrentStateBytesPerCopy(TargetShape());
    std::printf("  -- 单个检查点 = %.2f MiB（= 一份 recurrent 状态）\n",
                (double) ckpt / (double) kMiB);

    // 32G 机的 FreePhysicalMemory 常只剩约 2.5 GiB。
    llama_runtime::PlanInputs tight = MakeInputs(16384, kGpuFree32G, kMaxAlloc);
    tight.host_avail = (uint64_t) (2.5 * (double) kGiB);
    const llama_runtime::RuntimePlan p_tight = llama_runtime::PlanRuntime(tight);
    std::printf("  -- host_avail=2.5 GiB => head_checkpoints=%d, host 常驻=%.0f MiB\n",
                p_tight.head_checkpoints,
                (double) p_tight.ledger.host_checkpoints / (double) kMiB);
    CheckTrue("32G / host 紧张 => head_checkpoints ∈ {0, 1}",
              p_tight.head_checkpoints == 0 || p_tight.head_checkpoints == 1);

    llama_runtime::PlanInputs roomy = MakeInputs(49152, kGpuFree48G, kMaxAlloc);
    roomy.host_avail = 20ULL * kGiB;
    const llama_runtime::RuntimePlan p_roomy = llama_runtime::PlanRuntime(roomy);
    CheckEq("48G / host 宽裕 => head_checkpoints=2", (uint64_t) p_roomy.head_checkpoints, 2);
    CheckEq("host 常驻 = (N + 3) × ckpt_bytes", p_roomy.ledger.host_checkpoints, 5 * ckpt);
    // 检查点是 host 常驻 + GPU 瞬时峰值，不是「每个检查点占 199 MiB GPU 显存」（修正 r26 的记载）。
    CheckEq("GPU 侧只承担一份读回峰值", p_roomy.ledger.runtime_peak, ckpt);

    llama_runtime::PlanInputs forced = MakeInputs(49152, kGpuFree48G, kMaxAlloc);
    forced.host_avail = (uint64_t) (2.5 * (double) kGiB);
    forced.explicit_head_checkpoints = 2;
    const llama_runtime::RuntimePlan p_forced = llama_runtime::PlanRuntime(forced);
    CheckEq("显式 head_checkpoints 不被 host 规则推翻",
            (uint64_t) p_forced.head_checkpoints, 2);
}

void TestContextClamp()
{
    std::printf("\n== ctx 可行上限钳制（硬下界判据，不是保守值） ==\n");
    // 已实测可用的三档都不许被钳制 —— 这是本项最重要的回归面。
    const struct
    {
        const char *name;
        uint32_t ctx;
        uint64_t gpu_free;
    } keep[] = {{"32G / 16K", 16384, kGpuFree32G},
                {"48G / 32K", 32768, kGpuFree48G},
                {"48G / 48K", 49152, kGpuFree48G},
                {"64G / 64K", 65536, kGpuFree64G}};
    for (const auto &k: keep)
    {
        const llama_runtime::RuntimePlan plan =
            llama_runtime::PlanRuntime(MakeInputs(k.ctx, k.gpu_free, kMaxAlloc));
        char label[160];
        std::snprintf(label, sizeof(label), "%s 不被钳制（ctx 保持 %u）", k.name, k.ctx);
        CheckTrue(label, !plan.ctx_clamped && plan.ctx_effective == k.ctx);
        std::snprintf(label, sizeof(label), "%s 不触发 n_max 兜底下调", k.name);
        CheckTrue(label, !plan.n_max_reduced && plan.spec_draft_n_max == 7);
    }

    // 32G 请求 48K：必须被钳制到可行值并告警，而不是静默等运行期 OOM。
    const llama_runtime::RuntimePlan over =
        llama_runtime::PlanRuntime(MakeInputs(49152, kGpuFree32G, kMaxAlloc));
    std::printf("  -- 32G 请求 48K => ctx_effective=%u, required_min=%.2f GiB, free=%.2f GiB\n",
                over.ctx_effective, (double) over.ledger.gpu_required_min() / (double) kGiB,
                (double) kGpuFree32G / (double) kGiB);
    CheckTrue("32G 请求 48K 被钳制", over.ctx_clamped && over.ctx_effective < 49152);
    CheckTrue("钳制后仍满足硬下界",
              over.ledger.gpu_required_min() <= kGpuFree32G);
    CheckTrue("rationale 写明钳制原因",
              over.rationale.find("context clamped") != std::string::npos);
    CheckTrue("钳制不等于 full_gpu（32G 仍走拆分）", !over.full_gpu);

    // 显存极端不足：钳到下限仍不行时才动 n_max，且必须写明原因。
    llama_runtime::PlanInputs starved = MakeInputs(16384, 1, kMaxAlloc);
    const llama_runtime::RuntimePlan p_starved = llama_runtime::PlanRuntime(starved);
    std::printf("  -- free=1 byte => ctx=%u n_max=%u clamped=%d n_max_reduced=%d\n",
                p_starved.ctx_effective, p_starved.spec_draft_n_max, p_starved.ctx_clamped ? 1 : 0,
                p_starved.n_max_reduced ? 1 : 0);
    CheckTrue("显存极端不足时先升档到上限，再降 ctx、再降 n_max",
              p_starved.ffn_cpu_layers == llama_runtime::kFfnCpuLayersEscalationMax &&
                  p_starved.ctx_effective == 8192 && p_starved.n_max_reduced &&
                  p_starved.spec_draft_n_max >= 3);
    CheckTrue("n_max 下调有日志依据",
              p_starved.rationale.find("spec_draft_n_max reduced") != std::string::npos);

    llama_runtime::PlanInputs starved_explicit = starved;
    starved_explicit.explicit_n_max = 7;
    const llama_runtime::RuntimePlan p_explicit = llama_runtime::PlanRuntime(starved_explicit);
    CheckTrue("显式 n_max 永不被兜底下调",
              !p_explicit.n_max_reduced && p_explicit.spec_draft_n_max == 7);
}

void TestSplitPlacementLedger()
{
    std::printf("\n== 拆分放置的账本：GPU 侧必须扣掉 CPU 常驻权重 ==\n");
    const llama_runtime::ModelShape tgt = TargetShape();
    const llama_runtime::RuntimePlan split =
        llama_runtime::PlanRuntime(MakeInputs(16384, kGpuFree32G, kMaxAlloc));
    const llama_runtime::RuntimePlan full =
        llama_runtime::PlanRuntime(MakeInputs(32768, kGpuFree48G, kMaxAlloc));

    CheckTrue("32G 走拆分", !split.full_gpu);
    CheckTrue("48G 走全 GPU", full.full_gpu);
    // 全 GPU 档也要扣 token embedding：它始终在 CPU（日志里那 682 MiB 的 CPU model buffer）。
    CheckEq("全 GPU 档 weights_host = embedding", full.ledger.weights_host, tgt.embedding_bytes);
    uint64_t escalated_ffn_bytes = 0;
    const size_t first_cpu_layer = tgt.ffn_bytes_per_layer.size() - split.ffn_cpu_layers;
    for (size_t i = first_cpu_layer; i < tgt.ffn_bytes_per_layer.size(); ++i)
    {
        escalated_ffn_bytes += tgt.ffn_bytes_per_layer[i];
    }
    CheckEq("拆分档 weights_host = embedding + 升档后的 FFN",
            split.ledger.weights_host, tgt.embedding_bytes + escalated_ffn_bytes);
    CheckTrue("升档后 weights_host 高于 30 层基线",
              split.ledger.weights_host > tgt.embedding_bytes + 30 * tgt.ffn_bytes_per_layer.back());
}

} // namespace

int main()
{
    std::printf("=== llama_runtime_planner 离线单元测试 ===\n");
    TestLayerSplit();
    TestAttentionKv();
    TestRecurrent();
    TestComputeKq();
    TestFlashAttnCriterion();
    TestMeasuredTiers();
    TestLazyScratchAndEscalation();
    TestExplicitWins();
    TestMaxAllocFallback();
    TestHostCheckpoints();
    TestContextClamp();
    TestSplitPlacementLedger();

    std::printf("\n=== %d / %d 通过，%d 失败 ===\n", g_total - g_failed, g_total, g_failed);
    return g_failed == 0 ? 0 : 1;
}
