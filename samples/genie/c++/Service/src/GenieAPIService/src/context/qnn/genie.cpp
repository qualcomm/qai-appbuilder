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

    if (prefill_heartbeat && !prefill_heartbeat())
        return false;

    const char *query_type = get_query_type_label(model_input);
    My_Log{} << "[QNN Restore] Query begin: type=" << query_type
             << ", model_name=" << model_config_.get_model_name()
             << ", model_dir=" << m_model_dir_ << std::endl;

    Genie_Status_t status = 0;
    if (!kv_path_.empty() && GENIE_STATUS_SUCCESS != (status = GenieDialog_restore(m_DialogHandle, kv_path_.c_str())))
    {
        throw std::runtime_error("restore kv failed: " + std::to_string(status));
    }

    stopped_by_output_limit_ = false;
    struct LimitCommit
    {
        GenieContext *self;

        ~LimitCommit() { self->last_stopped_by_output_limit_.store(self->stopped_by_output_limit_.load()); }
    } limit_commit{this};
    inf_impl_->inf_->cur_length_ = TokenLengthUnlocked(model_input.text_);
    if (!inf_impl_->inf_->set_content(const_cast<ModelInput &>(model_input)))
    {
        return false;
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
        if (!j["components"].empty() && j["components"][0]["type"] == "dialog")
        {
            const auto &events = j["components"][0]["events"];
            if (!events.empty())
            {
                const auto &last_event = events.back();
                if (last_event.at("type") == "GenieDialog_query")
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
                    result["num_generated_tokens"] = last_event.at("num-generated-tokens")["value"];
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
