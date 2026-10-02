//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_cpp.h"
#include <llama.h>
#include <arg.h>
#include <common.h>
#include <ggml-backend.h>
#include <sampling.h>
#include <speculative.h>
#include "log.h"
#include "utils.h"
#include "llama_speculative_config.h"
#include "watermark_provider_host.h"
#include <filesystem>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <cctype>
#include <thread>
#include <chrono>

namespace fs = std::filesystem;

// ─────────────────────────────────────────────────────────────────────────────
// 返回自 epoch 以来的毫秒数（用于 prefill/生成速率等性能日志的时间戳计算）
// ─────────────────────────────────────────────────────────────────────────────
static inline int64_t now_ms()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ─────────────────────────────────────────────────────────────────────────────
// Watermark sampling-chain adapter
// Implements llama_sampler_i and forwards to the plugin VTable token hooks.
// Inserted at the absolute front of the chain once during Impl construction.
// ─────────────────────────────────────────────────────────────────────────────
namespace {

struct WatermarkSamplerData {
    const GenieWatermarkProviderVTable* vtable;
    GenieWatermarkTokenHook*             hook;
    uint32_t                             n_vocab; // stored for clone()
    uint64_t                             seed;    // stored for clone()
};

static const char* wm_name(const llama_sampler * /*smpl*/)
{
    return "genie_watermark";
}

static void wm_accept(llama_sampler * smpl, llama_token token)
{
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    d->vtable->token_hook_accept(d->hook, static_cast<int32_t>(token));
}

static void wm_apply(llama_sampler * smpl, llama_token_data_array * cur_p)
{
    if (cur_p->size == 0) { return; }
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    const size_t n = cur_p->size;
    std::vector<int32_t> ids(n);
    std::vector<float>   logits(n);
    for (size_t i = 0; i < n; ++i) {
        ids[i]    = static_cast<int32_t>(cur_p->data[i].id);
        logits[i] = cur_p->data[i].logit;
    }
    d->vtable->token_hook_apply(d->hook, logits.data(), ids.data(), static_cast<int32_t>(n));
    for (size_t i = 0; i < n; ++i) {
        cur_p->data[i].logit = logits[i];
    }
    cur_p->sorted = false;
}

static llama_sampler * wm_clone(const llama_sampler * smpl);
static void wm_reset(llama_sampler * smpl);
static void wm_free(llama_sampler * smpl);

static llama_sampler_i kWatermarkSamplerIface = {
    wm_name,   // name
    wm_accept, // accept
    wm_apply,  // apply
    wm_reset,  // reset
    wm_clone,  // clone
    wm_free,   // free
    nullptr,   // backend_init
    nullptr,   // backend_accept
    nullptr,   // backend_apply
    nullptr,   // backend_set_input
};

static llama_sampler * wm_clone(const llama_sampler * smpl)
{
    // Speculative-decoding: create a fresh hook from stored n_vocab/seed rather
    // than copying live watermark state, so each "what-if" branch starts clean.
    const auto* d = static_cast<const WatermarkSamplerData*>(smpl->ctx);
    auto* new_data = new WatermarkSamplerData{
        d->vtable,
        d->vtable->token_hook_create(d->n_vocab, d->seed),
        d->n_vocab,
        d->seed
    };
    return llama_sampler_init(&kWatermarkSamplerIface, static_cast<llama_sampler_context_t>(new_data));
}

static void wm_reset(llama_sampler * smpl)
{
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    d->vtable->token_hook_reset(d->hook);
}

static void wm_free(llama_sampler * smpl)
{
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    if (d) {
        d->vtable->token_hook_free(d->hook);
        delete d;
    }
}

static llama_sampler* create_watermark_sampler(const GenieWatermarkProviderVTable* vtable,
                                               GenieWatermarkTokenHook*             hook,
                                               uint32_t                             n_vocab,
                                               uint64_t                             seed)
{
    auto* d = new WatermarkSamplerData{vtable, hook, n_vocab, seed};
    return llama_sampler_init(&kWatermarkSamplerIface, static_cast<llama_sampler_context_t>(d));
}

} // namespace

class LLAMACppBuilder::Impl
{
public:
    explicit Impl(common_params &&params) : params_{std::move(params)}
    {
        params_.warmup = false;

        if (params_.embedding)
        {
            throw std::runtime_error("embedding is not support yet");
        }

        if (params_.n_ctx != 0 && params_.n_ctx < 8)
        {
            params_.n_ctx = 4096;
        }

        common_init();

        RegisterLogAdapter();

        llama_backend_init();
        llama_numa_init(params_.numa);

        llama_init = common_init_from_params(params_);
        llama_model *model = llama_init->model();
        llama_context *ctx = llama_init->context();

        if (model == nullptr)
        {
            throw std::runtime_error("unable to load model");
        }

        vocab = llama_model_get_vocab(model);

        smpl = llama_init->sampler(0);
        if (!smpl)
        {
            throw std::runtime_error("failed to initialize sampling subsystem");
        }

        // Insert watermark adapter at the absolute front of the sampling chain (once, at construction).
        // When GENIE_WATERMARK_ENABLE is unset / plugin absent, HasTokenHook() is false → zero overhead.
        if (WatermarkProviderHost::Instance().HasTokenHook())
        {
            const GenieWatermarkProviderVTable* vt = WatermarkProviderHost::Instance().GetVTable();
            const uint32_t n_vocab = static_cast<uint32_t>(llama_vocab_n_tokens(vocab));
            GenieWatermarkTokenHook* hook = WatermarkProviderHost::Instance().CreateTokenHook(n_vocab, /*seed=*/0);
            if (vt && hook)
            {
                llama_sampler* wm_smp = create_watermark_sampler(vt, hook, n_vocab, /*seed=*/0);
                llama_sampler* chain  = common_sampler_get(smpl);
                std::vector<llama_sampler*> saved;
                while (llama_sampler_chain_n(chain) > 0)
                {
                    saved.push_back(llama_sampler_chain_remove(chain, 0));
                }
                llama_sampler_chain_add(chain, wm_smp);
                for (llama_sampler* s : saved)
                {
                    llama_sampler_chain_add(chain, s);
                }
                My_Log{My_Log::Level::kInfo} << "[LLAMACpp] Watermark token hook inserted at front of sampling chain"
                         << " (n_vocab=" << n_vocab << ")\n";
            }
            else
            {
                My_Log{My_Log::Level::kWarning}
                    << "[LLAMACpp] Watermark token hook creation failed; running without watermark\n";
            }
        }

        // 线程池创建与绑定，对齐 completion.cpp:163-202
        auto *cpu_dev = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_CPU);
        if (!cpu_dev)
        {
            throw std::runtime_error("no CPU backend found");
        }
        auto *reg = ggml_backend_dev_backend_reg(cpu_dev);
        ggml_threadpool_new_fn = (decltype(ggml_threadpool_new) *) ggml_backend_reg_get_proc_address(reg, "ggml_threadpool_new");
        ggml_threadpool_free_fn = (decltype(ggml_threadpool_free) *) ggml_backend_reg_get_proc_address(reg, "ggml_threadpool_free");

        struct ggml_threadpool_params tpp_batch =
                ggml_threadpool_params_from_cpu_params(params_.cpuparams_batch);
        struct ggml_threadpool_params tpp =
                ggml_threadpool_params_from_cpu_params(params_.cpuparams);

        set_process_priority(params_.cpuparams.priority);

        if (!ggml_threadpool_params_match(&tpp, &tpp_batch))
        {
            threadpool_batch = ggml_threadpool_new_fn(&tpp_batch);
            if (!threadpool_batch)
            {
                throw std::runtime_error("threadpool create failed with tpp");
            }
            tpp.paused = true;
        }

        threadpool = ggml_threadpool_new_fn(&tpp);
        if (!threadpool)
        {
            throw std::runtime_error("threadpool create failed");
        }

        llama_attach_threadpool(ctx, threadpool, threadpool_batch);

        // 重置性能计数器，从这里开始测量（对齐 completion.cpp:161）
        llama_perf_context_reset(ctx);

        if (!llama_model_has_encoder(model))
        {
            GGML_ASSERT(!llama_vocab_get_add_eos(vocab));
        }

        // 仅当模型声明了 draft_model 字段（params_.speculative.draft.mparams.path 非空，由外层
        // LLAMACppBuilder 构造函数注入）时才激活投机解码；未声明的模型完全跳过本块。
        is_speculative_ = !params_.speculative.draft.mparams.path.empty();
        if (is_speculative_)
        {
            common_params params_dft = common_base_params_to_speculative(params_);
            draft_init_ = common_speculative_init_from_params(params_dft, model, ctx);
            if (!draft_init_ || !draft_init_->model() || !draft_init_->context())
            {
                throw std::runtime_error("unable to load draft model for speculative decoding");
            }

            params_.speculative.draft.ctx_tgt = ctx;
            params_.speculative.draft.ctx_dft = draft_init_->context();

            spec_.reset(common_speculative_init(params_.speculative, /*n_seq=*/1));
            if (!spec_)
            {
                throw std::runtime_error("failed to initialize speculative decoding state");
            }

            My_Log{} << "[LLAMACpp] Speculative decoding ENABLED: draft_model="
                     << params_.speculative.draft.mparams.path
                     << ", n_max=" << params_.speculative.draft.n_max
                     << ", p_min=" << params_.speculative.draft.p_min
                     << ", n_gpu_layers=" << params_.speculative.draft.n_gpu_layers << "\n";
        }
    }

    bool Query(const std::string &prompt,
               const std::function<bool(std::string &)> &callback,
               const std::function<bool()> &prefill_heartbeat,
               const char *query_type, bool is_aux_inference)
    {
        auto t_query_start = std::chrono::steady_clock::now();

        // 截取 prompt 前 100 字符作为标识（安全检查 prompt 以 "You are a security classifier" 开头）
        std::string prompt_prefix = prompt.substr(0, std::min(prompt.size(), size_t(100)));
        for (auto &c : prompt_prefix) if (c == '\n') c = ' ';

        My_Log{} << "[LLAMACpp] " << query_type << " Query START"
                 << " | prompt_chars=" << prompt.size()
                 << " | prompt_prefix=\"" << prompt_prefix << "\""
                 << " @" << My_Log::GetTimeString() << "\n";

        {
            std::lock_guard<std::mutex> lk(m);
            done = false;
        }

        // Fix: 重置输出限制状态
        stopped_by_output_limit_ = false;
        int n_generated = 0;
        bool should_stop = false;

        // GenieAPIService 特有：每次 Query() 调用都要重置状态，因为同一个 Impl 实例要
        // 串行处理多个独立请求（completion.cpp 是单进程单次运行，从不需要这个）。
        n_past = 0;
        n_consumed = 0;
        embd.clear();
        embd_inp.clear();

        common_sampler_reset(smpl);

        llama_context *ctx = llama_init->context();
        llama_memory_t mem = llama_get_memory(ctx);
        llama_memory_clear(mem, false);

        // Fix: 投机解码场景下 draft context 的 KV cache 此前从未在这里被清空，跨请求残留
        // 上一次请求末尾的位置（如 X=85），与本次全新请求从 0 开始的 token 位置冲突，触发
        // "inconsistent sequence positions"/llama_decode(ctx_dft) failed rc=-1（M-RoPE 要求
        // X <= Y）。target/draft 两个 context 必须在每次 Query 开头同步重置。
        if (is_speculative_ && params_.speculative.draft.ctx_dft)
        {
            llama_memory_clear(llama_get_memory(params_.speculative.draft.ctx_dft), false);
        }

        // 重置性能计数器，确保每次 Query 的统计数据是独立的
        llama_perf_context_reset(ctx);

        const int n_ctx = llama_n_ctx(ctx);

        // tokenize prompt，对齐 completion.cpp:318-320
        const bool add_bos = llama_vocab_get_add_bos(vocab) && !params_.use_jinja;
        embd_inp = common_tokenize(ctx, prompt, true, true);

        if (embd_inp.empty())
        {
            // 对齐 completion.cpp:330-339：空 prompt 时退化为仅一个 BOS token，否则报错。
            if (add_bos)
            {
                embd_inp.push_back(llama_vocab_bos(vocab));
            }
            else
            {
                My_Log{My_Log::Level::kError} << "[LLAMACpp] " << query_type << " input is empty\n";
                std::lock_guard<std::mutex> lk(m);
                done = true;
                cv.notify_one();
                return false;
            }
        }

        My_Log{} << "[LLAMACpp] " << query_type
                 << " Tokenization done: " << embd_inp.size() << " tokens (add_bos=" << add_bos << ")\n";

        // n_keep 是本次调用范围内的局部值，不写回 params_，对齐 completion.cpp:399-404
        int n_keep;
        if (params_.n_keep < 0 || params_.n_keep > (int) embd_inp.size())
        {
            n_keep = (int) embd_inp.size();
        }
        else
        {
            n_keep = params_.n_keep + (add_bos ? 1 : 0);
        }

        int n_remain = params_.n_predict;

        bool prefill_done = false;
        int64_t t_prefill_start_ms = 0;
        int64_t t_prefill_end_ms = 0;
        int total_prefill_tokens = 0;
        int total_decode_batches = 0;
        int64_t t_gen_start_ms = 0;
        std::string generated_text_buf;  // 累积生成文本（用于日志）
        // 投机解码可观测性：逐轮累计草稿 token 数与被接受 token 数，供 Query DONE 日志汇报
        // 命中率证据（对齐本次交付测试方案要求的“接受计数 > 0”验收标准）；单模型路径下恒为 0。
        int64_t n_draft_proposed_total = 0;
        int64_t n_draft_accepted_total = 0;

        // Prefill 阶段心跳计时：记录上次发送心跳的时间点
        // 每隔 kPrefillHeartbeatIntervalMs 毫秒向客户端发送一次保活消息，
        // 防止客户端或中间代理因 prefill 期间长时间无数据而超时断开连接。
        auto t_last_prefill_heartbeat = std::chrono::steady_clock::now();
        constexpr int64_t kPrefillHeartbeatIntervalMs = 5000; // 每 5 秒发一次心跳

        // common_token_to_piece() emits text one token at a time, and a multi-byte UTF-8
        // character can legitimately be split across two tokens. Buffer until the character is
        // complete before forwarding to callback, otherwise a truncated tail reaches the SSE
        // JSON encoder as invalid UTF-8 (garbled output, or an assertion in a debug-enabled build).
        Utf8StreamProcessor token_utf8_processor([&](std::string &complete_chars)
                                                  {
                                                      if (!callback(complete_chars))
                                                      {
                                                          should_stop = true;
                                                      }
                                                  });

        // 投机解码时 embd_inp 的最后一个 token 保留不预先 decode（作为首轮 id_last、略后一同与
        // draft 批量一并验证），与现有单模型路径完全一致。
        const int effective_prompt_len = is_speculative_
                ? std::max(0, (int) embd_inp.size() - 1)
                : (int) embd_inp.size();
        bool switch_to_speculative = false;
        bool eog_hit = false;

        // 共享的逐 token 输出管线：回调/停止序列检测/输出长度限制，单模型解码与投机
        // 解码验证轮次产出的 token 共用同一套逻辑（D5）。返回 false 表示应停止生成
        // （should_stop/eog_hit/输出长度限制均已通过外层捕获的引用置位）。
        auto emit_generated_token = [&](llama_token id) -> bool
        {
            std::string token_str = common_token_to_piece(ctx, id, params_.special);

            // Qwen ChatML 对话轮次结束标记 <|im_end|>：部分 GGUF 量化转换未在词表元数据里把它标记
            // 为 EOG token（llama_vocab_is_eog 判定为 false），导致它作为普通文本被生成并残留在
            // 最终 content 字段里。在推入输出管线之前拦截该字面值，复用与 EOG 命中完全相同的
            // eog_hit 分支结束生成，不新增并行输出路径（D5）。其它模型（gemma4/gpt-oss-20b 等）
            // 词表不会产出恰好等于该字面值的 token，零影响。
            if (token_str == "<|im_end|>")
            {
                eog_hit = true;
                return false;
            }

            generated_text_buf += token_str;
            token_utf8_processor.processStream(token_str.data(), token_str.size());
            if (should_stop)
            {
                return false;
            }
            n_generated++;
            if (max_length_ > 0 && n_generated >= max_length_)
            {
                My_Log{My_Log::Level::kWarning}
                    << "[LLAMACpp] " << query_type
                    << " Generated " << n_generated
                    << " tokens, reached max output limit " << max_length_
                    << ". Stopping generation." << std::endl;
                stopped_by_output_limit_ = true;
                should_stop = true;
                return false;
            }
            if (llama_vocab_is_eog(vocab, id))
            {
                eog_hit = true;
                return false;
            }
            return true;
        };

        // 线性推理主循环，对齐 completion.cpp 非交互（-no-cnv）分支：溢出平移 → 分批 decode → 采样 → 回显 → EOG/n_predict 退出
        while (n_remain != 0)
        {
            if (!embd.empty())
            {
                int max_embd_size = n_ctx - 4;
                if ((int) embd.size() > max_embd_size)
                {
                    embd.resize(max_embd_size);
                }

                // 上下文溢出时的 n_keep 平移（对齐 completion.cpp:609-637）
                if (n_past + (int) embd.size() >= n_ctx)
                {
                    const int n_left = n_past - n_keep;
                    const int n_discard = n_left / 2;
                    llama_memory_seq_rm(mem, 0, n_keep, n_keep + n_discard);
                    llama_memory_seq_add(mem, 0, n_keep + n_discard, n_past, -n_discard);
                    n_past -= n_discard;
                    My_Log{My_Log::Level::kWarning}
                        << "[LLAMACpp] " << query_type
                        << " Context overflow: discarded " << n_discard << " tokens, n_past=" << n_past << "\n";
                }

                // 按 n_batch（非 n_ubatch）分批 decode，对齐 completion.cpp:684-703
                if (!prefill_done && t_prefill_start_ms == 0)
                {
                    t_prefill_start_ms = now_ms();
                }

                if (llama_decode(ctx, llama_batch_get_one(embd.data(), (int) embd.size())))
                {
                    My_Log{My_Log::Level::kError} << "[LLAMACpp] " << query_type << " llama_decode FAILED\n";
                    return false;
                }

                n_past += (int) embd.size();
                total_decode_batches++;

                if (!prefill_done)
                {
                    total_prefill_tokens += (int) embd.size();

                    // Prefill 阶段心跳：每隔 kPrefillHeartbeatIntervalMs 毫秒向客户端发送一次保活消息，
                    // 防止中间代理因长时间无数据而超时断开；heartbeat 返回 false 表示客户端已断开，立即中止 prefill。
                    if (prefill_heartbeat)
                    {
                        auto t_now_hb = std::chrono::steady_clock::now();
                        int64_t elapsed_hb = std::chrono::duration_cast<std::chrono::milliseconds>(
                            t_now_hb - t_last_prefill_heartbeat).count();
                        if (elapsed_hb >= kPrefillHeartbeatIntervalMs)
                        {
                            t_last_prefill_heartbeat = t_now_hb;
                            My_Log{} << "[LLAMACpp] " << query_type
                                     << " Prefill heartbeat: batch #" << total_decode_batches
                                     << ", n_past=" << n_past
                                     << ", elapsed_ms=" << elapsed_hb << "\n";
                            if (!prefill_heartbeat())
                            {
                                // 连接已断开，立即中止 prefill
                                My_Log{My_Log::Level::kWarning}
                                    << "[LLAMACpp] " << query_type
                                    << " Prefill aborted: connection broken at n_past=" << n_past
                                    << " (batch #" << total_decode_batches << ")\n";
                                {
                                    std::lock_guard<std::mutex> lk(m);
                                    done = true;
                                    // 必须 notify，否则若外部调用 Stop() 等待 done==true，
                                    // 将因无通知而永久阻塞（cv.wait 不会自动唤醒）。
                                    cv.notify_one();
                                }
                                return false;
                            }
                        }
                    }
                }
            }

            embd.clear();

            // is_generated_token：区分"采样出的新 token"与"转发摄入的 prompt token"，仅回显前者
            bool is_generated_token = false;
            if (effective_prompt_len <= n_consumed)
            {
                if (!prefill_done)
                {
                    prefill_done = true;
                    t_prefill_end_ms = now_ms();
                    t_gen_start_ms = t_prefill_end_ms;
                }

                if (is_speculative_)
                {
                    switch_to_speculative = true;
                    break;
                }

                const llama_token id = common_sampler_sample(smpl, ctx, -1);
                common_sampler_accept(smpl, id, true);
                embd.push_back(id);
                is_generated_token = true;
                --n_remain;
            }
            else
            {
                while (effective_prompt_len > n_consumed)
                {
                    embd.push_back(embd_inp[n_consumed]);
                    common_sampler_accept(smpl, embd_inp[n_consumed], false);
                    ++n_consumed;
                    if ((int) embd.size() == params_.n_batch)
                    {
                        break;
                    }
                }
            }

            // 仅回显生成出的 token，绝不回显 prompt 本身（与 completion.cpp 作为 CLI 会打印 prompt 不同）
            if (is_generated_token)
            {
                for (auto id: embd)
                {
                    if (!emit_generated_token(id))
                    {
                        break;
                    }
                }
            }

            if (should_stop)
            {
                My_Log{}.original(true) << "\n\n";
                My_Log{} << "[LLAMACpp] " << query_type
                         << " Query stopped by callback/limit after " << n_generated << " tokens\n";
                break;
            }

            // 结束生成判断：非交互模式下的单一退出点，对齐 completion.cpp:970
            if (eog_hit)
            {
                My_Log{}.original(true) << "\n";
                My_Log{} << "[LLAMACpp] " << query_type
                         << " EOG token detected after " << n_generated << " tokens. Stopping.\n";
                break;
            }
        }

        if (switch_to_speculative)
        {
            constexpr llama_seq_id kSpecSeqId = 0;
            llama_token id_last = embd_inp.back();
            llama_tokens spec_prompt(embd_inp.begin(), embd_inp.end() - 1);
            llama_tokens draft;
            const int n_draft_max = std::max(1, params_.speculative.draft.n_max);

            // drafter（如 dflash2）自身的解码/缓存状态只能通过 common_speculative_process() 驱动；
            // prompt 部分虽然已经在上面共享的 prefill 循环里 decode 进了 target ctx，但从未喂给过
            // spec_，drafter 对 prompt 一无所知。这里用一个只含 prompt token 的 batch 补喂一次，
            // 对齐官方参考实现 examples/speculative-simple/speculative-simple.cpp 的顺序（prompt
            // process 必须先于 common_speculative_begin），否则首轮草稿会在对 prompt 一无所知的
            // 情况下生成，质量无法保证。
            {
                llama_batch batch_prompt = llama_batch_init((int) spec_prompt.size(), 0, 1);
                for (size_t i = 0; i < spec_prompt.size(); ++i)
                {
                    common_batch_add(batch_prompt, spec_prompt[i], (llama_pos) i, {kSpecSeqId}, false);
                }
                common_speculative_process(spec_.get(), batch_prompt);
                llama_batch_free(batch_prompt);
            }

            common_speculative_begin(spec_.get(), kSpecSeqId, spec_prompt);

            llama_batch batch_tgt = llama_batch_init(params_.n_batch, 0, 1);
            bool decode_failed = false;

            while (n_remain != 0)
            {
                if (draft.empty())
                {
                    common_speculative_get_draft_params(spec_.get(), kSpecSeqId) = {
                            /*.drafting =*/ true,
                            /*.n_max    =*/ n_draft_max,
                            /*.pos0     =*/ n_past,
                            /*.id_last  =*/ id_last,
                            /*.prompt   =*/ &spec_prompt,
                            /*.result   =*/ &draft,
                    };
                    common_speculative_draft(spec_.get());
                    n_draft_proposed_total += (int64_t) draft.size();

                    // 对齐官方参考实现 examples/speculative-simple/speculative-simple.cpp:206-213——
                    // common_speculative_draft() 对 dflash/dflash2 drafter 而言，无条件把整块
                    // n_max+1 个 token（id_last + n_max 个 mask token）一次性 decode 进 drafter
                    // 自己的 KV cache（fork common/speculative.cpp 的
                    // common_speculative_impl_draft_dflash::draft()，单次 llama_decode 覆盖
                    // pos0..pos0+n_max，与实际采样/接受了多少 token 无关）。这段写入只是"投机性
                    // 噪声块"，验证前必须立即回滚到 pos0，否则 drafter ctx 的 KV 水位线会比预期
                    // 多出恰好 n_max，导致后续 llama_decode 命中 M-RoPE 位置一致性校验失败
                    // （llama_decode: failed to decode, ret=-1，X-Y 恒等于 spec_draft_n_max）。
                    // 回滚位置对齐参考实现的 ckpt.pos_max+1，即本轮起始时的 n_past（此刻尚未 ++）。
                    if (params_.speculative.draft.ctx_dft)
                    {
                        llama_memory_seq_rm(llama_get_memory(params_.speculative.draft.ctx_dft), kSpecSeqId, n_past, -1);
                    }
                }

                common_batch_clear(batch_tgt);
                common_batch_add(batch_tgt, id_last, n_past++, {kSpecSeqId}, true);
                for (size_t i = 0; i < draft.size(); ++i)
                {
                    common_batch_add(batch_tgt, draft[i], n_past + (int) i, {kSpecSeqId}, true);
                }

                if (llama_decode(ctx, batch_tgt))
                {
                    My_Log{My_Log::Level::kError}
                        << "[LLAMACpp] " << query_type << " speculative llama_decode FAILED\n";
                    decode_failed = true;
                    break;
                }

                common_speculative_process(spec_.get(), batch_tgt);

                auto ids = common_sampler_sample_and_accept_n(smpl, ctx, draft);
                common_speculative_accept(spec_.get(), kSpecSeqId, (uint16_t) (ids.size() - 1));
                n_draft_accepted_total += (int64_t) ids.size() - 1;
                n_past += (int) ids.size() - 1;

                bool stop_now = false;
                for (auto next_id: ids)
                {
                    spec_prompt.push_back(id_last);
                    id_last = next_id;
                    --n_remain;
                    if (!emit_generated_token(id_last))
                    {
                        stop_now = true;
                        break;
                    }
                }

                draft.clear();
                llama_memory_seq_rm(mem, kSpecSeqId, n_past, -1);
                if (params_.speculative.draft.ctx_dft)
                {
                    llama_memory_seq_rm(llama_get_memory(params_.speculative.draft.ctx_dft), kSpecSeqId, n_past, -1);
                }

                if (stop_now || n_remain == 0)
                {
                    break;
                }
            }

            llama_batch_free(batch_tgt);

            if (decode_failed)
            {
                std::lock_guard<std::mutex> lk(m);
                done = true;
                cv.notify_one();
                return false;
            }

            if (should_stop)
            {
                My_Log{}.original(true) << "\n\n";
                My_Log{} << "[LLAMACpp] " << query_type
                         << " Query stopped by callback/limit after " << n_generated << " tokens\n";
            }
            else if (eog_hit)
            {
                My_Log{}.original(true) << "\n";
                My_Log{} << "[LLAMACpp] " << query_type
                         << " EOG token detected after " << n_generated << " tokens. Stopping.\n";
            }
        }

        auto t_query_end = std::chrono::steady_clock::now();
        int64_t total_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            t_query_end - t_query_start).count();
        int64_t gen_elapsed_ms = (t_gen_start_ms > 0) ? (now_ms() - t_gen_start_ms) : 0;
        double gen_rate = (gen_elapsed_ms > 0 && n_generated > 0) ?
            (n_generated * 1000.0 / gen_elapsed_ms) : 0.0;

        llama_perf_context_data perf_ctx = llama_perf_context(ctx);
        double prompt_rate_llama = (perf_ctx.t_p_eval_ms > 0) ?
            (perf_ctx.n_p_eval * 1000.0) / perf_ctx.t_p_eval_ms : 0.0;
        double gen_rate_llama = (perf_ctx.t_eval_ms > 0) ?
            (perf_ctx.n_eval * 1000.0) / perf_ctx.t_eval_ms : 0.0;

        // 投机解码下 target context 的 decode() 调用永远是"批量验证草稿窗口"，从不做单 token 调用；
        // llama.cpp 内部 perf 计数器按"这次 decode 排队了多少 token"分类（==1 才计入 n_eval/生成，
        // >1 全部计入 n_p_eval/prefill，见 fork src/llama-context.cpp::decode()），导致 n_eval 恒为
        // 0（llama_gen 恒为 0.0 tok/s），且 llama_prefill 被真实 prompt prefill 与后续每一轮 draft
        // 验证批次混在一起，两者在投机模式下都不再是"每 token"含义。这是 llama.cpp/fork 底层计数
        // 器的口径限制（批大小驱动的分类，非我们代码的计时遗漏），故用 n/a 标注，避免被误读为"生成
        // 失败"；真实吞吐看 gen_rate（我们自己端到端计时）与 draft_accepted（详见 llama_cpp.md）。
        std::ostringstream llama_perf_oss;
        if (spec_)
        {
            llama_perf_oss << "llama_prefill=n/a(spec) | llama_gen=n/a(spec)";
        }
        else
        {
            llama_perf_oss << "llama_prefill=" << std::fixed << std::setprecision(1) << prompt_rate_llama << " tok/s"
                           << " | llama_gen=" << std::fixed << std::setprecision(1) << gen_rate_llama << " tok/s";
        }

        My_Log{} << "[LLAMACpp] " << query_type << " Query DONE"
                 << " | total_ms=" << total_ms
                 << " | prefill_tokens=" << total_prefill_tokens
                 << " | gen_tokens=" << n_generated
                 << " | gen_rate=" << std::fixed << std::setprecision(1) << gen_rate << " tok/s"
                 << " | " << llama_perf_oss.str()
                 << (spec_ ? (" | draft_proposed=" + std::to_string(n_draft_proposed_total) +
                              " | draft_accepted=" + std::to_string(n_draft_accepted_total)) : std::string())
                 << "\n";

        if (is_aux_inference && !generated_text_buf.empty()) {
            // 打印完整生成文本（辅助推理结果）
            My_Log{} << "[LLAMACpp] " << query_type
                     << " Full generated text: \"" << generated_text_buf << "\"\n";
        }

        std::lock_guard<std::mutex> lk(m);
        done = true;
        cv.notify_one();
        return true;
    }

    ~Impl()
    {
        {
            std::lock_guard<std::mutex> lk(m);
            done = true;
        }
        cv.notify_all();

        std::this_thread::sleep_for(std::chrono::milliseconds(100));

        // 先分离 threadpool，再释放（顺序必须）
        llama_context *ctx = llama_init ? llama_init->context() : nullptr;
        if (ctx)
        {
            llama_attach_threadpool(ctx, nullptr, nullptr);
        }

        if (threadpool)
        {
            ggml_threadpool_free_fn(threadpool);
            threadpool = nullptr;
        }
        if (threadpool_batch)
        {
            ggml_threadpool_free_fn(threadpool_batch);
            threadpool_batch = nullptr;
        }

        // 投机解码双 context 析构顺序契约见同名文档 llama_cpp.md：draft 必须先于 target 释放。
        spec_.reset();
        draft_init_.reset();

        // llama_init 释放时会自动释放 model/context/sampler，sampler 的所有权属于 llama_init，
        // 不要手动释放。
        if (llama_init)
        {
            llama_init.reset();
            smpl = nullptr;
        }

        llama_backend_free();
    }

    common_params params_;
    common_init_result_ptr llama_init;

    // 投机解码双 context 生命周期契约见同名文档 llama_cpp.md（draft 必须先于 target 析构）
    common_speculative_init_result_ptr draft_init_;
    common_speculative_ptr spec_;
    bool is_speculative_ = false;

    ggml_threadpool *(*ggml_threadpool_new_fn)(ggml_threadpool_params *){};

    void (*ggml_threadpool_free_fn)(ggml_threadpool *){};

    const llama_vocab *vocab = nullptr;
    common_sampler *smpl = nullptr;
    ggml_threadpool *threadpool_batch{};
    ggml_threadpool *threadpool{};

    int n_past = 0;
    int n_consumed = 0;

    std::vector<llama_token> embd;
    std::vector<llama_token> embd_inp;

    std::mutex m;
    std::condition_variable cv;
    bool done{false};

    // Fix: 最大输出 token 数（由 SetParams 设置，默认 4096）
    int max_length_ = 4096;
    // Fix: 是否因输出 token 限制而停止生成
    bool stopped_by_output_limit_ = false;

private:
    // 把 ggml/llama 内部日志按 ggml_log_level 映射到项目统一日志体系（My_Log::Level）转发，
    // 遵循项目默认日志级别（kWarning）过滤，不再使用裸时间戳 fprintf 直接写 stderr。
    static void RegisterLogAdapter()
    {
        llama_log_set([](ggml_log_level level, const char *text, void * /*user_data*/)
                    {
                        My_Log::Level mapped_level;
                        switch (level)
                        {
                            case GGML_LOG_LEVEL_ERROR:
                                mapped_level = My_Log::Level::kError;
                                break;
                            case GGML_LOG_LEVEL_WARN:
                                mapped_level = My_Log::Level::kWarning;
                                break;
                            case GGML_LOG_LEVEL_INFO:
                                mapped_level = My_Log::Level::kInfo;
                                break;
                            case GGML_LOG_LEVEL_DEBUG:
                                mapped_level = My_Log::Level::kDebug;
                                break;
                            default:
                                mapped_level = My_Log::Level::kInfo;
                                break;
                        }
                        My_Log{mapped_level} << text;
                    }, nullptr);
    }
};

LLAMACppBuilder::LLAMACppBuilder(const ModelInstanceConfig &config) :
        ContextBase{config}
{
    std::string gguf_path;
    for (const auto &entry: fs::directory_iterator(model_config_.get_model_path()))
    {
        if (entry.is_regular_file() && entry.path().extension() == ".gguf")
        {
            gguf_path = entry.path().string();
        }
    }

    // 投机解码检测必须在构造 argv 之前完成：-fa/-c/--reasoning-budget 等用户给定的激进参数
    // 需要在 common_params_parse 阶段就生效（这些 flag 本身在白名单内）；而 -md/--spec-type/
    // --spec-draft-n-max/--spec-draft-p-min 这 4 个投机专用 flag 不在 LLAMA_EXAMPLE_COMPLETION 白名单
    // 内，必须在 parse 成功后用 C++ 代码直接赋值（见下方）。完整契约见同名文档 llama_cpp.md。
    llama_speculative::DraftModelConfig draft_config;
    try
    {
        const std::string &config_path = model_config_.i_model_config_.get_config_path();
        if (!config_path.empty() && File::IsFileExist(config_path) && !File::IsFileEmpty(config_path))
        {
            std::ifstream config_stream(config_path);
            json model_json;
            config_stream >> model_json;
            draft_config = llama_speculative::DraftModelConfig::ParseFrom(model_json);
        }
    }
    catch (...)
    {
        My_Log{My_Log::Level::kDebug}
            << "[LLAMACpp] Failed to read optional draft_model field from model config.json, ignored\n";
    }
    const bool speculative_requested = draft_config.present && !draft_config.path.empty();

    // 在初始化 llama.cpp 后端之前设置该 OpenCL Adreno 大缓冲区环境变量。该后端只看变量是否存在、
    // 不看取值。workspace/qwen38-dflash2-x2-90-repro/README.md 105-110 行建议投机解码保持该
    // 变量 unset——但那条建议是针对 README 自己验证过的基线命令行 `-c 8192`（该配置下最大单个
    // 分配约 404 MiB，明显低于不设该变量时约 2GB 的地址算术上限）。我们的投机解码路径强制使用
    // 用户给定的激进值 `-c 32768`（见下方 n_ctx 赋值），是 README 基线的 4 倍；已实测证实：
    // unset 该变量时，target 模型（27B）在这个更大的上下文尺寸下会在 GPU 与 CPU 两条路径都
    // 抛出 "bad allocation"（服务日志：GGUFVerify GPU/CPU load failed: bad allocation），说明
    // 某个随 n_ctx 缩放的单一缓冲区（可能是完整 KV cache 缓冲或注意力计算缓冲）在 32768 上下文
    // 下已超出该 2GB 上限——必须保持该变量为 "1" 才能在这个上下文尺寸下成功分配，功能正确性优先
    // 于 README 描述的这一点性能损耗。现有单模型 GGUF 路径（gemma4/gpt-oss-20b 等）本就一直设为
    // "1"，此处统一为无条件设置，不再区分投机解码分支。
#if defined(_WIN32)
    _putenv_s("GGML_OPENCL_ADRENO_USE_LARGE_BUFFER", "1");
#else
    setenv("GGML_OPENCL_ADRENO_USE_LARGE_BUFFER", "1", 1);
#endif
    My_Log{} << "[Env] GGML_OPENCL_ADRENO_USE_LARGE_BUFFER=1 set\n";

    // 上下文窗口大小：未配置时使用默认值；声明了 draft_model 的模型强制使用用户给定的激进值
    // -c 32768，与 -ngld 99（下方 C++ 直接赋值 params.speculative.draft.*）同属一组只在声明了
    // draft_model 时才生效的硬编码值。
    size_t configured_context_size = model_config_.get_context_size();
    int n_ctx = configured_context_size > 0 ? static_cast<int>(configured_context_size) : 8192;
    if (speculative_requested)
    {
        n_ctx = 32768;
    }

    std::string device = model_config_.get_device();
    std::transform(device.begin(), device.end(), device.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

    // 直接构造与 llama-completion.exe（非投机分支）/llama-server.exe（投机分支）完全等价的
    // 命令行参数，交给 common_params_parse 走与 CLI 工具完全相同的解析/后处理路径——避免手动
    // 逐字段赋值遗漏只在参数解析时才生效的设置（如 --device），GPU/CPU 取值均已用
    // llama-completion.exe 反复 A/B 实测验证为最快组合。
    std::vector<std::string> arg_strings = {
        "GenieService.exe",
        "--model", gguf_path,
        "--ctx-size", std::to_string(n_ctx),
        "--no-warmup",
        "-s", "42",
        "-fa", speculative_requested ? "off" : "on",
    };
    if (!speculative_requested)
    {
        // --fit（自动显存/上下文拟合）在投机解码分支必须保持 llama.cpp 官方默认值 on：原生
        // llama-server.exe 用同一批模型+同一套激进参数跑通时，日志里的 "failed to measure the
        // memory of the extra model, fitting without it" 正是 fitting 阶段的正常输出——dflash
        // drafter 的 context 必须在 target ctx 之后才能建立，fitting 逻辑对此有专门容错。强制
        // --fit off 会让 target/draft 双 context 一次性按满额申请显存，在本机（大缓冲区模式实际
        // 被驱动编译器拒绝）直接抛 std::bad_alloc。
        arg_strings.insert(arg_strings.end(), {"--fit", "off"});

        // -no-cnv 的 .set_examples() 只注册了 LLAMA_EXAMPLE_COMPLETION，投机分支下面会切到
        // LLAMA_EXAMPLE_SERVER 解析（该 example 不认识这个 flag，会直接 invalid argument 导致
        // parse 失败），因此只在非投机分支拼接；我们代码从不读 params.conversation_mode，去掉
        // 对非投机分支零影响。
        arg_strings.push_back("-no-cnv");
    }
    if (speculative_requested)
    {
        // 用户给定的原始 llama-server.exe 命令行：
        // --spec-draft-n-max 7 --spec-draft-p-min 0 -fa 0 -ngl 99 -ngld 99 -c 32768
        // --reasoning-budget 3072，以及 -md/--spec-type。这 5 个投机专用 flag（-md/--spec-type/
        // --spec-draft-n-max/--spec-draft-p-min/-ngld）的 .set_examples() 范围包含
        // LLAMA_EXAMPLE_SERVER、不包含 LLAMA_EXAMPLE_COMPLETION，因此本分支下面把
        // common_params_parse 的 example 切到 LLAMA_EXAMPLE_SERVER，让它们走原生 argv 解析，
        // 不再需要 parse 后手动二次赋值 params.speculative.*。
        arg_strings.insert(arg_strings.end(), {"--reasoning-budget", "3072"});

        fs::path draft_path = fs::path(model_config_.get_model_path()) / draft_config.path;
        arg_strings.insert(arg_strings.end(), {"-md", draft_path.string()});
        if (!draft_config.spec_type.empty())
        {
            arg_strings.insert(arg_strings.end(), {"--spec-type", draft_config.spec_type});
        }
        if (draft_config.spec_draft_n_max > 0)
        {
            arg_strings.insert(arg_strings.end(),
                                {"--spec-draft-n-max", std::to_string(draft_config.spec_draft_n_max)});
        }
        arg_strings.insert(arg_strings.end(),
                            {"--spec-draft-p-min", std::to_string(draft_config.spec_draft_p_min)});
        if (device != "cpu")
        {
            arg_strings.insert(arg_strings.end(), {"-ngld", "99"});
        }
    }
    if (device == "cpu")
    {
        // CPU 路径：保持 mmap/repack/no-host 的 llama.cpp 官方默认值（已实测验证比强制关闭更快）。
        arg_strings.insert(arg_strings.end(), {"-ngl", "0"});
    }
    else if (speculative_requested)
    {
        // 投机解码分支严格对齐用户给定、且已用原生 llama-server.exe 在本机实测跑通的命令行：
        // 只给 -ngl 99，既不带 --no-mmap/--no-repack/--no-host/-ub 1024，也不带 --device。
        // 不带 --device 是关键：--device GPUOpenCL 会把可用后端设备裁剪成只剩 OpenCL GPU 一个，
        // 而 dflash 的 target+draft 双 context 在 --fit 阶段需要 CPU 设备参与兜底（本机驱动的
        // 大缓冲区模式实际为 OFF，超设备上限的 buffer 必须 fall back 到 host memory），裁剪掉
        // CPU 设备后 14.6GB target 的分配无处可退，直接抛 std::bad_alloc。原生 llama-server.exe
        // 的命令行同样没有 --device。
        arg_strings.insert(arg_strings.end(), {
            "-ngl", "99",
        });
    }
    else
    {
        // 非投机（现有全部单模型 GGUF）路径：保持历史 A/B 实测出的最快组合，本轮不改动。
        arg_strings.insert(arg_strings.end(), {
            "--device", "GPUOpenCL",
            "-ngl", "99",
            "--no-mmap",
            "--no-repack",
            "--no-host",
            "-ub", "1024",
        });
    }

    std::vector<char *> argv;
    argv.reserve(arg_strings.size());
    for (auto &s: arg_strings)
    {
        argv.push_back(s.data());
    }

    // 声明了 draft_model 的模型走 LLAMA_EXAMPLE_SERVER（与真实 llama-server.exe 入口
    // tools/server/server.cpp 的 common_params_parse 调用完全一致），使 -md/--spec-type/
    // --spec-draft-n-max/--spec-draft-p-min/-ngld 直接原生解析生效；不含该字段的模型继续走
    // LLAMA_EXAMPLE_COMPLETION，零回归风险。
    common_params params;
    const enum llama_example example = speculative_requested ? LLAMA_EXAMPLE_SERVER : LLAMA_EXAMPLE_COMPLETION;
    if (!common_params_parse((int) argv.size(), argv.data(), params, example, nullptr))
    {
        throw std::runtime_error("common param parse failed");
    }

    params.verbosity = 0;           // 禁用详细日志

    // 复刻原生 llama-server.exe 入口（tools/server/server.cpp:152-157）对 "auto" 槽位数的归一化：
    // common_params_parser_init()（common/arg.cpp:1391-1400）只在 ex == LLAMA_EXAMPLE_SERVER 时把
    // params.n_parallel 预置为 -1 表示 auto，真正把它变成合法值的代码在 llama-server 的 main() 里，
    // 而不是在 common_params_parse() 内部。我们自己建 context、不经过那个 main()，因此这个 -1 会
    // 原样流到 common_context_params_to_llama()（common/common.cpp:1723 `cparams.n_seq_max =
    // params.n_parallel`）被隐式转成 uint32_t 4294967295，两条路径由此同源失败：CPU 路径撞上
    // llama-context.cpp 的 `n_seq_max must be <= 256`（LLAMA_MAX_SEQ）早期校验直接抛异常；GPU 路径
    // 没有等价早期校验，继续按这个天量序列数分配 KV/计算缓冲，抛 std::bad_alloc（表现为
    // "the context is being destroyed" + "bad allocation"）。
    // 非投机分支走 LLAMA_EXAMPLE_COMPLETION，n_parallel 保持默认 1，此处条件恒不成立，零回归。
    // 取值不照搬原生的 4：原生那 4 是 llama-server 的并发请求槽位数，而本服务的一个
    // LLAMACppContext 恒定只解码一条序列（seq_id 0）。而 n_seq_max 会同时放大 KV cache 与
    // dflash 的 recurrent-state（rs）cache——实测取 4 时 rs cache 单块需 1995 MiB OpenCL buffer，
    // 在多轮加载/切换后会撞上 Adreno 单 context 分配上限而失败（"failed to allocate buffer for
    // rs cache"）。取 1 语义等价且显存占用降到 1/4，是本服务真实用法下的正确值。
    // kv_unified 一并置 true，与原生保持一致：统一 KV 池语义在单序列下等价，且避免按序列分池。
    if (params.n_parallel < 0)
    {
        params.n_parallel = 1;
        params.kv_unified = true;
    }

    // 线程数：运行时动态获取物理/逻辑核心数，替代旧的硬编码值 10，零配置适配不同机器
    // （已通过专项测试确认：GPU 全量卸载场景下线程数在合理范围内影响很小，CPU 场景下
    // 用满核心数是稳妥的默认策略）。
    const auto hw_threads = static_cast<int32_t>(std::thread::hardware_concurrency());
    params.cpuparams.n_threads = hw_threads > 0 ? hw_threads : 4;
    params.cpuparams_batch.n_threads = params.cpuparams.n_threads;

    // 生成配置：不设固定上限，交由 SetParams("size", ...) 驱动的 max_length_ 来控制输出长度上限。
    params.n_predict = -1;

    // Harmony 格式使用 <|return|>/<|call|> 等特殊 token 作为解码时停止标记；special=false（默认值）
    // 会让 common_token_to_piece() 对特殊 token 返回空串，导致 HarmonyProcessor 无法识别结束标记。
    params.special = true;

    if (speculative_requested)
    {
        My_Log{} << "[LLAMACpp] Speculative decoding requested: draft_path="
                 << params.speculative.draft.mparams.path
                 << ", spec_type=" << draft_config.spec_type
                 << ", n_max=" << params.speculative.draft.n_max
                 << ", p_min=" << params.speculative.draft.p_min
                 << ", n_gpu_layers=" << params.speculative.draft.n_gpu_layers
                 << ", required=" << draft_config.required << "\n";
    }

    My_Log{} << "[LLAMACpp] Loaded params: device=" << device
             << ", n_ctx=" << params.n_ctx
             << ", n_gpu_layers=" << params.n_gpu_layers
             << ", no_extra_bufts=" << params.no_extra_bufts
             << ", no_host=" << params.no_host
             << ", n_batch=" << params.n_batch
             << ", n_ubatch=" << params.n_ubatch
             << ", n_threads=" << params.cpuparams.n_threads
             << ", n_parallel=" << params.n_parallel
             << ", kv_unified=" << params.kv_unified
             << "\n";

    impl_ = new Impl{std::move(params)};
}

int LLAMACppBuilder::SetParams(const std::string &key, const std::string &value)
{
    if (impl_ && key == "size")
    {
        impl_->max_length_ = std::stoi(value);
        My_Log{My_Log::Level::kInfo}
            << "[LLAMACpp] SetParams: max_length=" << impl_->max_length_ << std::endl;
    }
    return 0;
}

bool LLAMACppBuilder::Query(const ModelInput &model_input, const Callback& callback,
                             PrefillHeartbeatCallback prefill_heartbeat)
{
    // 序列化所有 Query() 调用：llama_context 不是线程安全的，同一时刻只能有一个
    // 推理在执行。安全检查（ContentSecurityInspector）、复杂度评估
    // （TaskComplexityEvaluator）和正常推理都通过同一个模型句柄调用 Query()，
    // 必须通过此锁保证互斥，防止并发调用 llama_decode 导致 KV cache 损坏崩溃。

    // 检测锁竞争：等待超过 10ms 视为异常，记录警告
    auto t_lock_wait_start = std::chrono::steady_clock::now();
    std::lock_guard<std::mutex> lock(query_mutex_);
    int64_t lock_wait_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t_lock_wait_start).count();
    if (lock_wait_ms > 10)
    {
        My_Log{My_Log::Level::kWarning}
            << "[LLAMACpp] query_mutex_ wait time: " << lock_wait_ms << "ms (lock contention detected)\n";
    }

    auto &prompt = model_input.text_;

    // 检测查询类型（同时供本函数日志与 Impl::Query 内部日志复用，避免对同一个 prompt 重复扫描）
    const char *query_type_label;
    bool is_aux_inference;
    if (prompt.find("You are a security classifier") != std::string::npos)
    {
        query_type_label = "SECURITY_CHECK";
        is_aux_inference = true;
    }
    else if (prompt.find("You are a task complexity classifier") != std::string::npos)
    {
        query_type_label = "COMPLEXITY_CHECK";
        is_aux_inference = true;
    }
    else if (prompt.find("You are a redaction assistant") != std::string::npos)
    {
        query_type_label = "DESENSITIZE";
        is_aux_inference = true;
    }
    // 使用 ModelInput 中由 ModelInputBuilder 预先检测并设置的 agent_type_ 字段
    else if (model_input.agent_type_ == "main")
    {
        query_type_label = "MAINAGENT_INFERENCE";
        is_aux_inference = false;
    }
    else
    {
        query_type_label = "SUBAGENT_INFERENCE";
        is_aux_inference = false;
    }

    if (!is_aux_inference)
    {
        My_Log{} << "\n[Prompt] [" << query_type_label << "][" << model_config_.get_model_name() << "]:\n"
                 << prompt << "\n------------\n\n"
                 << "[Response] [" << query_type_label << "][" << model_config_.get_model_name() << "]:\n";
    }
    else
    {
        My_Log{} << "\n[Prompt] [" << query_type_label << "]:\n"
                 << prompt << "\n------------\n\n"
                 << "[Response] [" << query_type_label << "]:\n";
    }

    std::string query_type_bracketed = std::string("[") + query_type_label + "]";
    bool result = impl_->Query(prompt, callback, prefill_heartbeat,
                                query_type_bracketed.c_str(), is_aux_inference);
    // Fix: 将 Impl 内部的 stopped_by_output_limit_ 状态传播到外部
    stopped_by_output_limit_ = impl_->stopped_by_output_limit_;
    return result;
}

LLAMACppBuilder::~LLAMACppBuilder()
{
    delete impl_;
    impl_ = nullptr;
}

bool LLAMACppBuilder::Stop()
{
    std::unique_lock<std::mutex> lk(impl_->m);
    impl_->cv.wait(lk, [this]
    { return impl_->done; });
    return true;
}

json LLAMACppBuilder::HandleProfile()
{
    json result;
    
    if (impl_)
    {
        llama_context *ctx = impl_->llama_init->context();
        if (!ctx)
        {
            return result;
        }
        
        // 从 llama.cpp 获取性能统计数据
        llama_perf_context_data perf_ctx = llama_perf_context(ctx);
        
        std::ostringstream oss;
        
        // time_to_first_token (prompt 处理时间,转换为秒)
        oss << std::fixed << std::setprecision(2)
            << perf_ctx.t_p_eval_ms / 1000.0;
        result["time_to_first_token"] = oss.str();
        
        // token_generation_time (生成时间,转换为秒)
        oss.str("");
        oss << std::fixed << std::setprecision(2)
            << perf_ctx.t_eval_ms / 1000.0;
        result["token_generation_time"] = oss.str();
        
        // prompt_processing_rate (tokens/秒)
        oss.str("");
        oss << std::fixed << std::setprecision(2);
        if (perf_ctx.t_p_eval_ms > 0)
        {
            oss << (perf_ctx.n_p_eval * 1000.0) / perf_ctx.t_p_eval_ms;
        }
        else
        {
            oss << 0.0;
        }
        result["prompt_processing_rate"] = oss.str();
        
        // token_generation_rate (tokens/秒)
        oss.str("");
        oss << std::fixed << std::setprecision(2);
        if (perf_ctx.t_eval_ms > 0)
        {
            oss << (perf_ctx.n_eval * 1000.0) / perf_ctx.t_eval_ms;
        }
        else
        {
            oss << 0.0;
        }
        result["token_generation_rate"] = oss.str();
        
        // integer values
        result["num_prompt_tokens"] = perf_ctx.n_p_eval;
        result["num_generated_tokens"] = perf_ctx.n_eval;
    }
    
    return result;
}

size_t LLAMACppBuilder::TokenLength(const std::string &text)
{
    // 对输入文本进行 tokenize，并返回 token 数。
    // 注意：embd_inp 是对话/推理过程中的输入缓冲，和外部传入 text 无关。
    // 这里必须基于当前模型/上下文的 vocab 来做编码。
    llama_context *ctx = impl_->llama_init->context();
    if (ctx == nullptr)
    {
        return 0;
    }

    // common_tokenize 会基于 ctx 的 vocab 进行编码。
    // add_special=false: 不额外添加 BOS/EOS 等特殊 token（仅统计 text 自身的编码长度）
    // parse_special=true: 允许文本中包含类似 <|...|> 这类特殊 token 时按 token 解析
    const auto tokens = common_tokenize(ctx, text, /* add_special = */ false, /* parse_special = */ true);
    return tokens.size();
}
