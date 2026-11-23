//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "genie.h"
#include "genie_interface.h"
#include "utils.h"
#include "log.h"
#include "config_fixer.h"
#include "watermark_provider_host.h"
#include <algorithm>
#include <atomic>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <shared_mutex>
#include <system_error>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

static Genie_Status_t GenieDialogCreateGuarded(const GenieDialogConfig_Handle_t config,
                                               GenieDialog_Handle_t *dialog, bool *faulted)
{
    *faulted = false;
    __try
    {
        return GenieDialog_create(config, dialog);
    }
    __except (GetExceptionCode() == EXCEPTION_ACCESS_VIOLATION ? EXCEPTION_EXECUTE_HANDLER
                                                                : EXCEPTION_CONTINUE_SEARCH)
    {
        *faulted = true;
        return GENIE_STATUS_ERROR_GENERAL;
    }
}
#endif

struct QnnWatermarkState
{
    const GenieWatermarkProviderVTable *vtable{nullptr};
    GenieWatermarkTokenHook *hook{nullptr};
    uint32_t n_vocab{0};
    uint64_t seed{0};
    bool hook_created{false};
    std::vector<int32_t> ids_cache;

    std::vector<int32_t> pending_prompt_ids;
    bool prompt_pending{false};
};

namespace
{

std::shared_mutex &QnnWatermarkRegistryMutex()
{
    static std::shared_mutex m;
    return m;
}

std::string GenerateQnnWatermarkCallbackName()
{
    static std::atomic<uint64_t> s_counter{0};
    return "genie_wm_" + std::to_string(s_counter.fetch_add(1, std::memory_order_relaxed));
}

void QnnWatermarkSamplerCallback(const uint32_t logitsSize, const void *logits,
                                 const uint32_t numTokens, int32_t *tokens,
                                 const void *userData)
{
    if (numTokens == 0 || tokens == nullptr)
    {
        return;
    }

    auto *state = const_cast<QnnWatermarkState *>(static_cast<const QnnWatermarkState *>(userData));
    if (!state || !state->vtable || !logits || logitsSize == 0)
    {
        tokens[0] = 0;
        return;
    }

    const uint32_t n_vocab = logitsSize / static_cast<uint32_t>(sizeof(float));
    if (n_vocab == 0)
    {
        tokens[0] = 0;
        return;
    }

    if (!state->hook_created)
    {
        state->hook = state->vtable->token_hook_create(n_vocab, state->seed);
        state->n_vocab = n_vocab;
        state->hook_created = true;
    }

    if (state->prompt_pending)
    {
        if (state->hook)
        {
            for (const int32_t prompt_id : state->pending_prompt_ids)
            {
                state->vtable->token_hook_accept(state->hook, prompt_id);
            }
        }
        state->pending_prompt_ids.clear();
        state->prompt_pending = false;
    }

    const auto *src = reinterpret_cast<const float *>(logits);

    if (!state->hook)
    {

        tokens[0] = static_cast<int32_t>(std::distance(src, std::max_element(src, src + n_vocab)));
        return;
    }

    std::vector<float> logits_copy(src, src + n_vocab);
    if (state->ids_cache.size() != n_vocab)
    {
        state->ids_cache.resize(n_vocab);
        for (uint32_t i = 0; i < n_vocab; ++i)
        {
            state->ids_cache[i] = static_cast<int32_t>(i);
        }
    }

    state->vtable->token_hook_apply(state->hook, logits_copy.data(), state->ids_cache.data(),
                                    static_cast<int32_t>(n_vocab));

    int32_t best_id = 0;
    float best_val = logits_copy[0];
    for (uint32_t i = 1; i < n_vocab; ++i)
    {
        if (logits_copy[i] > best_val)
        {
            best_val = logits_copy[i];
            best_id = static_cast<int32_t>(i);
        }
    }
    tokens[0] = best_id;
    state->vtable->token_hook_accept(state->hook, best_id);
}

}

void GenieLog_Callback(const GenieLog_Handle_t  /*handle*/,
                       const char *fmt,
                       GenieLog_Level_t level,
                       uint64_t /*timestamp*/,
                       va_list args)
{
    auto length = std::vsnprintf(nullptr, 0, fmt, args);
    if (length < 0)
    {
        My_Log{My_Log::Level::kWarning} << "bad args and fmt in genie callback" << std::endl;
        return;
    }

    std::vector<char> buf(length);
    My_Log::Level my_level;
    switch (level)
    {
        case GENIE_LOG_LEVEL_ERROR:
            my_level = My_Log::Level::kError;
            break;
        case GENIE_LOG_LEVEL_WARN:
            my_level = My_Log::Level::kWarning;
            break;
        case GENIE_LOG_LEVEL_INFO:
            my_level = My_Log::Level::kInfo;
            break;
        case GENIE_LOG_LEVEL_VERBOSE:
            my_level = My_Log::Level::kVerbose;
            break;
    }

    std::vsnprintf(buf.data(), length, fmt, args);
    while (buf[length - 2] == '\n')
    {
        buf[length - 2] = '\0';
        length--;
    }
}

#if GENIE_API_VERSION_MAJOR > 1 || GENIE_API_VERSION_MINOR >= 15
#define QNN_CKPT_OCCUPANCY_API 1
#else
#define QNN_CKPT_OCCUPANCY_API 0
#endif

namespace
{

#if QNN_CKPT_OCCUPANCY_API
thread_local void *g_value_alloc = nullptr;
#endif

void NoopDialogCallback(const char *, GenieDialog_SentenceCode_t, const void *)
{
}

double ElapsedMs(std::chrono::steady_clock::time_point since)
{
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - since).count();
}

}

void GenieContext::inference_thread()
{
    while (true)
    {
        bool exit_requested = false;
        {
            std::unique_lock<std::mutex> lock(m_request_lock);
            m_request_cond.wait_for(lock, std::chrono::milliseconds(500),
                                    [this] { return m_request_ready.load() || m_thread_exit.load(); });
            if (m_thread_exit)
            {
                exit_requested = true;
            }
            else if (!m_request_ready)
            {
                continue;
            }
            else
            {
                m_request_ready = false;
            }
        }

        if (exit_requested)
        {
            {
                std::lock_guard<std::mutex> stream_guard(m_stream_lock);
                m_inference_busy = false;
            }
            m_stream_cond.notify_all();
            return;
        }

        try
        {
            std::shared_lock<std::shared_mutex> wm_read_lock;
            if (watermark_active_)
            {
                wm_read_lock = std::shared_lock<std::shared_mutex>(QnnWatermarkRegistryMutex());
            }
            auto status = inf_impl_->inf_->GenieDialogQuery();
            if (GENIE_STATUS_SUCCESS != status && GENIE_STATUS_WARNING_ABORTED != status
                && GENIE_STATUS_WARNING_CONTEXT_EXCEEDED != status)
            {
                inference_succeed_ = false;
                My_Log{My_Log::Level::kError} << "get response from GenieDialog failed: " << status
                                              << " current token size: " << inf_impl_->inf_->cur_length_ << "\n";
            }
            else
            {
                inference_succeed_ = true;
                if (GENIE_STATUS_WARNING_CONTEXT_EXCEEDED == status)
                {
                    stopped_by_output_limit_ = true;
                    My_Log{My_Log::Level::kError} << "[MODEL_DEFECT] reason=sdk_context_exceeded model="
                                                  << model_config_.get_model_name()
                                                  << " token_size=" << inf_impl_->inf_->cur_length_ << "\n";
                }
            }
        }
        catch (const std::exception &e)
        {
            inference_succeed_ = false;
            My_Log{My_Log::Level::kError} << "GenieDialogQuery threw exception: " << e.what() << std::endl;
        }
        catch (...)
        {
            inference_succeed_ = false;
            My_Log{My_Log::Level::kError} << "GenieDialogQuery threw unknown exception" << std::endl;
        }

        {
            std::lock_guard<std::mutex> stream_guard(m_stream_lock);
            m_inference_busy = false;
        }
        m_stream_cond.notify_all();
    }
}

bool GenieContext::Query(const ModelInput &model_input, const Callback &callback,
                         PrefillHeartbeatCallback prefill_heartbeat)
{

    auto t_lock_start = std::chrono::steady_clock::now();
    QueryScope query_scope{*this};
    if (!query_scope.admitted())
    {
        return false;
    }
    std::lock_guard<std::mutex> lock(query_mutex_);
    auto t_lock_acquired = std::chrono::steady_clock::now();
    int64_t lock_wait_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            t_lock_acquired - t_lock_start).count();
    if (lock_wait_ms > 10)
    {
        My_Log{My_Log::Level::kWarning}
                << "[QNN] query_mutex_ wait time: " << lock_wait_ms << "ms"
                << " (lock contention detected!)\n";
    }
    else
    {
        My_Log{} << "[QNN] query_mutex_ acquired immediately (wait=" << lock_wait_ms << "ms)\n";
    }
    {
        std::lock_guard<std::mutex> metrics_lock(trace_metrics_mutex_);
        last_trace_metrics_ = json::object();
    }
    ++query_seq_;
    last_query_prompt_tokens_ = 0;
    last_query_gen_tokens_ = 0;
    if (query_scope.stopped())
    {
        My_Log{My_Log::Level::kWarning}
                << "[QNN] Query cancelled: Stop() was issued while it was waiting for query_mutex_\n";
        return false;
    }

    if (busy_after_timeout_.load() || m_inference_busy.load())
    {
        constexpr int kBusyWaitMs = 5000;
        std::unique_lock<std::mutex> busy_lock(m_stream_lock);
        const bool idle = m_stream_cond.wait_for(busy_lock, std::chrono::milliseconds(kBusyWaitMs),
                                                 [this] { return !m_inference_busy.load(); });
        if (!idle)
        {
            My_Log{My_Log::Level::kError}
                    << "[QNN] previous GenieDialogQuery is still running after " << kBusyWaitMs
                    << "ms (busy_after_timeout); rejecting this Query instead of running concurrently\n";
            return false;
        }
        busy_after_timeout_ = false;
    }

    if (m_DialogHandle == nullptr)
    {
        My_Log{My_Log::Level::kError} << "[QNN] Query rejected: the Genie dialog is unavailable\n";
        return false;
    }

    if (prefill_heartbeat && !prefill_heartbeat())
        return false;

    const char *query_type = get_query_type_label(model_input);
    My_Log{} << "[QNN Restore] Query begin: type=" << query_type
             << ", model_name=" << model_config_.get_model_name()
             << ", model_dir=" << m_model_dir_ << std::endl;

    Genie_Status_t status = 0;

    stopped_by_output_limit_ = false;
    struct LimitCommit
    {
        GenieContext *self;

        ~LimitCommit() { self->last_stopped_by_output_limit_.store(self->stopped_by_output_limit_.load()); }
    } limit_commit{this};
    const size_t prompt_tokens = TokenLengthUnlocked(model_input.text_);
    inf_impl_->inf_->cur_length_ = prompt_tokens;
    if (!inf_impl_->inf_->set_content(const_cast<ModelInput &>(model_input)))
    {
        return false;
    }

    CheckpointRun ckpt_run;
    bool ckpt_retried = false;
prefill_start:
    if (!PrepareCheckpointedPrefill(model_input, query_scope, prefill_heartbeat, query_type, ckpt_retried, ckpt_run))
    {
        return false;
    }
    profile_gen_base_ = ckpt_run.suffix_mode ? ckpt_run.gen_base : 0;
    if (ckpt_run.suffix_mode)
    {
        inf_impl_->inf_->OverrideInput(ckpt_suffix_, GENIE_DIALOG_SENTENCE_END);
    }
    else
    {
        inf_impl_->inf_->OverrideInput(model_input.text_, GENIE_DIALOG_SENTENCE_COMPLETE);
        if (!kv_path_.empty() &&
            GENIE_STATUS_SUCCESS != (status = GenieDialog_restore(m_DialogHandle, kv_path_.c_str())))
        {
            throw std::runtime_error("restore kv failed: " + std::to_string(status));
        }
    }
    if (CheckpointEligible(query_type))
    {
        My_Log{} << "[QNN] " << query_type << " Prefix cache: enabled=1"
                 << " | reused_tokens=" << ckpt_run.reused_tokens
                 << " | checkpoint_at=" << ckpt_run.checkpoint_at
                 << " | prompt_tokens=" << prompt_tokens
                 << " | hit=" << ckpt_run.hit
                 << " | n_past=" << ckpt_run.reused_n_past
                 << " | created=" << ckpt_run.created
                 << " | restore_ms=" << static_cast<int64_t>(ckpt_run.restore_ms)
                 << " | save_ms=" << static_cast<int64_t>(ckpt_run.save_ms)
                 << " | prefill_ms=" << static_cast<int64_t>(ckpt_run.prefill_ms)
                 << " | suffix=" << ckpt_run.suffix_mode
                 << " | fallback=" << (ckpt_run.fallback.empty() ? "none" : ckpt_run.fallback) << "\n";
    }

    if (watermark_active_ && wm_state_)
    {
        const int32_t *prompt_buf = nullptr;
        uint32_t prompt_len = 0;
        if (GenerateTextToken(model_input.text_, prompt_buf, prompt_len) && prompt_buf != nullptr && prompt_len > 0)
        {
            wm_state_->pending_prompt_ids.assign(prompt_buf, prompt_buf + prompt_len);
            free((void *) prompt_buf);
        }
        else
        {
            wm_state_->pending_prompt_ids.clear();
        }
        wm_state_->prompt_pending = !wm_state_->pending_prompt_ids.empty();
    }

    if (query_scope.stopped())
    {
        My_Log{My_Log::Level::kWarning}
                << "[QNN] Query cancelled: Stop() was issued before inference started\n";
        return false;
    }

    {
        std::lock_guard<std::mutex> stream_guard(m_stream_lock);
        m_stream_answer.clear();
        m_inference_busy = true;
    }
    {
        std::lock_guard<std::mutex> request_guard(m_request_lock);
        m_request_ready = true;
    }
    m_request_cond.notify_one();

    std::string response;
    bool callback_rejected = false;
    bool stop_forwarded = false;
    bool output_emitted = false;
    int64_t ttft_ms = -1;
    auto last_output_time = std::chrono::steady_clock::now();

    while (m_inference_busy)
    {
        if (!stop_forwarded && query_scope.stopped())
        {
            stop_forwarded = true;
            AbortInference();
        }
        {
            std::unique_lock<std::mutex> lock(m_stream_lock);

            m_stream_cond.wait_for(lock, std::chrono::milliseconds(HEARTBEAT_INTERVAL_MS),
                                   [this] { return !m_stream_answer.empty() || !m_inference_busy; });

            if (!m_stream_answer.empty())
            {
                response = std::move(m_stream_answer);

                last_output_time = std::chrono::steady_clock::now();
            }
        }

        if (response.empty())
        {

            auto now = std::chrono::steady_clock::now();
            auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                    now - last_output_time).count();
            if (elapsed_ms >= HEARTBEAT_INTERVAL_MS)
            {
                std::string heartbeat = "";
                if (!callback(heartbeat))
                {
                    My_Log{My_Log::Level::kWarning}
                            << "[Query] Heartbeat callback rejected, stopping generation." << std::endl;
                    callback_rejected = true;
                    AbortInference();
                    break;
                }
                last_output_time = now;
            }
            continue;
        }

        if (!output_emitted)
        {
            output_emitted = true;
            ttft_ms = static_cast<int64_t>(ElapsedMs(t_lock_acquired));
        }
        if (!callback(response))
        {
            callback_rejected = true;

            AbortInference();
            break;
        }

        response.clear();
    }

    if (callback_rejected && m_inference_busy)
    {
        My_Log{My_Log::Level::kInfo}
                << "[Query] Waiting for inference_thread_ to finish after Stop()..." << std::endl;
        const int kMaxWaitMs = 5000;
        std::unique_lock<std::mutex> wait_lock(m_stream_lock);
        bool finished = m_stream_cond.wait_for(wait_lock,
                                               std::chrono::milliseconds(kMaxWaitMs),
                                               [this] { return !m_inference_busy.load(); });
        if (!finished)
        {
            busy_after_timeout_ = true;
            m_stream_answer.clear();
            My_Log{My_Log::Level::kWarning}
                    << "[Query] inference_thread_ did not finish within " << kMaxWaitMs
                    << "ms after Stop(). Marking context busy_after_timeout and failing this Query." << std::endl;
            return false;
        }
        else
        {
            My_Log{My_Log::Level::kInfo}
                    << "[Query] inference_thread_ finished (wait_for returned)." << std::endl;
        }
    }

    if (!inference_succeed_)
    {
        if (ckpt_run.suffix_mode && !output_emitted && !ckpt_retried && !callback_rejected && !query_scope.stopped())
        {
            ckpt_retried = true;
            if (ckpt_run.hit)
            {
                InvalidateCheckpoint(ckpt_run.hit_name, "query_failed");
            }
            if (RebuildDialog())
            {
                goto prefill_start;
            }
        }
        return false;
    }

    std::string tail_answer;
    {
        std::lock_guard<std::mutex> stream_guard(m_stream_lock);
        tail_answer = std::move(m_stream_answer);
        m_stream_answer.clear();
    }
    if (!callback_rejected && !tail_answer.empty())
    {
        callback(tail_answer);
    }

    last_query_prompt_tokens_ = prompt_tokens;
    last_query_gen_tokens_ = inf_impl_->inf_->cur_length_ > prompt_tokens ? inf_impl_->inf_->cur_length_ - prompt_tokens : 0;

    if (ckpt_capable_.load())
    {
        PublishTraceMetrics(ckpt_run, prompt_tokens, inf_impl_->inf_->cur_length_, ttft_ms);
    }

    if (std::string(query_type) != "MAINAGENT_INFERENCE" &&
        std::string(query_type) != "SUBAGENT_INFERENCE")
    {
        My_Log{My_Log::Level::kInfo}
                << "[QNN] Post-query reset for non-main-inference type=" << query_type
                << " (clearing SSD forecast state)" << std::endl;
        if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle))
        {
            My_Log{My_Log::Level::kWarning}
                    << "[QNN] Post-query GenieDialog_reset failed for type=" << query_type << std::endl;

        }
    }

    return true;
}

GenieContext::GenieContext(const ModelInstanceConfig &model_config) :
        ContextBase(model_config)
{
    auto fixer = ConfigFixer{model_config};

    std::string wm_callback_name;
    watermark_active_ = WatermarkProviderHost::Instance().HasTokenHook();
    if (watermark_active_)
    {
        wm_callback_name = GenerateQnnWatermarkCallbackName();
        wm_state_ = new QnnWatermarkState{};
        wm_state_->vtable = WatermarkProviderHost::Instance().GetVTable();
        wm_state_->seed = 0;

        Genie_Status_t reg_status;
        {

            std::unique_lock<std::shared_mutex> lock(QnnWatermarkRegistryMutex());
            reg_status = GenieSampler_registerUserDataCallback(
                    wm_callback_name.c_str(), QnnWatermarkSamplerCallback,
                    static_cast<const void *>(wm_state_));
        }
        if (GENIE_STATUS_SUCCESS != reg_status)
        {
            My_Log{My_Log::Level::kWarning}
                    << "[Watermark] GenieSampler_registerUserDataCallback failed (status=" << reg_status
                    << "); falling back to no watermark for this QNN model instance.\n";
            delete wm_state_;
            wm_state_ = nullptr;
            watermark_active_ = false;
            wm_callback_name.clear();
        }
    }

    try
    {
        Genie_Status_t status = 0;

        if (GENIE_STATUS_SUCCESS != GenieDialogConfig_createFromJson(
                fixer.FixConfig(wm_callback_name).dump().c_str(), &m_ConfigHandle))
        {
            throw std::runtime_error("Failed to create the Genie Dialog config.");
        }

        status = GenieLog_create(nullptr, GenieLog_Callback, get_genie_log_level(), &m_LogHandle);
        if ((GENIE_STATUS_SUCCESS != status) || (!m_LogHandle))
        {
            throw std::runtime_error("Failed to create the Log handle.");
        }

        status = GenieDialogConfig_bindLogger(m_ConfigHandle, m_LogHandle);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to bind the log handle with the dialog config");
        }

        status = GenieProfile_create(nullptr, &m_ProfileHandle);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to create the profile handle");
        }

        status = GenieDialogConfig_bindProfiler(m_ConfigHandle, m_ProfileHandle);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to bind the profile handle with the dialog config");
        }

        bool dialog_faulted = false;
#ifdef _WIN32
        const Genie_Status_t dialog_status = GenieDialogCreateGuarded(m_ConfigHandle, &m_DialogHandle, &dialog_faulted);
#else
        const Genie_Status_t dialog_status = GenieDialog_create(m_ConfigHandle, &m_DialogHandle);
#endif
        if (dialog_faulted)
        {
            m_DialogHandle = nullptr;
            My_Log{My_Log::Level::kError}
                    << "[QNN] GenieDialog_create raised an access violation inside the SDK; "
                       "treating as load failure and releasing handles.\n";
            throw std::runtime_error("create the genie dialog failed (SDK access violation)");
        }
        if (GENIE_STATUS_SUCCESS != dialog_status)
        {
            throw std::runtime_error("create the genie dialog failed");
        }

        status = GenieSamplerConfig_createFromJson(fixer.FixSampler().dump().c_str(), &m_SamplerConfigHandle);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to create sampler config");
        }

        status = GenieDialog_getSampler(m_DialogHandle, &m_SamplerHandle);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to get sampler");
        }
    }
    catch (...)
    {
        ReleaseHandles();
        ReleaseWatermarkState();
        throw;
    }

    std::vector<std::string> store_paths;
    if (fixer.has_ssd_prefix_)
        goto ahead;

    store_paths = {
            model_config.get_model_path() + "/",
            model_config.get_model_path() + "/Prefix/"
    };

    for (auto &path: store_paths)
    {
        if (File::IsFileExist(path + "kv-cache.primary.qnn-htp"))
        {
            if (fixer.kv_restore_eligible_)
            {
                kv_path_ = path;
                My_Log{} << "kv_path: " << kv_path_ << std::endl;
            }
            else
            {
                My_Log{My_Log::Level::kWarning}
                        << "kv-cache.primary.qnn-htp found at " << path
                        << " but dialog.context.bos-token is not -1 (Prefix Quant requires it); "
                           "skipping kv-cache restore\n";
            }
            break;
        }
    }

    ahead:
    if (!inf_impl_)
    {
        inf_impl_ = new QInterfaceImpl{this};
    }

    if (!m_stream_thread)
    {
        m_stream_thread = std::make_unique<std::thread>(&GenieContext::inference_thread, this);
    }

    const auto &prompt_template = model_config.get_prompt_template();
    const bool chatml_template = prompt_template.is_object() && prompt_template.contains("start") &&
                                 prompt_template["start"].is_string() &&
                                 prompt_template["start"].get<std::string>().find(qnn_ckpt::kMarker) !=
                                 std::string::npos;
    ckpt_capable_ = QNN_CKPT_OCCUPANCY_API && qnn_ckpt::GlobalConfig().enabled.load() && !fixer.has_ssd_prefix_ &&
                    fixer.kv_restore_eligible_ && !watermark_active_ && chatml_template &&
                    model_config.i_model_config_.get_qnn_embedding().embedding_type_ == QNNEmbeddingType::None;
#if !QNN_CKPT_OCCUPANCY_API
    if (qnn_ckpt::GlobalConfig().enabled.load())
    {
        My_Log{My_Log::Level::kWarning}
                << "[QNN] prefix checkpoints disabled: Genie API " << GENIE_API_VERSION_MAJOR << '.'
                << GENIE_API_VERSION_MINOR << " has no GenieDialog_getValue (needs 1.15)\n";
    }
#endif
    if (ckpt_capable_)
    {
        bool encoded = true;
        const std::vector<int32_t> marker = EncodeTokens(qnn_ckpt::kMarker, encoded);
        if (encoded && marker.size() == 1)
        {
            ckpt_marker_id_ = marker[0];
            RefreshCheckpointStore();
        }
        else
        {
            ckpt_capable_ = false;
            My_Log{My_Log::Level::kWarning}
                    << "[QNN] prefix checkpoints disabled: the template marker is not a single token\n";
        }
    }

    if (watermark_active_)
    {
        My_Log{My_Log::Level::kInfo}
                << "[Watermark] QNN backend token hook registered via custom sampler callback"
                << " (callback-name=" << wm_callback_name << ").\n";
    }
    else if (!WatermarkProviderHost::Instance().HasTokenHook())
    {
        static std::once_flag s_qnn_wm_log_once;
        std::call_once(s_qnn_wm_log_once, []()
        {
            My_Log{My_Log::Level::kInfo}
                    << "[Watermark] No watermark plugin declares GENIE_WM_CAP_TOKEN_HOOK; "
                    << "QNN backend running without watermark.\n";
        });
    }

}

GenieContext::~GenieContext()
{
    My_Log{} << "GenieContext::~GenieContext():\n";

    MarkDestroying();
    if (m_DialogHandle != nullptr && (m_inference_busy.load() || pending_queries_.load() > 0))
    {
        AbortInference();
    }
    WaitForIdle(8000);

    if (m_stream_thread)
    {
        {
            std::lock_guard<std::mutex> request_guard(m_request_lock);
            m_thread_exit = true;
        }
        m_request_cond.notify_all();

        m_stream_thread->join();
        m_stream_thread = nullptr;
        {
            std::lock_guard<std::mutex> request_guard(m_request_lock);
            m_request_ready = false;
            m_thread_exit = false;
        }
    }

    ReleaseHandles();
    ReleaseWatermarkState();

    delete inf_impl_;
    inf_impl_ = nullptr;
    My_Log{} << "GenieContext::~GenieContext() Done:\n";
}

void GenieContext::ReleaseHandles()
{

    if (m_ConfigHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialogConfig_free(m_ConfigHandle))
        {
            My_Log{} << "Failed to free the Genie Dialog config.\n";
        }
        m_ConfigHandle = nullptr;
    }

    if (m_DialogHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialog_free(m_DialogHandle))
        {
            My_Log{} << "Failed to free the Genie Dialog.\n";
        }
        m_DialogHandle = nullptr;
    }

    if (m_SamplerConfigHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieSamplerConfig_free(m_SamplerConfigHandle))
        {
            My_Log{} << "Failed to free the sampler config." << std::endl;
        }
        m_SamplerConfigHandle = nullptr;
    }

    if (m_LogHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieLog_free(m_LogHandle))
        {
            My_Log{} << "Failed to free the Log handle." << std::endl;
        }
        m_LogHandle = nullptr;
    }

    if (m_ProfileHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieProfile_free(m_ProfileHandle))
        {
            My_Log{} << "Failed to free the profile handle." << std::endl;
        }
        m_ProfileHandle = nullptr;
    }
}

void GenieContext::ReleaseWatermarkState()
{
    if (wm_state_ != nullptr)
    {
        if (wm_state_->vtable && wm_state_->hook)
        {
            wm_state_->vtable->token_hook_free(wm_state_->hook);
        }
        delete wm_state_;
        wm_state_ = nullptr;
    }
    watermark_active_ = false;
}

bool GenieContext::Stop()
{
    StopScope stop_scope{*this};
    if (!stop_scope.admitted())
    {
        return true;
    }
    stop_epoch_.fetch_add(1);
    return AbortInference();
}

bool GenieContext::AbortInference()
{
    std::lock_guard<std::mutex> handle_lock(dialog_handle_mutex_);
    if (m_DialogHandle == nullptr)
    {
        return false;
    }
    if (GENIE_STATUS_SUCCESS != GenieDialog_signal(m_DialogHandle, GENIE_DIALOG_ACTION_ABORT))
    {
        My_Log{} << "Failed to stop generation.\n";
        return false;
    }

    return true;
}

bool GenieContext::SetStopSequence(const std::string &stop_sequences)
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    if (GENIE_STATUS_SUCCESS != GenieDialog_setStopSequence(m_DialogHandle, stop_sequences.c_str()))
    {
        My_Log{} << "Failed to set stop sequence.\n";
        return false;
    }
    stop_sequences_ = stop_sequences;

    return true;
}

bool GenieContext::GenerateTextToken(const std::string &text, const int32_t *&buf, uint32_t &len)
{

    if (strlen(text.c_str()) == 0)
    {
        buf = nullptr;
        len = 0;
        return true;
    }

    std::string safe_text = text;
    bool has_invalid = sanitize_utf8_inplace(safe_text);
    if (has_invalid)
    {
        My_Log{My_Log::Level::kWarning}
                << "[GenerateTextToken] Detected and sanitized invalid UTF-8 bytes to prevent tokenizer crash."
                << " original_size=" << text.size() << std::endl;
    }
    const std::string &encode_text = has_invalid ? safe_text : text;

    GenieTokenizer_Handle_t tokenizerHandle = nullptr;
    Genie_Status_t status = GenieDialog_getTokenizer(m_DialogHandle, &tokenizerHandle);
    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "get tokenizer failed: " << status << std::endl;
        return false;
    }

    status = GenieTokenizer_encode(tokenizerHandle, encode_text.c_str(),
                                   [](const size_t size, const char **allocatedData)
                                   {
                                       *allocatedData = reinterpret_cast<const char *>(malloc(size));
                                   },
                                   &buf,
                                   &len);

    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "encode failed: " << status << ", "
                                      << "the string length is: " << text.size() << std::endl;
        return false;
    }
    return true;
}

size_t GenieContext::TokenLength(const std::string &text)
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    return TokenLengthUnlocked(text);
}

size_t GenieContext::TokenLengthUnlocked(const std::string &text)
{
    const int32_t *buf;
    uint32_t len;
    if (!GenerateTextToken(text, buf, len))
    {
        return text.size();
    }

    free((void *) buf);
    return len;
}

void GenieContext::applyLora(const std::string &engineRole, const std::string &loraAdapterName)
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    int32_t status = GenieDialog_applyLora(m_DialogHandle, engineRole.c_str(), loraAdapterName.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to apply the LoRA adapter.");
    }
    lora_applied_[engineRole] = loraAdapterName;
    if (ckpt_capable_.load())
    {
        RefreshCheckpointStore();
    }
}

void GenieContext::setLoraStrength(const std::string &engineRole,
                                   const std::unordered_map<std::string, float> &alphaValue)
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    for (auto it = alphaValue.begin(); it != alphaValue.end(); it++)
    {
        int32_t status = GenieDialog_setLoraStrength(m_DialogHandle, engineRole.c_str(), it->first.c_str(), it->second);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to set the LoRA alpha strength.");
        }
        lora_strengths_[{engineRole, it->first}] = it->second;
    }
    if (ckpt_capable_.load())
    {
        RefreshCheckpointStore();
    }
}

bool GenieContext::SupportsMultimodalInput() const
{
    const int model_types = static_cast<int>(model_config_.i_model_config_.get_qnn_embedding().model_types_);
    return (model_types & ModelType::Vision) || (model_types & ModelType::Audio);
}

json GenieContext::HandleProfile()
{
    std::unique_lock<std::mutex> query_lock(query_mutex_, std::try_to_lock);
    if (!query_lock.owns_lock())
    {
        std::lock_guard<std::mutex> cache_guard(profile_cache_mutex_);
        return last_profile_;
    }

    const Genie_AllocCallback_t callback([](size_t size, const char **data)
                                         {
                                             *data = (char *) calloc(1, size);
                                             if (*data == nullptr)
                                             {
                                                 My_Log{} << "cannot allocate memory for JSON data.\n";
                                             }
                                         });

    const char *jsonData = nullptr;
    const Genie_Status_t status = GenieProfile_getJsonData(m_ProfileHandle, callback, &jsonData);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{My_Log::Level::kError} << "get the profile data failed: " << status << "\n";
        return "";
    }

    std::string jsonStr(jsonData);
    free((char *) jsonData);

    json result;
    try
    {
        json j = json::parse(jsonStr);
        size_t dialog_idx = 0;
        for (size_t i = j["components"].size(); i > 0; --i)
        {
            const auto &component = j["components"][i - 1];
            if (component.value("type", std::string()) == "dialog" && component.contains("events") &&
                !component["events"].empty())
            {
                dialog_idx = i - 1;
                break;
            }
        }
        if (!j["components"].empty() && j["components"][dialog_idx]["type"] == "dialog")
        {
            const auto &events = j["components"][dialog_idx]["events"];
            if (!events.empty())
            {
                const auto &last_event = events.back();
                if (last_event.at("type") == "GenieDialog_query")
                {
                    const std::string event_sig = last_event.dump();
                    bool stale = false;
                    {
                        std::lock_guard<std::mutex> sig_guard(profile_cache_mutex_);
                        const uint64_t seq = query_seq_.load();
                        if (seq != profile_sig_seq_)
                        {
                            if (event_sig == profile_sig_)
                            {
                                stale = true;
                            }
                            else
                            {
                                profile_sig_ = event_sig;
                                profile_sig_seq_ = seq;
                            }
                        }
                    }
                    if (stale)
                    {
                        My_Log{My_Log::Level::kWarning}
                                << "[QNN] Genie profile has no new event for this query (e.g. context full); "
                                   "timing stats suppressed, token counts come from the service counters\n";
                        result["stale_profile"] = true;
                        result["num_prompt_tokens"] = last_query_prompt_tokens_.load();
                        result["num_generated_tokens"] = last_query_gen_tokens_.load();
                    }
                    else
                    {
                        result["time_to_first_token"] =
                                FormatFixed2(last_event.at("time-to-first-token")["value"].get<double>() / 1000000.0);
                        result["token_generation_time"] =
                                FormatFixed2(last_event.at("token-generation-time")["value"].get<double>() / 1000000.0);
                        result["prompt_processing_rate"] =
                                FormatFixed2(last_event.at("prompt-processing-rate")["value"].get<double>());
                        result["token_generation_rate"] =
                                FormatFixed2(last_event.at("token-generation-rate")["value"].get<double>());

                        result["num_prompt_tokens"] = last_event.at("num-prompt-tokens")["value"];
                        const int64_t generated = last_event.at("num-generated-tokens")["value"].get<int64_t>();
                        const int64_t gen_base = profile_gen_base_.load();
                        result["num_generated_tokens"] = generated > gen_base ? generated - gen_base : 0;

                        const json trace = GetLastQueryTraceMetrics();
                        if (trace.is_object() && trace.value("suffix_mode", false))
                        {
                            const double total = trace.value("prompt_tokens", 0.0);
                            const double reused = trace.value("prefix_reused_tokens", 0.0);
                            const double fresh = total > reused ? total - reused : 0.0;
                            const double sdk_ttft_s =
                                    last_event.at("time-to-first-token")["value"].get<double>() / 1000000.0;
                            const double ttft_ms = trace.value("ttft_ms", -1.0);
                            const double ttft_s = ttft_ms > 0.0
                                    ? ttft_ms / 1000.0
                                    : (trace.value("ckpt_restore_ms", 0.0) + trace.value("prefill_ms", 0.0)) / 1000.0
                                      + sdk_ttft_s;
                            const double save_s = trace.value("ckpt_save_ms", 0.0) / 1000.0;
                            const double prefill_s = ttft_s > save_s ? ttft_s - save_s : ttft_s;
                            result["num_prompt_tokens"] = static_cast<int64_t>(total);
                            result["num_prefill_tokens"] = static_cast<int64_t>(fresh);
                            result["time_to_first_token"] = FormatFixed2(ttft_s);
                            result["prefill_time"] = FormatFixed2(prefill_s);
                            result["prompt_processing_rate"] = FormatFixed2(prefill_s > 0.0 ? fresh / prefill_s : 0.0);
                        }
                    }
                }
            }
        }
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "parse profile failed: " << jsonStr << "\n";
        return "";
    }
    {
        std::lock_guard<std::mutex> cache_guard(profile_cache_mutex_);
        last_profile_ = result;
    }
    return result;
}

GenieLog_Level_t GenieContext::get_genie_log_level()
{
    switch (My_Log::Level_)
    {
        case My_Log::Level::kError:
            return GENIE_LOG_LEVEL_ERROR;
        case My_Log::Level::kWarning:
        case My_Log::Level::kInfo:
            return GENIE_LOG_LEVEL_INFO;
        default:
            return GENIE_LOG_LEVEL_VERBOSE;
    }
}

void GenieContext::Reset()
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    ResetUnlocked();
}

void GenieContext::ResetUnlocked()
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle))
    {
        My_Log{} << "reset Genie Dialog failed\n";
    }

    if (watermark_active_ && wm_state_ && wm_state_->vtable && wm_state_->hook)
    {
        wm_state_->vtable->token_hook_reset(wm_state_->hook);
    }
}

void GenieContext::ClearConversationState()
{
    std::lock_guard<std::mutex> lock(query_mutex_);
    ResetUnlocked();
}

void GenieContext::ConfigureCheckpoint(bool enabled, uint64_t max_bytes, int max_checkpoints, bool tail_fallback,
                                       const std::string &dir)
{
    auto &cfg = qnn_ckpt::GlobalConfig();
    cfg.enabled.store(enabled);
    cfg.max_bytes.store(max_bytes);
    cfg.max_checkpoints.store(max_checkpoints);
    cfg.tail_fallback.store(tail_fallback);
    cfg.dir = dir;
}

bool GenieContext::UsesCrossRequestPrefixCache() const
{
    return ckpt_capable_.load() && qnn_ckpt::GlobalConfig().enabled.load();
}

json GenieContext::GetLastQueryTraceMetrics() const
{
    std::lock_guard<std::mutex> lock(trace_metrics_mutex_);
    return last_trace_metrics_;
}

void GenieContext::PublishTraceMetrics(const CheckpointRun &run, size_t prompt_tokens, size_t cur_length,
                                       int64_t ttft_ms)
{
    json m = json::object();
    m["prompt_tokens"] = prompt_tokens;
    m["gen_tokens"] = cur_length > prompt_tokens ? cur_length - prompt_tokens : 0;
    m["prefix_reused_tokens"] = run.reused_tokens;
    m["suffix_mode"] = run.suffix_mode;
    m["ttft_ms"] = ttft_ms;
    m["prefill_ms"] = static_cast<int64_t>(run.prefill_ms);
    m["ckpt_hit"] = run.hit;
    m["ckpt_created"] = run.created;
    m["ckpt_restore_ms"] = static_cast<int64_t>(run.restore_ms);
    m["ckpt_save_ms"] = static_cast<int64_t>(run.save_ms);
    m["ckpt_fallback"] = run.fallback;
    m["ckpt_store_count"] = ckpt_store_.Count();
    m["ckpt_store_bytes"] = ckpt_store_.Bytes();
    std::lock_guard<std::mutex> lock(trace_metrics_mutex_);
    last_trace_metrics_ = std::move(m);
}

bool GenieContext::CheckpointEligible(const char *query_type) const
{
    if (!ckpt_capable_.load() || !qnn_ckpt::GlobalConfig().enabled.load() || m_DialogHandle == nullptr)
    {
        return false;
    }
    const std::string type = query_type;
    return type == "MAINAGENT_INFERENCE" || type == "SUBAGENT_INFERENCE";
}

std::vector<int32_t> GenieContext::EncodeTokens(const std::string &text, bool &ok)
{
    const int32_t *buf = nullptr;
    uint32_t len = 0;
    ok = GenerateTextToken(text, buf, len);
    std::vector<int32_t> out;
    if (ok && buf != nullptr && len > 0)
    {
        out.assign(buf, buf + len);
    }
    if (buf != nullptr)
    {
        free(const_cast<int32_t *>(buf));
    }
    return out;
}

int64_t GenieContext::ReadOccupancy()
{
#if QNN_CKPT_OCCUPANCY_API
    Genie_DataType_t type;
    Genie_Value_t value;
    memset(&value, 0, sizeof(value));
    g_value_alloc = nullptr;
    const Genie_Status_t status = GenieDialog_getValue(
            m_DialogHandle, GENIE_DIALOG_PARAM_CONTEXT_OCCUPANCY,
            [](const size_t size, const char **data)
            {
                g_value_alloc = calloc(1, size);
                *data = static_cast<const char *>(g_value_alloc);
            },
            &type, &value);
    if (g_value_alloc != nullptr)
    {
        free(g_value_alloc);
        g_value_alloc = nullptr;
    }
    if (status != GENIE_STATUS_SUCCESS)
    {
        return -1;
    }
    return static_cast<int64_t>(value.uint32Value);
#else
    return -1;
#endif
}

bool GenieContext::SaveCheckpoint(const std::string &name, uint32_t n_tokens, bool head, double &save_ms)
{
    const auto t0 = std::chrono::steady_clock::now();
    const qnn_ckpt::fs::path tmp = ckpt_store_.NewTempDir();
    std::error_code ec;
    std::string why;
    bool ok = false;
    qnn_ckpt::Entry entry;
    const Genie_Status_t status = GenieDialog_save(m_DialogHandle, tmp.generic_string().c_str());
    const int64_t occupancy = ReadOccupancy();
    if (status != GENIE_STATUS_SUCCESS)
    {
        why = "save_status_" + std::to_string(status);
        qnn_ckpt::fs::remove_all(tmp, ec);
    }
    else if (occupancy <= 0)
    {
        why = "occupancy_unreadable";
        qnn_ckpt::fs::remove_all(tmp, ec);
    }
    else
    {
        ok = ckpt_store_.Commit(tmp, name, static_cast<uint32_t>(occupancy), n_tokens, head, entry, why);
    }
    const double ms = ElapsedMs(t0);
    save_ms += ms;
    if (ok)
    {
        My_Log{} << "[QNN] checkpoint saved: tokens=" << n_tokens << " n_past=" << entry.n_past
                 << " bytes=" << entry.total_bytes << " ms=" << static_cast<int64_t>(ms)
                 << " store_count=" << ckpt_store_.Count() << " store_bytes=" << ckpt_store_.Bytes() << "\n";
    }
    else
    {
        My_Log{My_Log::Level::kWarning} << "[QNN] checkpoint save skipped: tokens=" << n_tokens
                                        << " reason=" << why << "\n";
    }
    return ok;
}

void GenieContext::InvalidateCheckpoint(const std::string &name, const char *why)
{
    ckpt_store_.Remove(name);
    My_Log{My_Log::Level::kWarning} << "[QNN] checkpoint invalidated: reason=" << why
                                    << " store_count=" << ckpt_store_.Count() << "\n";
}

bool GenieContext::RebuildDialog()
{
    My_Log{My_Log::Level::kWarning} << "[QNN] rebuilding the Genie dialog (free + create)\n";
    const auto t0 = std::chrono::steady_clock::now();
    std::lock_guard<std::mutex> handle_lock(dialog_handle_mutex_);
    if (m_DialogHandle != nullptr)
    {
        GenieDialog_free(m_DialogHandle);
        m_DialogHandle = nullptr;
        m_SamplerHandle = nullptr;
    }
    bool faulted = false;
#ifdef _WIN32
    const Genie_Status_t status = GenieDialogCreateGuarded(m_ConfigHandle, &m_DialogHandle, &faulted);
#else
    const Genie_Status_t status = GenieDialog_create(m_ConfigHandle, &m_DialogHandle);
#endif
    if (faulted || status != GENIE_STATUS_SUCCESS || m_DialogHandle == nullptr)
    {
        m_DialogHandle = nullptr;
        ckpt_capable_ = false;
        My_Log{My_Log::Level::kError} << "[QNN] rebuilding the Genie dialog failed: status=" << status << "\n";
        return false;
    }
    if (GENIE_STATUS_SUCCESS != GenieDialog_getSampler(m_DialogHandle, &m_SamplerHandle))
    {
        m_SamplerHandle = nullptr;
        My_Log{My_Log::Level::kWarning} << "[QNN] rebuilt dialog has no sampler handle\n";
    }
    if (m_SamplerHandle != nullptr && !watermark_active_)
    {
        GenieSampler_applyConfig(m_SamplerHandle, m_SamplerConfigHandle);
    }
    bool replay_failed = false;
    if (!stop_sequences_.empty() &&
        GENIE_STATUS_SUCCESS != GenieDialog_setStopSequence(m_DialogHandle, stop_sequences_.c_str()))
    {
        replay_failed = true;
    }
    for (const auto &applied: lora_applied_)
    {
        if (GENIE_STATUS_SUCCESS !=
            GenieDialog_applyLora(m_DialogHandle, applied.first.c_str(), applied.second.c_str()))
        {
            replay_failed = true;
        }
    }
    for (const auto &strength: lora_strengths_)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialog_setLoraStrength(m_DialogHandle, strength.first.first.c_str(),
                                                                strength.first.second.c_str(), strength.second))
        {
            replay_failed = true;
        }
    }
    if (replay_failed)
    {
        ckpt_capable_ = false;
        My_Log{My_Log::Level::kError}
                << "[QNN] replaying the dialog settings after the rebuild failed; prefix checkpoints are disabled "
                   "for this model until it is reloaded\n";
    }
    My_Log{My_Log::Level::kWarning} << "[QNN] Genie dialog rebuilt in " << static_cast<int64_t>(ElapsedMs(t0))
                                    << " ms\n";
    return true;
}

void GenieContext::RefreshCheckpointStore()
{
    namespace fs = std::filesystem;
    std::ostringstream key;
    key << model_config_.get_model_name() << '|' << model_config_.get_model_path() << '|'
        << model_config_.get_context_size() << '|' << model_config_.getloraAdapter() << '|'
        << model_config_.getloraAlpha() << "|genie=" << Genie_getApiMajorVersion() << '.'
        << Genie_getApiMinorVersion() << '.' << Genie_getApiPatchVersion();
    for (const auto &applied: lora_applied_)
    {
        key << "|lora:" << applied.first << ':' << applied.second;
    }
    for (const auto &strength: lora_strengths_)
    {
        key << "|alpha:" << strength.first.first << ':' << strength.first.second << ':' << strength.second;
    }
    std::error_code ec;
    std::vector<std::string> weights;
    for (fs::directory_iterator it(model_config_.get_model_path(), ec), end; !ec && it != end; it.increment(ec))
    {
        std::error_code ec2;
        if (!it->is_regular_file(ec2))
        {
            continue;
        }
        weights.push_back(it->path().filename().string() + ':' + std::to_string(it->file_size(ec2)) + ':' +
                          std::to_string(static_cast<int64_t>(it->last_write_time(ec2).time_since_epoch().count())));
    }
    std::sort(weights.begin(), weights.end());
    for (const auto &w: weights)
    {
        key << "|w:" << w;
    }
    if (!kv_path_.empty())
    {
        const fs::path kv = fs::path(kv_path_) / qnn_ckpt::kKvFile;
        key << "|static:" << fs::file_size(kv, ec) << ':'
            << static_cast<int64_t>(fs::last_write_time(kv, ec).time_since_epoch().count());
    }
    else
    {
        key << "|static:none";
    }
    if (key.str() == ckpt_key_text_)
    {
        return;
    }
    ckpt_key_text_ = key.str();
    const std::string ctx_name = qnn_ckpt::HashString(ckpt_key_text_);
    const std::string &configured = qnn_ckpt::GlobalConfig().dir;
    const fs::path root_base =
        configured.empty() ? fs::path(RootDir.empty() ? fs::current_path(ec).string() : RootDir) : fs::path(configured);
    const fs::path root = root_base / "qnn_prefix_cache";
    ckpt_store_.Open(root, ctx_name);
    My_Log{} << "[QNN] checkpoint store: dir=" << (root / ctx_name).generic_string()
             << " entries=" << ckpt_store_.Count() << " bytes=" << ckpt_store_.Bytes() << "\n";
}

bool GenieContext::PrepareCheckpointedPrefill(const ModelInput &model_input, QueryScope &scope,
                                              const PrefillHeartbeatCallback &heartbeat, const char *query_type,
                                              bool bypass, CheckpointRun &run)
{
    run = CheckpointRun{};
    if (bypass)
    {
        run.fallback = "retry_full_prefill";
        return true;
    }
    if (!CheckpointEligible(query_type))
    {
        return true;
    }
    const std::string &text = model_input.text_;
    const std::vector<size_t> cuts = qnn_ckpt::FindBoundaries(text);
    if (cuts.empty())
    {
        run.fallback = "no_boundary";
        return true;
    }
    bool ok = true;
    const std::vector<int32_t> full = EncodeTokens(text, ok);
    if (!ok || full.empty())
    {
        run.fallback = "encode_failed";
        return true;
    }
    const std::vector<std::string> names = qnn_ckpt::PrefixNames(text, cuts);

    auto verify_cut = [&](size_t i, uint32_t &n_tokens)
    {
        bool encoded = true;
        const std::vector<int32_t> prefix = EncodeTokens(text.substr(0, cuts[i]), encoded);
        if (!encoded || prefix.empty() || prefix.size() >= full.size() ||
            !std::equal(prefix.begin(), prefix.end(), full.begin()) || full[prefix.size()] != ckpt_marker_id_)
        {
            return false;
        }
        n_tokens = static_cast<uint32_t>(prefix.size());
        return true;
    };

    qnn_ckpt::Entry hit;
    int hit_idx = -1;
    for (int i = static_cast<int>(cuts.size()) - 1; i >= 0; --i)
    {
        std::string why;
        if (ckpt_store_.Lookup(names[i], hit, why))
        {
            hit_idx = i;
            break;
        }
        if (!why.empty())
        {
            My_Log{My_Log::Level::kWarning} << "[QNN] checkpoint dropped on lookup: reason=" << why << "\n";
        }
    }
    if (hit_idx >= 0)
    {
        uint32_t n_tokens = 0;
        if (!verify_cut(static_cast<size_t>(hit_idx), n_tokens) || n_tokens != hit.n_tokens)
        {
            InvalidateCheckpoint(hit.name, "token_split_changed");
            run.fallback = "token_split_changed";
            hit_idx = -1;
        }
    }

    std::vector<size_t> want{cuts.size() - 1};
    if (qnn_ckpt::GlobalConfig().tail_fallback.load() && cuts.size() >= 2)
    {
        want.push_back(cuts.size() - 2);
    }
    const int head_idx = qnn_ckpt::HeadIndex(text, cuts);
    if (head_idx >= 0)
    {
        want.push_back(static_cast<size_t>(head_idx));
    }
    std::sort(want.begin(), want.end());
    want.erase(std::unique(want.begin(), want.end()), want.end());
    std::vector<size_t> create;
    for (size_t idx: want)
    {
        if (!model_input.skip_checkpoint_save_ && static_cast<int>(idx) > hit_idx)
        {
            create.push_back(idx);
        }
    }

    const auto t_begin = std::chrono::steady_clock::now();
    bool primed = false;
    bool began = false;
    size_t pos = 0;
    if (hit_idx >= 0)
    {
        if (scope.stopped() || (heartbeat && !heartbeat()))
        {
            return false;
        }
        const auto t_restore = std::chrono::steady_clock::now();
        GenieDialog_reset(m_DialogHandle);
        const Genie_Status_t restore_status = GenieDialog_restore(
                m_DialogHandle, qnn_ckpt::fs::path(hit.dir).generic_string().c_str());
        const int64_t occupancy = restore_status == GENIE_STATUS_SUCCESS ? ReadOccupancy() : -1;
        run.restore_ms = ElapsedMs(t_restore);
        if (restore_status != GENIE_STATUS_SUCCESS || occupancy != static_cast<int64_t>(hit.n_past))
        {
            run.fallback = restore_status != GENIE_STATUS_SUCCESS ? "restore_failed" : "occupancy_mismatch";
            My_Log{My_Log::Level::kWarning} << "[QNN] checkpoint restore rejected: status=" << restore_status
                                            << " occupancy=" << occupancy << " expected=" << hit.n_past << "\n";
            InvalidateCheckpoint(hit.name, run.fallback.c_str());
            if (!RebuildDialog())
            {
                throw std::runtime_error("rebuild genie dialog failed");
            }
            return true;
        }
        ckpt_store_.Touch(hit.name);
        primed = true;
        began = true;
        pos = cuts[static_cast<size_t>(hit_idx)];
        run.hit = true;
        run.hit_name = hit.name;
        run.reused_tokens = hit.n_tokens;
        run.reused_n_past = hit.n_past;
        run.gen_base = hit.n_gen;
        run.checkpoint_at = hit.n_tokens;
    }

    for (size_t idx: create)
    {
        if (scope.stopped() || (heartbeat && !heartbeat()))
        {
            return false;
        }
        uint32_t n_tokens = 0;
        if (!verify_cut(idx, n_tokens))
        {
            run.fallback = "token_split";
            break;
        }
        if (!primed)
        {
            GenieDialog_reset(m_DialogHandle);
            Genie_Status_t static_status = GENIE_STATUS_SUCCESS;
            if (!kv_path_.empty() &&
                GENIE_STATUS_SUCCESS != (static_status = GenieDialog_restore(m_DialogHandle, kv_path_.c_str())))
            {
                throw std::runtime_error("restore kv failed: " + std::to_string(static_status));
            }
            primed = true;
        }
        const std::string segment = text.substr(pos, cuts[idx] - pos);
        const Genie_Status_t status = GenieDialog_query(
                m_DialogHandle, segment.c_str(),
                began ? GENIE_DIALOG_SENTENCE_CONTINUE : GENIE_DIALOG_SENTENCE_BEGIN, NoopDialogCallback, nullptr);
        if (status == GENIE_STATUS_WARNING_ABORTED || scope.stopped())
        {
            return false;
        }
        if (status != GENIE_STATUS_SUCCESS)
        {
            My_Log{My_Log::Level::kWarning} << "[QNN] checkpointed prefill failed: status=" << status << "\n";
            if (run.hit)
            {
                InvalidateCheckpoint(run.hit_name, "prefill_failed");
            }
            const int created = run.created;
            run = CheckpointRun{};
            run.created = created;
            run.fallback = "prefill_failed";
            if (!RebuildDialog())
            {
                throw std::runtime_error("rebuild genie dialog failed");
            }
            return true;
        }
        began = true;
        ++run.gen_base;
        pos = cuts[idx];
        if (SaveCheckpoint(names[idx], n_tokens, static_cast<int>(idx) == head_idx, run.save_ms))
        {
            ++run.created;
            run.checkpoint_at = n_tokens;
        }
    }
    if (!primed)
    {
        return true;
    }
    run.suffix_mode = true;
    ckpt_suffix_ = text.substr(pos);
    run.prefill_ms = ElapsedMs(t_begin) - run.restore_ms;
    return true;
}
