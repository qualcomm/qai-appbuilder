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
#include <atomic>
#include <vector>

namespace fs = std::filesystem;

static std::atomic<bool> g_llama_prefix_cache_enabled{true};
static std::atomic<int> g_llama_prefix_head_checkpoints{2};
static std::atomic<bool> g_llama_prefix_head_checkpoints_explicit{false};

static inline int64_t now_ms()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

namespace {

struct WatermarkSamplerData {
    const GenieWatermarkProviderVTable* vtable;
    GenieWatermarkTokenHook*             hook;
    uint32_t                             n_vocab;
    uint64_t                             seed;
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
    explicit Impl(common_params &&params, llama_flash_attn_type draft_flash_attn = LLAMA_FLASH_ATTN_TYPE_AUTO)
            : params_{std::move(params)}, draft_flash_attn_{draft_flash_attn}
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
            throw TargetWeightsLoadError("unable to load model");
        }

        if (ctx == nullptr)
        {
            throw std::runtime_error("unable to create llama context for model");
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
            // common_base_params_to_speculative() 整份复制主模型 params（含 flash_attn_type），
            // 草稿 context 由本服务自己创建，故在此单独覆盖，使主模型/草稿的 FA 可以分别取值。
            params_dft.flash_attn_type = draft_flash_attn_;
            My_Log{} << "[LLAMACpp] draft flash_attn: "
                     << llama_flash_attn_type_name(params_dft.flash_attn_type)
                     << " (target: " << llama_flash_attn_type_name(params_.flash_attn_type) << ")\n";
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

            ReserveWorstCaseGraph(ctx);
        }
    }

    void ReserveWorstCaseGraph(llama_context *ctx)
    {
        llama_token warmup_tok = llama_vocab_bos(vocab);
        if (warmup_tok == LLAMA_TOKEN_NULL)
        {
            warmup_tok = llama_vocab_eos(vocab);
        }
        if (warmup_tok == LLAMA_TOKEN_NULL)
        {
            My_Log{My_Log::Level::kWarning}
                    << "[LLAMACpp] worst-case graph reserve skipped: vocab has neither BOS nor EOS\n";
            return;
        }

        My_Log{} << "[LLAMACpp] worst-case graph reserve: warmup decode\n";
        const int rc = llama_decode(ctx, llama_batch_get_one(&warmup_tok, 1));
        llama_memory_clear(llama_get_memory(ctx), false);
        llama_perf_context_reset(ctx);
        if (rc != 0)
        {
            throw std::runtime_error("worst-case graph reserve failed: llama_decode returned " +
                                     std::to_string(rc));
        }
        My_Log{} << "[LLAMACpp] worst-case graph reserve: done\n";
    }

    bool Query(const std::string &prompt,
               const std::function<bool(std::string &)> &callback,
               const std::function<bool()> &prefill_heartbeat,
               const char *query_type, bool is_aux_inference)
    {
        auto t_query_start = std::chrono::steady_clock::now();

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

        stopped_by_output_limit_ = false;
        if (!is_aux_inference)
        {
            last_trace_metrics_ = nullptr;
        }
        int n_generated = 0;
        bool should_stop = false;

        n_past = 0;
        n_consumed = 0;
        embd.clear();
        embd_inp.clear();

        common_sampler_reset(smpl);

        llama_context *ctx = llama_init->context();
        llama_memory_t mem = llama_get_memory(ctx);
        llama_context *ctx_dft = (is_speculative_ && params_.speculative.draft.ctx_dft)
                ? params_.speculative.draft.ctx_dft : nullptr;
        const bool prefix_cache_on = g_llama_prefix_cache_enabled.load() && !is_aux_inference;
        const bool had_prefix_cache = prefix_cache_valid_;
        prefix_cache_valid_ = false;

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
        int64_t n_draft_proposed_total = 0;
        int64_t n_draft_accepted_total = 0;
        std::vector<int64_t> draft_accept_hist;
        int64_t spec_draft_ms = 0;
        int64_t spec_verify_ms = 0;
        int64_t spec_post_ms = 0;
        int64_t spec_readback_ms = 0;
        int64_t spec_rounds = 0;

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

        int n_reuse = 0;
        if (prefix_cache_on && had_prefix_cache && effective_prompt_len > 1)
        {
            n_reuse = MatchPrefixCache(ctx, ctx_dft, effective_prompt_len - 1);
        }
        if (n_reuse == 0)
        {
            prefix_ckpts_.clear();
            prefix_cache_tokens_.clear();
            llama_memory_clear(mem, false);
            if (ctx_dft)
            {
                llama_memory_clear(llama_get_memory(ctx_dft), false);
            }
        }
        else
        {
            for (int i = 0; i < n_reuse; ++i)
            {
                common_sampler_accept(smpl, embd_inp[i], false);
            }
            n_past = n_reuse;
            n_consumed = n_reuse;
            prefix_cache_valid_ = true;
        }
        int ckpt_target = FindCheckpointPos(effective_prompt_len, n_reuse);
        std::vector<int> ckpt_points = prefix_cache_on
                ? FindHeadCheckpointPos(effective_prompt_len, n_reuse, ckpt_target) : std::vector<int>{};
        if (ckpt_target > 0)
        {
            ckpt_points.push_back(ckpt_target);
        }
        const int ckpt_end = prefix_cache_on ? FindCheckpointPos(effective_prompt_len, n_reuse, 1) : -1;
        if (ckpt_end > ckpt_target)
        {
            ckpt_points.push_back(ckpt_end);
        }
        std::string head_str;
        for (size_t i = 0; i + 1 < ckpt_points.size(); ++i)
        {
            head_str += (i ? "," : "") + std::to_string(ckpt_points[i]);
        }
        My_Log{} << "[LLAMACpp] " << query_type
                 << " Prefix cache: enabled=" << prefix_cache_on
                 << " | reused_tokens=" << n_reuse
                 << " | checkpoint_at=" << ckpt_target
                 << " | head_checkpoints=" << head_str
                 << " | prompt_tokens=" << embd_inp.size() << "\n";

        bool think_in_progress = false;
        int think_token_start = -1;
        bool think_force_closing = false;
        std::vector<llama_token> think_force_tokens;
        const int ctx_left_for_output = std::max(1, n_ctx - (int) embd_inp.size());
        const int max_output_tokens = max_length_ > 0 ? std::min(max_length_, ctx_left_for_output) : ctx_left_for_output;
        int32_t think_budget_tokens = params_.sampling.reasoning_budget_tokens;
        if (!is_aux_inference && think_time_budget_s_ > 0)
        {
            const double tg_rate = last_decode_tok_s_ > 0.0 ? last_decode_tok_s_ : 10.0;
            int64_t budget = (int64_t) (tg_rate * think_time_budget_s_);
            const int64_t ctx_left = (int64_t) n_ctx - (int64_t) embd_inp.size() - think_ctx_reserve_;
            if (think_max_tokens_ > 0)
            {
                budget = std::min<int64_t>(budget, think_max_tokens_);
            }
            budget = std::max<int64_t>(budget, think_min_tokens_);
            budget = std::max<int64_t>(0, std::min(budget, ctx_left));
            think_budget_tokens = (int32_t) budget;
            think_in_progress = true;
            think_token_start = 0;
            static const std::string kOpenThink = "<think>\n";
            generated_text_buf += kOpenThink;
            token_utf8_processor.processStream(kOpenThink.data(), kOpenThink.size());
            My_Log{} << "[LLAMACpp] " << query_type << " adaptive think budget=" << think_budget_tokens
                     << " (tg_rate=" << tg_rate << ", time_budget_s=" << think_time_budget_s_
                     << ", ctx_left=" << ctx_left << ")\n";
        }

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
            if (think_budget_tokens >= 0)
            {
                if (!think_in_progress)
                {
                    if (generated_text_buf.find("<think>") != std::string::npos)
                    {
                        think_in_progress = true;
                        think_token_start = n_generated;
                    }
                }
                else if (generated_text_buf.find("</think>") != std::string::npos)
                {
                    think_in_progress = false;
                }
                else if (!think_force_closing && n_generated - think_token_start >= think_budget_tokens)
                {
                    std::string force_text = params_.sampling.reasoning_budget_message;
                    if (force_text.empty())
                    {
                        force_text = "\n\nConsidering the time constraints, I will give the final answer directly based on the analysis above.\n";
                    }
                    force_text += "</think>\n\n";
                    think_force_tokens = common_tokenize(ctx, force_text, false, true);
                    think_force_closing = !think_force_tokens.empty();
                    think_in_progress = false;
                    My_Log{My_Log::Level::kWarning}
                        << "[LLAMACpp] " << query_type
                        << " Reasoning budget (" << think_budget_tokens
                        << " tokens) exceeded after " << n_generated
                        << " total generated tokens. Forcing </think> close." << std::endl;
                }
            }
            if (n_generated >= max_output_tokens)
            {
                My_Log{My_Log::Level::kWarning}
                    << "[LLAMACpp] " << query_type
                    << " Generated " << n_generated
                    << " tokens, reached max output limit " << max_output_tokens
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
                    ckpt_target = -1;
                    ckpt_points.clear();
                    prefix_cache_valid_ = false;
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
                    prefix_cache_valid_ = false;
                    return false;
                }

                if (is_speculative_)
                {
                    llama_batch batch_chunk = llama_batch_init((int) embd.size(), 0, 1);
                    for (size_t i = 0; i < embd.size(); ++i)
                    {
                        common_batch_add(batch_chunk, embd[i], (llama_pos) (n_past + (int) i), {0}, false);
                    }
                    common_speculative_process(spec_.get(), batch_chunk);
                    llama_batch_free(batch_chunk);
                }

                n_past += (int) embd.size();
                total_decode_batches++;

                if (prefix_cache_on && !prefill_done &&
                    std::find(ckpt_points.begin(), ckpt_points.end(), n_past) != ckpt_points.end())
                {
                    SavePrefixCheckpoint(ctx, ctx_dft, n_past);
                }

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

                llama_token id;
                if (think_force_closing && !think_force_tokens.empty())
                {
                    id = think_force_tokens.front();
                    think_force_tokens.erase(think_force_tokens.begin());
                    if (think_force_tokens.empty())
                    {
                        think_force_closing = false;
                    }
                }
                else
                {
                    id = common_sampler_sample(smpl, ctx, -1);
                }
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
                    if ((int) embd.size() == params_.n_batch ||
                        std::find(ckpt_points.begin(), ckpt_points.end(), n_consumed) != ckpt_points.end())
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

            common_speculative_begin(spec_.get(), kSpecSeqId, spec_prompt);

            llama_batch batch_tgt = llama_batch_init(params_.n_batch, 0, 1);
            bool decode_failed = false;

            while (n_remain != 0)
            {
                if (n_past + 1 + n_draft_max >= n_ctx)
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[LLAMACpp] " << query_type
                        << " Context limit reached (n_past=" << n_past << ", n_ctx=" << n_ctx
                        << "). Stopping generation." << std::endl;
                    stopped_by_output_limit_ = true;
                    should_stop = true;
                    break;
                }

                if (think_force_closing && !think_force_tokens.empty())
                {
                    llama_token forced_id = think_force_tokens.front();
                    think_force_tokens.erase(think_force_tokens.begin());
                    if (think_force_tokens.empty())
                    {
                        think_force_closing = false;
                    }

                    common_batch_clear(batch_tgt);
                    common_batch_add(batch_tgt, id_last, n_past++, {kSpecSeqId}, true);
                    if (llama_decode(ctx, batch_tgt))
                    {
                        My_Log{My_Log::Level::kError}
                            << "[LLAMACpp] " << query_type << " forced-close llama_decode FAILED\n";
                        decode_failed = true;
                        break;
                    }
                    common_speculative_process(spec_.get(), batch_tgt);
                    common_sampler_accept(smpl, forced_id, true);
                    spec_prompt.push_back(id_last);
                    id_last = forced_id;
                    --n_remain;
                    if (!emit_generated_token(id_last))
                    {
                        break;
                    }
                    continue;
                }

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
                    const int64_t t_draft0 = now_ms();
                    common_speculative_draft(spec_.get());
                    spec_draft_ms += now_ms() - t_draft0;
                    n_draft_proposed_total += (int64_t) draft.size();

                    // dflash drafter 草稿块必须立即回滚到 pos0，依据见 llama_cpp.cpp.notes.md
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

                const int64_t t_verify0 = now_ms();
                if (llama_decode(ctx, batch_tgt))
                {
                    My_Log{My_Log::Level::kError}
                        << "[LLAMACpp] " << query_type << " speculative llama_decode FAILED\n";
                    decode_failed = true;
                    break;
                }
                const int64_t t_verify1 = now_ms();
                spec_verify_ms += t_verify1 - t_verify0;
                ++spec_rounds;

                common_speculative_process(spec_.get(), batch_tgt);
                const int64_t t_readback1 = now_ms();
                spec_readback_ms += t_readback1 - t_verify1;

                auto ids = common_sampler_sample_and_accept_n(smpl, ctx, draft);
                spec_post_ms += now_ms() - t_readback1;
                common_speculative_accept(spec_.get(), kSpecSeqId, (uint16_t) (ids.size() - 1));
                n_draft_accepted_total += (int64_t) ids.size() - 1;
                if (draft_accept_hist.size() < ids.size())
                {
                    draft_accept_hist.resize(ids.size(), 0);
                }
                draft_accept_hist[ids.size() - 1]++;
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

            if (!decode_failed && eog_hit && prefix_cache_on && !is_aux_inference && (int) spec_prompt.size() == n_past)
            {
                SavePrefixCheckpoint(ctx, ctx_dft, n_past, &spec_prompt);
            }

            if (decode_failed)
            {
                prefix_cache_valid_ = false;
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

        // 投机模式下 llama.cpp 内部 perf 计数器口径失真（见 llama_cpp.md「HandleProfile()」一节），用 n/a 标注
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
                              " | draft_accepted=" + std::to_string(n_draft_accepted_total) +
                              " | spec_rounds=" + std::to_string(spec_rounds) +
                              " | draft_ms=" + std::to_string(spec_draft_ms) +
                              " | verify_ms=" + std::to_string(spec_verify_ms) +
                              " | readback_ms=" + std::to_string(spec_readback_ms) +
                              " | post_ms=" + std::to_string(spec_post_ms)) : std::string())
                 << "\n";

        if (is_aux_inference && !generated_text_buf.empty()) {
            My_Log{} << "[LLAMACpp] " << query_type
                     << " Full generated text: \"" << generated_text_buf << "\"\n";
        }

        if (is_aux_inference)
        {
            aux_ms_accum_ += total_ms;
            ++aux_calls_accum_;
        }
        else
        {
            int64_t t_query_start_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                t_query_start.time_since_epoch()).count();
            int64_t ttft_ms = (t_gen_start_ms > 0) ? (t_gen_start_ms - t_query_start_ms) : -1;
            int64_t prefill_ms = (prefill_done && t_prefill_end_ms > t_prefill_start_ms) ?
                (t_prefill_end_ms - t_prefill_start_ms) : 0;
            double prefill_tok_s = (prefill_ms > 0 && total_prefill_tokens > 0) ?
                (total_prefill_tokens * 1000.0 / prefill_ms) : 0.0;

            json metrics = json::object();
            metrics["prompt_tokens"] = (int) embd_inp.size();
            metrics["gen_tokens"] = n_generated;
            metrics["prefix_reused_tokens"] = n_reuse;
            metrics["aux_ms"] = aux_ms_accum_;
            metrics["aux_calls"] = aux_calls_accum_;
            aux_ms_accum_ = 0;
            aux_calls_accum_ = 0;
            metrics["ttft_ms"] = ttft_ms;
            metrics["prefill_ms"] = prefill_ms;
            metrics["prefill_tok_s"] = prefill_tok_s;
            metrics["decode_ms"] = gen_elapsed_ms;
            metrics["decode_tok_s"] = gen_rate;
            if (spec_)
            {
                metrics["draft_proposed"] = n_draft_proposed_total;
                metrics["draft_accepted"] = n_draft_accepted_total;
                metrics["draft_acceptance_rate"] = (n_draft_proposed_total > 0) ?
                    ((double) n_draft_accepted_total / (double) n_draft_proposed_total) : 0.0;
                metrics["draft_accept_hist"] = draft_accept_hist;
                metrics["spec_rounds"] = spec_rounds;
                metrics["spec_draft_ms"] = spec_draft_ms;
                metrics["spec_verify_ms"] = spec_verify_ms;
                metrics["spec_post_ms"] = spec_post_ms;
                metrics["spec_readback_ms"] = spec_readback_ms;
            }
            else
            {
                metrics["draft_proposed"] = nullptr;
                metrics["draft_accepted"] = nullptr;
                metrics["draft_acceptance_rate"] = nullptr;
            }
            last_trace_metrics_ = metrics;
            if (gen_rate > 0.0 && n_generated >= 16)
            {
                last_decode_tok_s_ = gen_rate;
            }
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
    llama_flash_attn_type draft_flash_attn_ = LLAMA_FLASH_ATTN_TYPE_AUTO;
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

    int max_length_ = 0;
    bool stopped_by_output_limit_ = false;

    json last_trace_metrics_ = nullptr;
    double last_decode_tok_s_ = 0.0;
    int think_time_budget_s_ = 0;
    int think_min_tokens_ = 0;
    int think_max_tokens_ = 0;
    int think_ctx_reserve_ = 0;
    int64_t aux_ms_accum_ = 0;
    int64_t aux_calls_accum_ = 0;

    struct PrefixCheckpoint
    {
        int pos = 0;
        std::vector<uint8_t> tgt;
        std::vector<uint8_t> dft;
        std::vector<uint8_t> spec;
    };

    bool prefix_cache_valid_ = false;
    std::vector<llama_token> prefix_cache_tokens_;
    std::vector<PrefixCheckpoint> prefix_ckpts_;

    void ClearPrefixCache()
    {
        prefix_cache_valid_ = false;
        prefix_cache_tokens_.clear();
        prefix_ckpts_.clear();
        n_past = 0;
        n_consumed = 0;
        if (llama_init && llama_init->context())
        {
            llama_memory_clear(llama_get_memory(llama_init->context()), false);
        }
        if (is_speculative_ && params_.speculative.draft.ctx_dft)
        {
            llama_memory_clear(llama_get_memory(params_.speculative.draft.ctx_dft), false);
        }
    }

    json GetLastQueryTraceMetrics() const
    {
        return last_trace_metrics_;
    }

    static bool NeedsPartialState(llama_context *c)
    {
        const llama_model *mdl = llama_get_model(c);
        return llama_model_is_recurrent(mdl) || llama_model_is_hybrid(mdl) || llama_model_n_swa(mdl) > 0;
    }

    static bool SaveSeqState(llama_context *c, std::vector<uint8_t> &buf)
    {
        const size_t n = llama_state_seq_get_size_ext(c, 0, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
        if (n == 0)
        {
            return false;
        }
        buf.resize(n);
        return llama_state_seq_get_data_ext(c, buf.data(), buf.size(), 0, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY) > 0;
    }

    static bool RestoreSeqState(llama_context *c, const std::vector<uint8_t> &buf)
    {
        return !buf.empty() &&
               llama_state_seq_set_data_ext(c, buf.data(), buf.size(), 0, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY) > 0;
    }

    int FindCheckpointPos(int effective_prompt_len, int n_reuse, int nth = 2) const
    {
        const llama_token bos = llama_vocab_bos(vocab);
        llama_token msg_start = LLAMA_TOKEN_NULL;
        for (int i = 0; i < effective_prompt_len; ++i)
        {
            if (embd_inp[i] != bos && llama_vocab_is_control(vocab, embd_inp[i]))
            {
                msg_start = embd_inp[i];
                break;
            }
        }
        int pos = effective_prompt_len;
        if (msg_start != LLAMA_TOKEN_NULL)
        {
            int found = 0;
            for (int i = effective_prompt_len - 1; i > n_reuse; --i)
            {
                if (embd_inp[i] == msg_start)
                {
                    ++found;
                    if (found == nth)
                    {
                        pos = i;
                        break;
                    }
                }
            }
        }
        return pos > n_reuse ? pos : -1;
    }

    std::vector<int> FindHeadCheckpointPos(int effective_prompt_len, int n_reuse, int tail_pos) const
    {
        std::vector<int> out;
        const int n_head = g_llama_prefix_head_checkpoints.load();
        if (n_head <= 0 || tail_pos <= 0)
        {
            return out;
        }
        const llama_token bos = llama_vocab_bos(vocab);
        llama_token msg_start = LLAMA_TOKEN_NULL;
        int first = -1;
        for (int i = 0; i < effective_prompt_len; ++i)
        {
            if (embd_inp[i] != bos && llama_vocab_is_control(vocab, embd_inp[i]))
            {
                msg_start = embd_inp[i];
                first = i;
                break;
            }
        }
        if (msg_start == LLAMA_TOKEN_NULL)
        {
            return out;
        }
        int seen = 0;
        for (int i = first + 1; i < tail_pos && seen < n_head; ++i)
        {
            if (embd_inp[i] == msg_start)
            {
                ++seen;
                if (i > n_reuse)
                {
                    out.push_back(i);
                }
            }
        }
        return out;
    }

    void SavePrefixCheckpoint(llama_context *c, llama_context *c_dft, int pos, const llama_tokens *tokens = nullptr)
    {
        PrefixCheckpoint ck;
        ck.pos = pos;
        bool ok = !NeedsPartialState(c) || SaveSeqState(c, ck.tgt);
        if (ok && c_dft)
        {
            ok = !NeedsPartialState(c_dft) || SaveSeqState(c_dft, ck.dft);
        }
        if (!ok)
        {
            My_Log{My_Log::Level::kWarning} << "[LLAMACpp] Prefix checkpoint save failed at pos=" << pos << "\n";
            return;
        }
        if (spec_ && !common_speculative_get_state(spec_.get(), 0, ck.spec))
        {
            ck.spec.clear();
        }
        My_Log{} << "[LLAMACpp] Prefix checkpoint saved: pos=" << pos
                 << " | tgt_bytes=" << ck.tgt.size() << " | dft_bytes=" << ck.dft.size() << "\n";
        prefix_ckpts_.push_back(std::move(ck));
        const size_t n_keep_head = (size_t) std::max(0, g_llama_prefix_head_checkpoints.load());
        if (prefix_ckpts_.size() > n_keep_head + 3)
        {
            prefix_ckpts_.erase(prefix_ckpts_.begin() + (long long) n_keep_head);
        }
        if (tokens)
            prefix_cache_tokens_.assign(tokens->begin(), tokens->begin() + pos);
        else
            prefix_cache_tokens_.assign(embd_inp.begin(), embd_inp.begin() + pos);
        prefix_cache_valid_ = true;
    }

    int MatchPrefixCache(llama_context *c, llama_context *c_dft, int limit)
    {
        const bool partial_tgt = NeedsPartialState(c);
        const bool partial_dft = c_dft && NeedsPartialState(c_dft);
        const int n_cached = (int) prefix_cache_tokens_.size();
        int n = 0;
        const PrefixCheckpoint *hit = nullptr;
        if (partial_tgt || partial_dft)
        {
            for (auto it = prefix_ckpts_.rbegin(); it != prefix_ckpts_.rend(); ++it)
            {
                if (it->pos > 0 && it->pos <= limit && it->pos <= n_cached &&
                    std::equal(prefix_cache_tokens_.begin(), prefix_cache_tokens_.begin() + it->pos, embd_inp.begin()))
                {
                    n = it->pos;
                    hit = &*it;
                    break;
                }
            }
        }
        else
        {
            const int n_cmp = std::min(n_cached, limit);
            while (n < n_cmp && prefix_cache_tokens_[n] == embd_inp[n])
            {
                ++n;
            }
        }
        if (n == 0)
        {
            const int n_cmp = std::min(n_cached, limit);
            int common = 0;
            while (common < n_cmp && prefix_cache_tokens_[common] == embd_inp[common])
            {
                ++common;
            }
            My_Log{My_Log::Level::kWarning}
                << "[LLAMACpp] Prefix cache miss: mode="
                << ((partial_tgt || partial_dft) ? "partial" : "incremental")
                << ", n_cached=" << n_cached << ", limit=" << limit
                << ", common_prefix=" << common << "\n";
            return 0;
        }
        bool ok = !partial_tgt || RestoreSeqState(c, hit->tgt);
        ok = ok && llama_memory_seq_rm(llama_get_memory(c), 0, n, -1);
        if (ok && c_dft)
        {
            ok = !partial_dft || RestoreSeqState(c_dft, hit->dft);
            ok = ok && llama_memory_seq_rm(llama_get_memory(c_dft), 0, n, -1);
        }
        if (!ok)
        {
            My_Log{My_Log::Level::kWarning} << "[LLAMACpp] Prefix cache restore failed, full prefill\n";
            return 0;
        }
        if (spec_ && hit && !hit->spec.empty())
        {
            common_speculative_set_state(spec_.get(), 0, hit->spec);
        }
        if (n < n_cached)
        {
            My_Log{} << "[LLAMACpp] Prefix cache fallback hit: pos=" << n << " (latest=" << n_cached << ")\n";
        }
        prefix_ckpts_.erase(std::remove_if(prefix_ckpts_.begin(), prefix_ckpts_.end(),
                                           [n](const PrefixCheckpoint &ck) { return ck.pos > n; }),
                            prefix_ckpts_.end());
        const size_t n_keep_head = (size_t) std::max(0, g_llama_prefix_head_checkpoints.load());
        if (prefix_ckpts_.size() > n_keep_head + 1)
        {
            prefix_ckpts_.erase(prefix_ckpts_.begin() + (long long) n_keep_head, prefix_ckpts_.end() - 1);
        }
        prefix_cache_tokens_.resize(n);
        return n;
    }

private:
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

    // 投机检测必须在构造 argv 之前完成；LLAMA_EXAMPLE_SERVER 切换的完整依据见 llama_cpp.cpp.notes.md。
    llama_speculative::DraftModelConfig draft_config;
    int configured_n_batch = 0;
    int configured_n_ubatch = 0;
    std::vector<std::string> configured_extra_args;
    try
    {
        const std::string &config_path = model_config_.i_model_config_.get_config_path();
        if (!config_path.empty() && File::IsFileExist(config_path) && !File::IsFileEmpty(config_path))
        {
            std::ifstream config_stream(config_path);
            json model_json;
            config_stream >> model_json;
            draft_config = llama_speculative::DraftModelConfig::ParseFrom(model_json);
            if (model_json.is_object() && model_json.contains("llama") && model_json["llama"].is_object())
            {
                const json &llama_block = model_json["llama"];
                if (llama_block.contains("n_batch") && llama_block["n_batch"].is_number_integer())
                {
                    configured_n_batch = llama_block["n_batch"].get<int>();
                }
                if (llama_block.contains("n_ubatch") && llama_block["n_ubatch"].is_number_integer())
                {
                    configured_n_ubatch = llama_block["n_ubatch"].get<int>();
                }
                if (llama_block.contains("extra_args") && llama_block["extra_args"].is_array())
                {
                    for (const auto &arg: llama_block["extra_args"])
                    {
                        if (arg.is_string())
                        {
                            configured_extra_args.push_back(arg.get<std::string>());
                        }
                    }
                }
            }
        }
    }
    catch (...)
    {
        My_Log{My_Log::Level::kDebug}
            << "[LLAMACpp] Failed to read optional draft_model field from model config.json, ignored\n";
    }
    const bool speculative_requested = draft_config.present && !draft_config.path.empty();

    // 该后端只看变量是否存在、不看取值；在本机实际未生效的原因见 llama_cpp.cpp.notes.md。
#if defined(_WIN32)
    _putenv_s("GGML_OPENCL_ADRENO_USE_LARGE_BUFFER", "1");
#else
    setenv("GGML_OPENCL_ADRENO_USE_LARGE_BUFFER", "1", 1);
#endif
    My_Log{} << "[Env] GGML_OPENCL_ADRENO_USE_LARGE_BUFFER=1 set\n";

    size_t configured_context_size = model_config_.get_context_size();
    int n_ctx;
    if (speculative_requested)
    {
        n_ctx = configured_context_size > 0 ? static_cast<int>(configured_context_size) : 32768;
    }
    else
    {
        n_ctx = configured_context_size > 0 ? static_cast<int>(configured_context_size) : 8192;
    }

    std::string device = model_config_.get_device();
    std::transform(device.begin(), device.end(), device.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

    const llama_runtime::RuntimePlan *plan =
        (speculative_requested && model_config_.has_runtime_plan()) ? &model_config_.runtime_plan()
                                                                    : nullptr;
    if (plan)
    {
        if (plan->ctx_effective > 0 && static_cast<int>(plan->ctx_effective) != n_ctx)
        {
            My_Log{My_Log::Level::kWarning}
                << "[LLAMACppBuilder] context_size clamped by planner: " << n_ctx << " -> "
                << plan->ctx_effective << std::endl;
            n_ctx = static_cast<int>(plan->ctx_effective);
        }
        My_Log{My_Log::Level::kWarning} << "[LLAMACppBuilder] [Planner] " << plan->rationale
                                        << std::endl;
    }

    auto parse_flash_attn = [](const std::string &value, llama_flash_attn_type fallback) {
        if (value == "on")
        {
            return LLAMA_FLASH_ATTN_TYPE_ENABLED;
        }
        if (value == "off")
        {
            return LLAMA_FLASH_ATTN_TYPE_DISABLED;
        }
        return fallback;
    };
    const llama_flash_attn_type draft_flash_attn =
            plan ? (llama_runtime::FlashAttnEnabled(plan->fa_draft) ? LLAMA_FLASH_ATTN_TYPE_ENABLED
                                                                    : LLAMA_FLASH_ATTN_TYPE_DISABLED)
                 : parse_flash_attn(draft_config.draft_flash_attn, LLAMA_FLASH_ATTN_TYPE_AUTO);

    std::vector<std::string> arg_strings = {
        "GenieService.exe",
        "--model", gguf_path,
        "--ctx-size", std::to_string(n_ctx),
        "--no-warmup",
        "-s", "42",
    };
    if (plan)
    {
        arg_strings.insert(arg_strings.end(),
                           {"-fa", llama_runtime::FlashAttnEnabled(plan->fa_target) ? "on" : "off"});
    }
    else if (!draft_config.flash_attn.empty())
    {
        arg_strings.insert(arg_strings.end(), {"-fa", draft_config.flash_attn});
    }
    if (!speculative_requested)
    {
        arg_strings.insert(arg_strings.end(), {"--fit", "off"});

        arg_strings.push_back("-no-cnv");
    }
    if (speculative_requested)
    {
        if (model_config_.is_thinking_model())
        {
            arg_strings.insert(arg_strings.end(), {"--reasoning-budget", "3072"});
        }

        fs::path draft_path = fs::path(model_config_.get_model_path()) / draft_config.path;
        arg_strings.insert(arg_strings.end(), {"-md", draft_path.string()});
        // ngram-mod 是 draftless 的 n-gram 查表 drafter，优先于 dflash 出手、约 16 MB 纯 CPU 哈希池、
        // 不占显存。32G/X2-90 实测（同一热稳态、背靠背五格、生成逐字相同）：接受率 0.6455 -> 0.6816
        // 且提案数 959 -> 917，即分子升、分母降（真提升而非压分母），gen_rate 16.2 -> 17.0 tok/s。
        // 故默认追加，而不是要求每个模型 config.json 自己写全；写 "ngram_mod": false 可关闭。
        std::string spec_type = draft_config.spec_type;
        if (draft_config.ngram_mod && !spec_type.empty() &&
            spec_type.find("ngram-mod") == std::string::npos)
        {
            spec_type += ",ngram-mod";
            My_Log{My_Log::Level::kWarning}
                << "[LLAMACppBuilder] ngram-mod appended to spec_type: " << spec_type << std::endl;
        }
        if (!spec_type.empty())
        {
            arg_strings.insert(arg_strings.end(), {"--spec-type", spec_type});
        }
        if (draft_config.spec_draft_n_max > 0)
        {
            arg_strings.insert(arg_strings.end(),
                                {"--spec-draft-n-max", std::to_string(draft_config.spec_draft_n_max)});
        }
        arg_strings.insert(arg_strings.end(),
                            {"--spec-draft-p-min", std::to_string(draft_config.spec_draft_p_min)});
        const int draft_n_gpu_layers = model_config_.is_speculative_draft_forced_cpu()
                                           ? 0
                                           : draft_config.n_gpu_layers;
        if (device != "cpu" && draft_n_gpu_layers >= 0)
        {
            arg_strings.insert(arg_strings.end(), {"-ngld", std::to_string(draft_n_gpu_layers)});
        }
        int fit_target_mib = draft_config.fit_target_mib;
        // -fitt 在两条路径上都是空操作，所以规划器接管后不再自动拼它（D6）：
        //   - 拆分路径有内置 -ot ⇒ common_fit_params 报 "tensor_buft_overrides already set by
        //     user, abort"，显存拟合直接放弃（exp3 实测）；
        //   - 全 GPU 路径显式给 -ngl 99 ⇒ fork 的 fit.cpp 放弃拟合。
        // 继续拼只会让 ARGV 产生「它在起作用」的误导。显式配置 fit_target_mib 仍然照拼。
        if (plan && fit_target_mib < 0)
        {
            My_Log{My_Log::Level::kWarning}
                << "[LLAMACppBuilder] -fitt omitted: no-op on both paths (built-in -ot aborts "
                   "common_fit_params; explicit -ngl makes fit.cpp give up)" << std::endl;
        }
        else if (fit_target_mib < 0 && device != "cpu" && draft_n_gpu_layers != 0)
        {
            std::error_code size_ec;
            const auto draft_bytes = fs::file_size(draft_path, size_ec);
            if (!size_ec && draft_bytes > 0)
            {
                constexpr uint64_t kBytesPerMib = 1ULL << 20;
                constexpr int kLlamaFitTargetDefaultMib = 1024;
                const int draft_weight_mib =
                    static_cast<int>((draft_bytes + kBytesPerMib - 1) / kBytesPerMib);
                fit_target_mib = kLlamaFitTargetDefaultMib + draft_weight_mib;
                My_Log{My_Log::Level::kWarning}
                    << "[LLAMACppBuilder] -fitt auto: " << kLlamaFitTargetDefaultMib << " + draft weights "
                    << draft_weight_mib << " MiB = " << fit_target_mib << std::endl;
            }
        }
        if (fit_target_mib >= 0)
        {
            arg_strings.insert(arg_strings.end(), {"-fitt", std::to_string(fit_target_mib)});
        }
        if (!draft_config.samplers.empty())
        {
            arg_strings.insert(arg_strings.end(), {"--samplers", draft_config.samplers});
        }
        if (draft_config.temp >= 0.0f)
        {
            arg_strings.insert(arg_strings.end(), {"--temp", std::to_string(draft_config.temp)});
        }
        if (draft_config.top_k >= 0)
        {
            arg_strings.insert(arg_strings.end(), {"--top-k", std::to_string(draft_config.top_k)});
        }
        if (draft_config.top_p >= 0.0f)
        {
            arg_strings.insert(arg_strings.end(), {"--top-p", std::to_string(draft_config.top_p)});
        }
        if (draft_config.ngram_mod_n_min >= 0)
        {
            arg_strings.insert(arg_strings.end(),
                                {"--spec-ngram-mod-n-min", std::to_string(draft_config.ngram_mod_n_min)});
        }
        if (draft_config.ngram_mod_n_match >= 0)
        {
            arg_strings.insert(arg_strings.end(),
                                {"--spec-ngram-mod-n-match", std::to_string(draft_config.ngram_mod_n_match)});
        }
    }

    constexpr int kDefaultBatch = 2048;
    constexpr int kDefaultUbatch = 512;
    // 优先级：llama 块里显式配置的 n_batch/n_ubatch > 规划器推导值 > 历史默认值。
    // 规划器目前对这两项给的就是 2048/512（与历史默认一致），留这条通路是为了让以后按档位调
    // ubatch（例如长上下文用 -ub 256 把 kq 压回设备单次分配上限内）只改规划器一处。
    int n_batch = configured_n_batch > 0 ? configured_n_batch
                  : (plan ? static_cast<int>(plan->n_batch) : kDefaultBatch);
    int n_ubatch = configured_n_ubatch > 0 ? configured_n_ubatch
                   : (plan ? static_cast<int>(plan->n_ubatch) : kDefaultUbatch);
    if (configured_n_batch < 0 || configured_n_ubatch < 0)
    {
        My_Log{My_Log::Level::kWarning} << "[LLAMACppBuilder] llama.n_batch=" << configured_n_batch
                                      << " / llama.n_ubatch=" << configured_n_ubatch
                                      << " must be positive; negative values replaced by defaults "
                                      << kDefaultBatch << " / " << kDefaultUbatch << "." << std::endl;
    }
    if (n_batch > n_ctx)
    {
        My_Log{My_Log::Level::kWarning} << "[LLAMACppBuilder] n_batch=" << n_batch << " exceeds n_ctx=" << n_ctx
                                      << "; clamping n_batch to n_ctx." << std::endl;
        n_batch = n_ctx;
    }
    if (n_ubatch > n_batch)
    {
        My_Log{My_Log::Level::kWarning} << "[LLAMACppBuilder] n_ubatch=" << n_ubatch << " exceeds n_batch=" << n_batch
                                      << "; clamping n_ubatch to n_batch." << std::endl;
        n_ubatch = n_batch;
    }
    if (model_config_.is_speculative_reduced_ubatch() && n_ubatch > 1)
    {
        const int reduced = n_ubatch / 2;
        My_Log{My_Log::Level::kWarning} << "[LLAMACppBuilder] reduced ubatch requested: n_ubatch " << n_ubatch
                                      << " -> " << reduced << std::endl;
        n_ubatch = reduced;
    }
    arg_strings.insert(arg_strings.end(), {"-b", std::to_string(n_batch), "-ub", std::to_string(n_ubatch)});
    My_Log{My_Log::Level::kInfo} << "[LLAMACppBuilder] n_batch=" << n_batch << ", n_ubatch=" << n_ubatch
                                  << (configured_n_batch > 0 || configured_n_ubatch > 0 ? " (from config llama block)" : " (defaults)")
                                  << std::endl;
    if (device == "cpu")
    {
        // CPU 路径：保持 mmap/repack/no-host 的 llama.cpp 官方默认值（已实测验证比强制关闭更快）。
        arg_strings.insert(arg_strings.end(), {"-ngl", "0"});
    }
    else if (!speculative_requested)
    {
        // 非投机（现有全部单模型 GGUF）路径：保持历史 A/B 实测出的最快组合，本轮不改动。
        arg_strings.insert(arg_strings.end(), {
            "--device", "GPUOpenCL",
            "-ngl", "99",
            "--no-mmap",
            "--no-repack",
            "--no-host",
        });
    }
    if (device != "cpu")
    {
        const bool user_ot = std::any_of(configured_extra_args.begin(), configured_extra_args.end(),
                                         [](const std::string &a) { return a == "-ot" || a == "--override-tensor"; });
        if (speculative_requested && model_config_.is_speculative_full_gpu())
        {
            arg_strings.insert(arg_strings.end(), {"-ngl", "99"});
        }
        else if (speculative_requested && !user_ot && draft_config.spec_type.find("dflash") != std::string::npos)
        {
            // 层数只有两个来源：配置显式给的 ffn_cpu_layers，或内置默认值。不要在这里按显存余量反算
            // ——该路径已被真机实测判死（28 层能加载、前 4 个请求正常，第 5 个请求存前缀检查点时
            // clCreateBuffer err=-5 直接 GGML_ASSERT 崩溃），依据见 llama_cpp.cpp.notes.md 对应章节。
            const uint32_t block_count = llama_speculative::ReadGgufBlockCount(gguf_path);
            const uint32_t cpu_layers = draft_config.ffn_cpu_layers >= 0
                                            ? static_cast<uint32_t>(draft_config.ffn_cpu_layers)
                                            : (plan ? plan->ffn_cpu_layers
                                                    : llama_speculative::kDflashFfnCpuLayersDefault);
            const std::string ot = llama_speculative::BuildDflashFfnCpuOverride(block_count, cpu_layers);
            My_Log{My_Log::Level::kWarning}
                << "[LLAMACppBuilder] ffn cpu layers: " << cpu_layers
                << (draft_config.ffn_cpu_layers >= 0 ? " (from config)" : " (default)")
                << ", blocks=" << block_count << std::endl;
            if (!ot.empty())
            {
                arg_strings.insert(arg_strings.end(), {"-ot", ot});
            }
        }
        arg_strings.insert(arg_strings.end(), configured_extra_args.begin(), configured_extra_args.end());
    }
    // 投机解码分支（非 CPU）不显式传 -ngl：llama-completion.exe 实测确认，一旦显式给出 -ngl（含
    // 99），fork 的 common/fit.cpp 会直接放弃显存拟合、强行按请求值全量分配，而本机 OpenCL 设备
    // 可用显存（约15078MiB）不足以装下 Qwen3.8-27B 全部66层（需约15234MiB），导致最后几批 q6_K
    // 张量上传阶段崩溃。不给 -ngl 时 --fit 自动决定 offload 层数（实测58-61/66），加载与推理均
    // 成功，且 prefill 速度最快；--fit 同样会把 common_fit_extra_model（草稿模型）计入显存预算，
    // -ngld 99 不受影响。不带 --device 的原因同上：裁剪掉 CPU 设备后超设备上限的 buffer 无处 fallback。

    {
        std::string argv_dump;
        for (const auto &s: arg_strings)
        {
            argv_dump += s + " ";
        }
        My_Log{} << "[LLAMACpp] ARGV: " << argv_dump << "\n";
    }

    std::vector<char *> argv;
    argv.reserve(arg_strings.size());
    for (auto &s: arg_strings)
    {
        argv.push_back(s.data());
    }

    common_params params;
    const enum llama_example example = speculative_requested ? LLAMA_EXAMPLE_SERVER : LLAMA_EXAMPLE_COMPLETION;
    if (!common_params_parse((int) argv.size(), argv.data(), params, example, nullptr))
    {
        throw std::runtime_error("common param parse failed");
    }

    params.verbosity = 0;           // 禁用详细日志

    if (params.n_parallel < 0)
    {
        params.n_parallel = 1;
        params.kv_unified = true;
    }

    const bool skip_thread_override = plan && plan->full_gpu && draft_config.n_threads <= 0 &&
                                      draft_config.n_threads_batch <= 0;
    if (skip_thread_override)
    {
        My_Log{My_Log::Level::kWarning}
            << "[LLAMACppBuilder] -t/-tb not set: full GPU placement, no resident CPU weights"
            << std::endl;
    }
    else
    {
        const auto hw_threads = static_cast<int32_t>(std::thread::hardware_concurrency());
        params.cpuparams.n_threads = hw_threads > 0 ? hw_threads : 4;
        if (speculative_requested && draft_config.n_threads > 0)
        {
            params.cpuparams.n_threads = draft_config.n_threads;
        }
        params.cpuparams_batch.n_threads = (speculative_requested && draft_config.n_threads_batch > 0)
                                               ? draft_config.n_threads_batch
                                               : params.cpuparams.n_threads;
    }

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

    impl_ = new Impl{std::move(params), draft_flash_attn};
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
    else if (prompt.find("You are a compression assistant") != std::string::npos)
    {
        query_type_label = "SUMMARIZATION";
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
    impl_->think_time_budget_s_ = model_input.think_time_budget_s_;
    impl_->think_min_tokens_ = model_input.think_min_tokens_;
    impl_->think_max_tokens_ = model_input.think_max_tokens_;
    impl_->think_ctx_reserve_ = model_input.think_ctx_reserve_;
    bool result = impl_->Query(prompt, callback, prefill_heartbeat,
                                query_type_bracketed.c_str(), is_aux_inference);
    stopped_by_output_limit_ = impl_->stopped_by_output_limit_;
    return result;
}

LLAMACppBuilder::~LLAMACppBuilder()
{
    delete impl_;
    impl_ = nullptr;
}

void LLAMACppBuilder::SetPrefixCacheEnabled(bool enabled)
{
    g_llama_prefix_cache_enabled.store(enabled);
}

void LLAMACppBuilder::SetPrefixHeadCheckpoints(int n, bool from_config)
{
    g_llama_prefix_head_checkpoints.store(n);
    if (from_config)
    {
        g_llama_prefix_head_checkpoints_explicit.store(true);
    }
}

bool LLAMACppBuilder::ApplyPlannedHeadCheckpoints(int n)
{
    // 显式配置永远胜出。检查点常驻在 host 侧（prefix_ckpts_ 是 std::vector<uint8_t>，常驻上限
    // head_checkpoints + 3 份），所以它的份数由主机可用内存推导、而不是显存；r23 实测
    // head_checkpoints=2 的 prefill 收益测不出（1310 vs 1311 s），即 host 紧张时降档是零代价的。
    if (g_llama_prefix_head_checkpoints_explicit.load())
    {
        return false;
    }
    g_llama_prefix_head_checkpoints.store(n);
    return true;
}

bool LLAMACppBuilder::UsesCrossRequestPrefixCache() const
{
    return g_llama_prefix_cache_enabled.load();
}

void LLAMACppBuilder::ClearConversationState()
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    if (impl_)
    {
        impl_->ClearPrefixCache();
    }
}

json LLAMACppBuilder::GetLastQueryTraceMetrics() const
{
    return impl_ ? impl_->GetLastQueryTraceMetrics() : json(nullptr);
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
    
    if (impl_ && impl_->last_trace_metrics_.is_object())
    {
        const json &m = impl_->last_trace_metrics_;
        auto fmt = [](double v)
        {
            std::ostringstream o;
            o << std::fixed << std::setprecision(2) << v;
            return o.str();
        };
        int64_t ttft_ms = m.value("ttft_ms", (int64_t) 0);
        result["time_to_first_token"] = fmt(ttft_ms > 0 ? ttft_ms / 1000.0 : 0.0);
        result["token_generation_time"] = fmt(m.value("decode_ms", (int64_t) 0) / 1000.0);
        result["prompt_processing_rate"] = fmt(m.value("prefill_tok_s", 0.0));
        result["token_generation_rate"] = fmt(m.value("decode_tok_s", 0.0));
        result["num_prompt_tokens"] = m.value("prompt_tokens", 0);
        result["num_generated_tokens"] = m.value("gen_tokens", 0);
        return result;
    }

    if (impl_)
    {
        llama_context *ctx = impl_->llama_init->context();
        if (!ctx)
        {
            return result;
        }
        
        llama_perf_context_data perf_ctx = llama_perf_context(ctx);
        
        std::ostringstream oss;
        
        oss << std::fixed << std::setprecision(2)
            << perf_ctx.t_p_eval_ms / 1000.0;
        result["time_to_first_token"] = oss.str();
        
        oss.str("");
        oss << std::fixed << std::setprecision(2)
            << perf_ctx.t_eval_ms / 1000.0;
        result["token_generation_time"] = oss.str();
        
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
    llama_context *ctx = impl_->llama_init->context();
    if (ctx == nullptr)
    {
        return 0;
    }

    const auto tokens = common_tokenize(ctx, text, /* add_special = */ false, /* parse_special = */ true);
    return tokens.size();
}
