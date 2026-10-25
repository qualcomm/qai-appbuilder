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

// GGUF 投机解码路径的运行参数规划器：由「GPU 可见显存实测值 + 主机可用内存 + GGUF 元数据 +
// 目标上下文」连续推导出整套运行参数，并产出逐项可对账的内存账本。
//
// 存在的理由是「单一真相源」：在此之前 GGUFVerify 的加载门槛（SpeculativeFitsFullGpu）与
// LLAMACppBuilder 的 argv 拼装各算一遍显存，口径已经漂移，且两边都漏了 recurrent 状态、
// 漏了随 -fa 开关变化的 compute buffer。本模块被设计为纯函数 + 一次设备查询，形状读取与
// 账本计算不依赖 Windows API / 真实硬件，便于脱离 MSVC/ARM64 环境离线单元验证
// （见 test/test_runtime_planner.cpp）。
namespace llama_runtime
{

// 全部来自 GGUF 文件头的 KV 元数据（no_alloc 解析，不加载任何张量数据）。
struct ModelShape
{
    bool ok = false;
    uint64_t weight_bytes = 0;        // GGUF 文件字节数（权重常驻量的代理值）
    // 以下两项按张量粒度统计，用于把「哪些权重并不在 GPU 上」从账本里扣除：
    //   - token embedding **始终**留在 CPU（即使 -ngl 99 全 GPU，日志里那 682 MiB 的
    //     `CPU model buffer` 就是它）；
    //   - 拆分加载时内置 -ot 把最后 N 层的 ffn_(up|gate|down) 挂到 CPU（日志里的
    //     `CPU_KLEIDIAI model buffer`）。
    // 不扣这两项会把 GPU 账本算得偏大，32G 拆分档的可行 ctx 上限会被低估。
    uint64_t embedding_bytes = 0;                 // token_embd.weight
    std::vector<uint64_t> ffn_bytes_per_layer;    // 按 block 编号的 ffn_(up|gate|down) 字节数

    uint32_t n_layer_all = 0;         // block_count 原值
    uint32_t n_layer_nextn = 0;       // nextn_predict_layers；不参与 KV / recurrent 分配
    uint32_t n_layer_attn = 0;        // 带 attention 的层数（见 LayerSplit 推导）
    uint32_t n_layer_recr = 0;        // recurrent（SSM）层数

    uint32_t n_head = 0;              // attention.head_count   —— FA 判据必需
    uint32_t n_head_kv = 0;           // attention.head_count_kv
    uint32_t key_length = 0;          // attention.key_length
    uint32_t n_embd = 0;              // embedding_length
    uint32_t n_swa = 0;               // attention.sliding_window
    uint32_t full_attention_interval = 0;

    // State Space Model（recurrent）形状
    uint32_t ssm_d_conv = 0;          // ssm.conv_kernel
    uint32_t ssm_d_inner = 0;         // ssm.inner_size
    uint32_t ssm_d_state = 0;         // ssm.state_size
    uint32_t ssm_n_group = 0;         // ssm.group_count

    // 以下四项只为复刻 llama-hparams.cpp 的 n_embd_r()/n_embd_s() 的非 Mamba 分支
    uint32_t wkv_head_size = 0;       // RWKV
    uint32_t token_shift_count = 2;   // RWKV
    uint32_t n_shortconv_l_cache = 0; // LFM2
    uint32_t n_embd_head_kda = 0;     // Kimi KDA
    uint32_t n_embd_head_la = 0;      // MiniMax-Text-01 linear attention

    // 参与 KV / recurrent 分配的层数 = block_count - nextn_predict_layers。
    uint32_t n_layer() const;

    // 逐字复刻 llama_hparams::n_embd_r()（llama-hparams.cpp:208-234）的分支顺序。
    uint64_t n_embd_r() const;
    // 逐字复刻 llama_hparams::n_embd_s()（llama-hparams.cpp:236-258）的分支顺序。
    uint64_t n_embd_s() const;
};

// 显存/内存账本，逐项保存以便启动日志逐项打印——上一次 OOM（r26）之所以难定位，正是因为
// 旧代码只打印一个汇总的 required 数字。
struct MemoryLedger
{
    uint64_t weights_tgt = 0;     // weights_* 两项是 **GPU 常驻** 部分（已扣 embedding 与拆到 CPU 的 FFN）
    uint64_t weights_dft = 0;
    uint64_t weights_host = 0;    // 回到 CPU 上的权重（embedding + 内置 -ot 的 FFN），不计入 GPU
    uint64_t kv_attn_tgt = 0;
    uint64_t kv_attn_dft = 0;
    uint64_t recurrent_tgt = 0;   // (1 + n_rs_seq) 份
    uint64_t recurrent_dft = 0;
    uint64_t compute_const = 0;   // 经验上界，见 kComputeConstBytes
    uint64_t compute_kq = 0;      // 非 FA 的注意力矩阵在 **GPU 侧的实际增量**，详见 BuildLedger
    uint64_t runtime_peak = 0;    // 检查点读回瞬时峰值 ≈ 一份 recurrent 状态
    uint64_t safety = 0;

    uint64_t host_checkpoints = 0; // host 侧常驻：(N + 3) × ckpt_bytes，不计入 GPU
    uint64_t ckpt_bytes = 0;       // 单个检查点字节数（target + draft 两侧之和）

    // 保守判据：用于「要不要走全 GPU」。多算了 compute_kq / runtime_peak / safety，
    // 算得偏大的代价只是退回拆分加载（安全降级）。
    uint64_t gpu_required() const;

    // 硬下界：这几项是**无论如何都要在 GPU 上落实的分配**（GPU 常驻权重 + 两侧 attention KV
    // + recurrent 状态 + 两个模型各一份 compute buffer + 检查点读回的瞬时峰值）。它装不下就一定
    // 加载不了，所以用它做 ctx 可行上限的钳制判据。**不能改用 gpu_required()**：那个带 1 GiB
    // safety 的保守值会把 32G/16K 这个已实测可用的拆分档判成不可行（实测 GPU 侧总占用约
    // 13.9 GiB / 可用 14.72 GiB），从而把用户的上下文白白调小。
    // ⚠️ 包含 runtime_peak 是有实测依据的：r26 那次 28 层档比 30 层档多占约 277 MiB GPU，就在
    // 第 5 个请求存检查点时 clCreateBuffer err=-5 —— 存检查点是长请求的必经路径，不是可选峰值。
    uint64_t gpu_required_min() const;
};

// 单个 -fa 开关的三态：配置未给出时由账本推导。
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

    uint64_t gpu_free = 0;        // ggml_backend_dev_memory 报告的可用显存
    uint64_t host_avail = 0;      // GlobalMemoryStatusEx 的 ullAvailPhys
    uint32_t ctx_requested = 0;

    // 设备单次分配上限。两个来源分开传，便于日志标注 device / explicit / fallback。
    uint64_t max_alloc_device = 0;      // QueryDeviceMaxAllocBytes() 的返回值；0 = 查询失败
    int max_alloc_explicit_mib = -1;    // draft_model.max_alloc_mib；<0 = 未配置

    uint32_t n_seq_max = 1;       // 投机解码路径恒 1（params.n_parallel = 1、kv_unified = true）

    // 显式配置（来自 config.json 的 draft_model 块）。显式给出的永远胜出，规划器只填未给出的项。
    int explicit_fa_target = -1;      // <0 未给出；0 off；1 on
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
    uint32_t ffn_cpu_layers = 0;      // 非全 GPU 时恒 kFfnCpuLayersDefault，禁止按显存反算（r26）
    FlashAttnChoice fa_target = FlashAttnChoice::kDerivedOff;
    FlashAttnChoice fa_draft = FlashAttnChoice::kDerivedOff;
    uint32_t n_batch = 0;
    uint32_t n_ubatch = 0;
    uint32_t spec_draft_n_max = 0;
    int head_checkpoints = 0;
    uint32_t ctx_effective = 0;       // 按可行上限钳制后的值
    bool ctx_clamped = false;
    bool n_max_reduced = false;

    // 实际生效的设备单次分配上限及其来源，日志必须标注（取不到真值时失败模式是
    // 「该开 FA 却没开」=> 变慢但不崩，且 splits 与 CPU compute buffer 会在日志里暴露）。
    uint64_t max_alloc = 0;
    enum class MaxAllocSource
    {
        kDevice = 0,
        kExplicit,
        kFallback,
    } max_alloc_source = MaxAllocSource::kFallback;

    uint64_t kq_bytes = 0;            // FA 判据的左侧：单个注意力矩阵字节数
    uint64_t gpu_free_observed = 0;   // 做决定时实测到的可用显存（原样回填，供日志与回归断言用）
    uint64_t host_avail_observed = 0; // 同上，主机可用内存

    MemoryLedger ledger;
    std::string rationale;            // 每项「值 + 来源（explicit/derived）+ 判据」
};

// ---- 账本公式（每条都有源码或实测锚点，见 llama_cpp.cpp.notes.md） ----

// attention KV：K+V 两份 × n_layer_attn × n_ctx × n_head_kv × key_length × sizeof(f16)。
// 锚点：16 层 / 32K / f16 → 2048 MiB，与启动日志逐字吻合。
uint64_t AttentionKvCacheBytes(const ModelShape &shape, uint64_t n_ctx);

// recurrent 状态：n_layer_recr × (n_embd_r + n_embd_s) × 4 B × n_seq_max × (1 + n_rs_seq)。
// 锚点：llama-memory-recurrent.cpp:101 `const uint32_t n_rows = mem_size * (1 + n_rs_seq);`；
// 48 层 / n_rs_seq=7 → 1,255,145,472 B（1197 MiB），单份 156,893,184 B 与检查点 tgt_bytes 吻合。
uint64_t RecurrentStateBytes(const ModelShape &shape, uint32_t n_seq_max, uint32_t n_rs_seq);

// 单份 recurrent 状态（= 一个前缀检查点的 target 侧字节数）。
uint64_t RecurrentStateBytesPerCopy(const ModelShape &shape);

// 非 FA 路径的完整注意力矩阵 kq：f32 [n_kv, n_tokens, n_head]，reserve 时
// n_tokens = min(n_ctx, n_ubatch)（llama-context.cpp 的最坏情况图）。
// 锚点：512 × 49152 × 24 × 4 B = 2304 MiB，超过 Adreno 单次分配上限 2048 MiB。
uint64_t ComputeKqBytes(const ModelShape &shape, uint64_t n_ctx, uint32_t n_ubatch);

// 使 `compute_kq ≤ max_alloc` 成立的最大 n_ctx（即「还能关 FA」的上限）。
// 本机（n_head=24、max_alloc=2048 MiB）：ub 512 → 43,690；ub 256 → 87,381；ub 1024 → 21,845。
uint64_t CriticalContextForFlashAttn(const ModelShape &shape, uint32_t n_ubatch, uint64_t max_alloc);

// FA 开启时 compute buffer 近似与 n_ctx 无关：实测主模型 16K 541 MiB → 48K 603 MiB。
// ⚠️ 这是经验拟合的上界，不是源码推导；其构成未完全查清（ggml-alloc 存在张量复用，
// 16K 的 FA on/off 两档实测只差 339 MiB 而非公式的 768 MiB）。
constexpr uint64_t kComputeConstBytes = 650ULL * 1024 * 1024;

// 草稿模型自己那份 compute buffer。**必须单独计**：32G/16K 真机实测两个模型的 OpenCL
// compute buffer 是主 571 + 草稿 538 = 约 1109 MiB，已经**超过**上面那个单模型上界。
// 只计一份是建模对象搞错（不是外推不足），会让 ctx 钳制放过实际装不下的档位。
constexpr uint64_t kComputeConstDraftBytes = 550ULL * 1024 * 1024;

// 安全余量恒 ≥ 1 GiB：r26 实测省 287 MiB 就会在存检查点时 clCreateBuffer err=-5。
constexpr uint64_t kSafetyBytes = 1024ULL * 1024 * 1024;

// 设备单次分配上限查询失败时的保守常量（本机实测值）。失败模式是「该开 FA 却没开」=> 变慢但不崩。
constexpr uint64_t kMaxAllocFallbackBytes = 2048ULL * 1024 * 1024;

constexpr uint32_t kFfnCpuLayersDefault = 30;
constexpr uint32_t kNBatchDefault = 2048;
constexpr uint32_t kNUbatchDefault = 512;
constexpr uint32_t kNMaxDefault = 7;
constexpr uint32_t kNMaxFloor = 3;
constexpr uint32_t kCtxStep = 4096;
constexpr uint32_t kCtxFloor = 8192;
constexpr int kHeadCheckpointsMax = 2;

// 前缀检查点最多占用多少主机可用内存。计划里写的是 0.4，但它自己的验收数字
// （主机只剩约 2.5 GiB 时应得 0–1 份）在 0.4 下算不出来。取 0.1 的理由：r23 实测
// `head_checkpoints=2` 的 prefill 收益测不出（1310 vs 1311 s），即降档是**零代价**的，
// 所以没必要为一个测不出收益的缓存花掉四成的主机余量。
constexpr double kHostCheckpointBudgetFraction = 0.1;

// ---- 形状读取与设备查询 ----

// 只读 GGUF 文件头的 KV 元数据。未编译 USE_GGUF 时恒返回 ok=false 的默认实例。
// 注意：weight_bytes 由调用方按 GGUF 文件字节数填入，本函数不碰文件系统以外的信息。
ModelShape ReadGgufModelShape(const std::string &gguf_path);

// 由 full_attention_interval 与 SSM 形状推导 n_layer_attn / n_layer_recr。
// 单独暴露是为了让离线单元测试能直接喂手工构造的形状，不依赖真实 GGUF 文件。
void DeriveLayerSplit(ModelShape &shape);

// 设备单次分配上限（CL_DEVICE_MAX_MEM_ALLOC_SIZE）。失败返回 0。
// 不要改用 ggml_backend_buft_get_max_size()：OpenCL 后端未实现 get_max_size 回调，它返回 SIZE_MAX。
uint64_t QueryDeviceMaxAllocBytes();

// ---- 规划 ----

// 按**实际生效**的 fa_target 构建账本。显式配置胜出时也必须用生效值重算，否则日志里的
// ledger 会与真实 argv 不一致，反而误导排查。
// max_alloc 是必需的：它决定那个 kq 到底落在 GPU 还是 CPU，两者对 GPU 账本的贡献完全不同。
// ffn_cpu_layers：拆分加载时被内置 -ot 挂到 CPU 的后段 FFN 层数；0 = 全 GPU。
MemoryLedger BuildLedger(const ModelShape &tgt, const ModelShape &dft, uint32_t n_ctx,
                         uint32_t n_ubatch, uint32_t n_seq_max, uint32_t n_max, bool fa_target,
                         uint64_t max_alloc, uint32_t ffn_cpu_layers);

RuntimePlan PlanRuntime(const PlanInputs &in);

} // namespace llama_runtime

#endif //LLAMA_RUNTIME_PLANNER_H
