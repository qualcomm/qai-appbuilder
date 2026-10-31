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
#include "degenerate_repeat_guard.h"
#include "think_budget_floor.h"
#include "watermark_provider_host.h"
#include "llama_watermark_sampler.h"
#include <filesystem>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <cctype>
#include <thread>
#include <chrono>
#include <atomic>
#include <cstddef>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

static std::atomic<bool> g_llama_prefix_cache_enabled{true};
static std::atomic<int> g_llama_prefix_head_checkpoints{2};
static std::atomic<bool> g_llama_prefix_head_checkpoints_explicit{false};
static std::atomic<bool> g_degenerate_repeat_guard_enabled{true};
static std::atomic<int> g_degenerate_repeat_guard_max_period{48};
static std::mutex g_llama_backend_mutex;
static std::atomic<int> g_llama_backend_refs{0};

static inline int64_t now_ms()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

class LlamaBackendRef
{
public:
    LlamaBackendRef() = default;

    LlamaBackendRef(const LlamaBackendRef &) = delete;

    LlamaBackendRef &operator=(const LlamaBackendRef &) = delete;

    void Acquire()
    {
        std::lock_guard<std::mutex> lk(g_llama_backend_mutex);
        if (!acquired_)
        {
            if (g_llama_backend_refs.fetch_add(1) == 0)
            {
                llama_backend_init();
            }
            acquired_ = true;
        }
    }

    ~LlamaBackendRef()
    {
        if (!acquired_)
        {
            return;
        }
        std::lock_guard<std::mutex> lk(g_llama_backend_mutex);
        if (g_llama_backend_refs.fetch_sub(1) == 1)
        {
            llama_backend_free();
        }
    }

private:
    bool acquired_{false};
};

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

        backend_ref_.Acquire();
        llama_numa_init(params_.numa);

        const auto main_init_start = std::chrono::steady_clock::now();
        llama_init = common_init_from_params(params_);
        My_Log{} << "[LLAMACpp][timing] stage=main_model_init ms="
                 << std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - main_init_start).count() << "\n";
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

        llama_perf_context_reset(ctx);

        if (!llama_model_has_encoder(model))
        {
            GGML_ASSERT(!llama_vocab_get_add_eos(vocab));
        }

        is_speculative_ = !params_.speculative.draft.mparams.path.empty();
        if (is_speculative_)
        {
            common_params params_dft = common_base_params_to_speculative(params_);

            params_dft.flash_attn_type = draft_flash_attn_;
            My_Log{} << "[LLAMACpp] draft flash_attn: "
                     << llama_flash_attn_type_name(params_dft.flash_attn_type)
                     << " (target: " << llama_flash_attn_type_name(params_.flash_attn_type) << ")\n";
            const auto draft_init_start = std::chrono::steady_clock::now();
            draft_init_ = common_speculative_init_from_params(params_dft, model, ctx);
            My_Log{} << "[LLAMACpp][timing] stage=draft_model_init ms="
                     << std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::steady_clock::now() - draft_init_start).count() << "\n";
            if (!draft_init_ || !draft_init_->model() || !draft_init_->context())
            {
                throw std::runtime_error("unable to load draft model for speculative decoding");
            }

            params_.speculative.draft.ctx_tgt = ctx;
            params_.speculative.draft.ctx_dft = draft_init_->context();

            const auto speculative_state_start = std::chrono::steady_clock::now();
            spec_.reset(common_speculative_init(params_.speculative, /*n_seq=*/1));
            My_Log{} << "[LLAMACpp][timing] stage=speculative_state_init ms="
                     << std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::steady_clock::now() - speculative_state_start).count() << "\n";
            if (!spec_)
            {
                throw std::runtime_error("failed to initialize speculative decoding state");
            }

            My_Log{} << "[LLAMACpp] Speculative decoding ENABLED: draft_model="
                     << params_.speculative.draft.mparams.path
                     << ", n_max=" << params_.speculative.draft.n_max
                     << ", p_min=" << params_.speculative.draft.p_min
                     << ", n_gpu_layers=" << params_.speculative.draft.n_gpu_layers << "\n";

            const auto reserve_start = std::chrono::steady_clock::now();
            ReserveWorstCaseGraph(ctx);
            My_Log{} << "[LLAMACpp][timing] stage=reserve_worst_case_graph ms="
                     << std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::steady_clock::now() - reserve_start).count() << "\n";
        }
    }

    void ReserveWorstCaseGraph(llama_context *ctx)
    {
        auto log_gpu_memory = [](const char *phase)
        {
            ggml_backend_dev_t gpu = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_GPU);
            if (!gpu)
            {
                My_Log{} << "[LLAMACpp][memory] phase=" << phase << " gpu=unavailable\n";
                return;
            }
            size_t free = 0;
            size_t total = 0;
            ggml_backend_dev_memory(gpu, &free, &total);
            My_Log{} << "[LLAMACpp][memory] phase=" << phase
                     << " free_bytes=" << free << " total_bytes=" << total << "\n";
        };
        log_gpu_memory("reserve_before");
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
        log_gpu_memory("reserve_after");
    }

    bool Query(const std::string &prompt,
               const std::function<bool(std::string &)> &callback,
               const std::function<bool()> &prefill_heartbeat,
               const char *query_type, bool is_aux_inference,
               const std::atomic<uint64_t> &stop_epoch, uint64_t entry_epoch)
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
        const auto is_stop_requested = [&stop_epoch, entry_epoch]()
        {
            return stop_epoch.load() != entry_epoch;
        };

        stopped_by_output_limit_ = false;
        if (!is_aux_inference)
        {
            std::lock_guard<std::mutex> metrics_lock(trace_metrics_mutex_);
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

        const bool add_bos = llama_vocab_get_add_bos(vocab) && !params_.use_jinja;
        embd_inp = common_tokenize(ctx, prompt, true, true);

        if (embd_inp.empty())
        {

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
        std::string generated_text_buf;
        int64_t n_draft_proposed_total = 0;
        int64_t n_draft_accepted_total = 0;
        std::vector<int64_t> draft_accept_hist;
        int64_t spec_draft_ms = 0;
        int64_t spec_verify_ms = 0;
        int64_t spec_post_ms = 0;
        int64_t spec_readback_ms = 0;
        int64_t spec_rounds = 0;

        auto t_last_prefill_heartbeat = std::chrono::steady_clock::now();
        constexpr int64_t kPrefillHeartbeatIntervalMs = 5000;

        Utf8StreamProcessor token_utf8_processor([&](std::string &complete_chars)
                                                  {
                                                      if (!callback(complete_chars))
                                                      {
                                                          should_stop = true;
                                                      }
                                                  });

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

            const int64_t pre_floor_budget = std::max<int64_t>(0, std::min(budget, ctx_left));
            think_budget_tokens = think_budget_floor::ApplyThinkBudgetFloor(
                budget, ctx_left, think_min_tokens_, think_force_min_floor_);
            think_in_progress = true;
            think_token_start = 0;
            static const std::string kOpenThink = "<think>\n";
            generated_text_buf += kOpenThink;
            token_utf8_processor.processStream(kOpenThink.data(), kOpenThink.size());
            My_Log{} << "[LLAMACpp] " << query_type << " adaptive think budget=" << think_budget_tokens
                     << " (tg_rate=" << tg_rate << ", time_budget_s=" << think_time_budget_s_
                     << ", ctx_left=" << ctx_left << ")\n";
            if (think_force_min_floor_ && think_budget_tokens > pre_floor_budget)
            {
                My_Log{My_Log::Level::kInfo}
                    << "[LLAMACpp] " << query_type
                    << " forced-unblock think budget floor applied: would be clamped to "
                    << pre_floor_budget << ", floored to " << think_budget_tokens
                    << " (本轮由限流死锁兜底强制放行，给予最小保底思考预算)\n";
            }
        }

        auto emit_generated_token = [&](llama_token id) -> bool
        {
            std::string token_str = common_token_to_piece(ctx, id, params_.special);

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

        degenerate_repeat_guard::StreamGuard repeat_guard(
                g_degenerate_repeat_guard_enabled.load(),
                std::max(0, g_degenerate_repeat_guard_max_period.load()));
        std::vector<std::pair<llama_token, int>> guard_pending;
        bool degenerate_loop_hit = false;

        auto flush_guard_pending = [&]() -> bool
        {
            for (const auto &pending : guard_pending)
            {
                if (!emit_generated_token(pending.first))
                {
                    guard_pending.clear();
                    return false;
                }
            }
            guard_pending.clear();
            return true;
        };

        auto guarded_emit = [&](llama_token id, int kv_pos) -> bool
        {

            const bool terminal = (params_.n_predict > 0 && n_remain <= 0)
                                  || llama_vocab_is_eog(vocab, id);
            const auto decision = repeat_guard.Observe(static_cast<int32_t>(id), terminal);
            switch (decision.action)
            {
                case degenerate_repeat_guard::Action::kEmit:
                    return emit_generated_token(id);
                case degenerate_repeat_guard::Action::kHold:
                    guard_pending.emplace_back(id, kv_pos);
                    return true;
                case degenerate_repeat_guard::Action::kFlush:
                    guard_pending.emplace_back(id, kv_pos);
                    return flush_guard_pending();
                case degenerate_repeat_guard::Action::kAbort:
                {
                    guard_pending.emplace_back(id, kv_pos);
                    const int rollback_pos = guard_pending.front().second;
                    guard_pending.clear();
                    n_past = rollback_pos;
                    if (!is_speculative_)
                    {
                        llama_memory_seq_rm(mem, 0, rollback_pos, -1);
                    }
                    degenerate_loop_hit = true;
                    should_stop = true;
                    My_Log{My_Log::Level::kWarning}
                        << "[LLAMACpp] " << query_type
                        << " Degenerate repeat guard stopped period=" << decision.period
                        << " rollback_pos=" << rollback_pos << "\n";
                    return false;
                }
            }
            return true;
        };

        while (n_remain != 0)
        {
            if (is_stop_requested())
            {
                should_stop = true;
                My_Log{My_Log::Level::kWarning}
                    << "[LLAMACpp] " << query_type << " Query interrupted by Stop() after "
                    << n_generated << " tokens\n";
                break;
            }
            if (!embd.empty())
            {
                int max_embd_size = n_ctx - 4;
                if ((int) embd.size() > max_embd_size)
                {
                    embd.resize(max_embd_size);
                }

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

                if (!prefill_done && t_prefill_start_ms == 0)
                {
                    t_prefill_start_ms = now_ms();
                }

                if (llama_decode(ctx, llama_batch_get_one(embd.data(), (int) embd.size())))
                {
                    My_Log{My_Log::Level::kError} << "[LLAMACpp] " << query_type << " llama_decode FAILED\n";
                    prefix_cache_valid_ = false;
                    {
                        std::lock_guard<std::mutex> lk(m);
                        done = true;
                    }
                    cv.notify_all();
                    return false;
                }

                if (is_speculative_)
                {
                    common_batch batch_chunk(ctx);
                    for (size_t i = 0; i < embd.size(); ++i)
                    {
                        batch_chunk.add(embd[i], (llama_pos) (n_past + (int) i), {0}, false);
                    }
                    common_speculative_process(spec_.get(), batch_chunk);
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

            if (is_generated_token)
            {
                for (auto id: embd)
                {
                    if (!guarded_emit(id, n_past))
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
            common_batch spec_batch_tgt(ctx);
            auto add_batch_token = [](llama_batch &batch, llama_token id, llama_pos pos, llama_seq_id seq_id, bool logits)
            {
                const int32_t index = batch.n_tokens++;
                batch.token[index] = id;
                batch.pos[index] = pos;
                batch.n_seq_id[index] = 1;
                batch.seq_id[index][0] = seq_id;
                batch.logits[index] = logits;
            };
            bool decode_failed = false;

            while (n_remain != 0)
            {
                if (is_stop_requested())
                {
                    should_stop = true;
                    My_Log{My_Log::Level::kWarning}
                        << "[LLAMACpp] " << query_type << " Speculative query interrupted by Stop() after "
                        << n_generated << " tokens\n";
                    break;
                }
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

                    batch_tgt.n_tokens = 0;
                    spec_batch_tgt.clear();
                    add_batch_token(batch_tgt, id_last, n_past++, kSpecSeqId, true);
                    spec_batch_tgt.add(id_last, n_past - 1, kSpecSeqId, true);
                    if (llama_decode(ctx, batch_tgt))
                    {
                        My_Log{My_Log::Level::kError}
                            << "[LLAMACpp] " << query_type << " forced-close llama_decode FAILED\n";
                        decode_failed = true;
                        break;
                    }
                    common_speculative_process(spec_.get(), spec_batch_tgt);
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
                                            true,
                                            n_draft_max,
                                            n_past,
                                            id_last,
                                            &spec_prompt,
                                            &draft,
                    };
                    const int64_t t_draft0 = now_ms();
                    common_speculative_draft(spec_.get());
                    spec_draft_ms += now_ms() - t_draft0;
                    n_draft_proposed_total += (int64_t) draft.size();

                    if (params_.speculative.draft.ctx_dft)
                    {
                        llama_memory_seq_rm(llama_get_memory(params_.speculative.draft.ctx_dft), kSpecSeqId, n_past, -1);
                    }
                }

                batch_tgt.n_tokens = 0;
                spec_batch_tgt.clear();
                add_batch_token(batch_tgt, id_last, n_past++, kSpecSeqId, true);
                spec_batch_tgt.add(id_last, n_past - 1, kSpecSeqId, true);
                for (size_t i = 0; i < draft.size(); ++i)
                {
                    add_batch_token(batch_tgt, draft[i], n_past + (int) i, kSpecSeqId, true);
                    spec_batch_tgt.add(draft[i], n_past + (int) i, kSpecSeqId, true);
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

                common_speculative_process(spec_.get(), spec_batch_tgt);
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
                const int round_base = n_past;
                n_past += (int) ids.size() - 1;

                bool stop_now = false;
                for (size_t j = 0; j < ids.size(); ++j)
                {
                    const llama_token next_id = ids[j];
                    spec_prompt.push_back(id_last);
                    id_last = next_id;
                    --n_remain;
                    if (!guarded_emit(id_last, round_base + static_cast<int>(j)))
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

        for (const auto &pending : guard_pending)
        {
            emit_generated_token(pending.first);
        }
        guard_pending.clear();

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
                 << (degenerate_loop_hit ? " | degenerate_repeat_guard=1" : "")
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
            {
                std::lock_guard<std::mutex> metrics_lock(trace_metrics_mutex_);
                last_trace_metrics_ = metrics;
            }
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

        spec_.reset();
        draft_init_.reset();

        if (llama_init)
        {
            llama_init.reset();
            smpl = nullptr;
        }
    }

    LlamaBackendRef backend_ref_;
    common_params params_;
    llama_flash_attn_type draft_flash_attn_ = LLAMA_FLASH_ATTN_TYPE_AUTO;
    common_init_result_ptr llama_init;

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
    bool done{true};

    int max_length_ = 0;
    bool stopped_by_output_limit_ = false;

    mutable std::mutex trace_metrics_mutex_;
    json last_trace_metrics_ = nullptr;
    double last_decode_tok_s_ = 0.0;
    int think_time_budget_s_ = 0;
    int think_min_tokens_ = 0;
    int think_max_tokens_ = 0;
    int think_ctx_reserve_ = 0;

    bool think_force_min_floor_ = false;
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
        std::lock_guard<std::mutex> metrics_lock(trace_metrics_mutex_);
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
            const int n_cmp = std::min(n_cached, limit);
            int common = 0;
            while (common < n_cmp && prefix_cache_tokens_[common] == embd_inp[common])
            {
                ++common;
            }
            std::string detail;
            for (const auto &ck : prefix_ckpts_)
            {
                const char *state = ck.pos > limit      ? "over_limit"
                                    : ck.pos > n_cached ? "over_cached"
                                    : std::equal(prefix_cache_tokens_.begin(), prefix_cache_tokens_.begin() + ck.pos,
                                                 embd_inp.begin())
                                        ? "match"
                                        : "mismatch";
                detail += std::to_string(ck.pos) + ":" + state + " ";
            }
            My_Log{} << "[LLAMACpp] Prefix cache fallback detail: limit=" << limit << ", common_prefix=" << common
                     << ", checkpoints=" << detail << "\n";
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

        n_ctx = static_cast<int>(llama_speculative::ResolveContextSizeByPhysicalMemory());
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

        arg_strings.insert(arg_strings.end(), {"-ngl", "0"});
    }
    else if (!speculative_requested)
    {

        arg_strings.insert(arg_strings.end(), {
            "--device", "GPUOpenCL",
            "-ngl", "99",
            "--load-mode", "none",
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
    }
    arg_strings.insert(arg_strings.end(), configured_extra_args.begin(), configured_extra_args.end());

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

    params.verbosity = 0;

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

    params.n_predict = -1;

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

    auto t_lock_wait_start = std::chrono::steady_clock::now();
    QueryScope query_scope{*this};
    if (!query_scope.admitted())
    {
        return false;
    }
    std::lock_guard<std::mutex> lock(query_mutex_);
    int64_t lock_wait_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t_lock_wait_start).count();
    if (lock_wait_ms > 10)
    {
        My_Log{My_Log::Level::kWarning}
            << "[LLAMACpp] query_mutex_ wait time: " << lock_wait_ms << "ms (lock contention detected)\n";
    }
    if (query_scope.stopped())
    {
        My_Log{My_Log::Level::kWarning}
            << "[LLAMACpp] Query cancelled: Stop() was issued while it was waiting for query_mutex_\n";
        return false;
    }
    if (prefill_heartbeat && !prefill_heartbeat())
        return false;

    struct DoneGuard
    {
        Impl *impl;

        ~DoneGuard()
        {
            {
                std::lock_guard<std::mutex> lk(impl->m);
                impl->done = true;
            }
            impl->cv.notify_all();
        }
    } done_guard{impl_};

    auto &prompt = model_input.text_;

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
    impl_->think_force_min_floor_ = model_input.think_force_min_floor_;
    bool result = impl_->Query(prompt, callback, prefill_heartbeat,
                                query_type_bracketed.c_str(), is_aux_inference,
                                stop_epoch_, query_scope.epoch());
    stopped_by_output_limit_ = impl_->stopped_by_output_limit_;
    return result;
}

LLAMACppBuilder::~LLAMACppBuilder()
{
    MarkDestroying();
    if (impl_)
    {
        std::unique_lock<std::mutex> lk(impl_->m);
        if (!impl_->cv.wait_for(lk, std::chrono::seconds(6), [this]
        { return impl_->done; }))
        {
            My_Log{My_Log::Level::kWarning}
                << "[LLAMACpp] ~LLAMACppBuilder(): query did not finish within 6s after stop request" << std::endl;
        }
    }
    WaitForIdle(8000);
    delete impl_;
    impl_ = nullptr;
}

void LLAMACppBuilder::SetPrefixCacheEnabled(bool enabled)
{
    g_llama_prefix_cache_enabled.store(enabled);
}

void LLAMACppBuilder::SetDegenerateRepeatGuardEnabled(bool enabled)
{
    g_degenerate_repeat_guard_enabled.store(enabled);
}

void LLAMACppBuilder::SetDegenerateRepeatGuardMaxPeriod(int max_period)
{
    g_degenerate_repeat_guard_max_period.store(max_period);
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
    StopScope stop_scope{*this};
    if (!stop_scope.admitted() || !impl_)
    {
        return true;
    }
    stop_epoch_.fetch_add(1);
    std::unique_lock<std::mutex> lk(impl_->m);
    if (!impl_->cv.wait_for(lk, std::chrono::seconds(6), [this]
    { return impl_->done; }))
    {
        My_Log{My_Log::Level::kWarning}
            << "[LLAMACpp] Stop(): query did not finish within 6s after stop request" << std::endl;
    }
    return true;
}

json LLAMACppBuilder::HandleProfile()
{
    json result;

    const json m = impl_ ? impl_->GetLastQueryTraceMetrics() : json(nullptr);
    if (m.is_object())
    {
        int64_t ttft_ms = m.value("ttft_ms", (int64_t) 0);
        result["time_to_first_token"] = FormatFixed2(ttft_ms > 0 ? ttft_ms / 1000.0 : 0.0);
        result["token_generation_time"] = FormatFixed2(m.value("decode_ms", (int64_t) 0) / 1000.0);
        result["prompt_processing_rate"] = FormatFixed2(m.value("prefill_tok_s", 0.0));
        result["token_generation_rate"] = FormatFixed2(m.value("decode_tok_s", 0.0));
        result["num_prompt_tokens"] = m.value("prompt_tokens", 0);
        result["num_generated_tokens"] = m.value("gen_tokens", 0);
        return result;
    }

    if (impl_)
    {
        std::unique_lock<std::mutex> query_lock(query_mutex_, std::try_to_lock);
        if (!query_lock.owns_lock())
        {
            return result;
        }
        llama_context *ctx = impl_->llama_init->context();
        if (!ctx)
        {
            return result;
        }

        llama_perf_context_data perf_ctx = llama_perf_context(ctx);

        result["time_to_first_token"] = FormatFixed2(perf_ctx.t_p_eval_ms / 1000.0);

        result["token_generation_time"] = FormatFixed2(perf_ctx.t_eval_ms / 1000.0);

        result["prompt_processing_rate"] = FormatFixed2(
                perf_ctx.t_p_eval_ms > 0 ? (perf_ctx.n_p_eval * 1000.0) / perf_ctx.t_p_eval_ms : 0.0);

        result["token_generation_rate"] = FormatFixed2(
                perf_ctx.t_eval_ms > 0 ? (perf_ctx.n_eval * 1000.0) / perf_ctx.t_eval_ms : 0.0);

        result["num_prompt_tokens"] = perf_ctx.n_p_eval;
        result["num_generated_tokens"] = perf_ctx.n_eval;
    }

    return result;
}

size_t LLAMACppBuilder::TokenLength(const std::string &text)
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    llama_context *ctx = impl_->llama_init->context();
    if (ctx == nullptr)
    {
        return 0;
    }

    const auto tokens = common_tokenize(ctx, text, /* add_special = */ false, /* parse_special = */ true);
    return tokens.size();
}
