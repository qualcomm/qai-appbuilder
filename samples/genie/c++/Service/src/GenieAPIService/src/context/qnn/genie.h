//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _GENIEBUILDER_H
#define _GENIEBUILDER_H

#include <thread>
#include <mutex>
#include <atomic>
#include <condition_variable>
#include <vector>
#include <chrono>
#include <string>

#include <GenieCommon.h>
#include <GenieDialog.h>
#include <GenieSampler.h>
#include "../context_base.h"
#include "log.h"

struct QnnWatermarkState;

class GenieContext : public ContextBase
{
public:
    explicit GenieContext(const ModelInstanceConfig &config);

    ~GenieContext() override;

    bool Query(const ModelInput &, const Callback &,
               PrefillHeartbeatCallback prefill_heartbeat = nullptr) override;

    bool SupportsMultimodalInput() const override;

    bool Stop() override;

    int SetParams(const std::string &key, const std::string &value) override
    {

        if (watermark_active_ &&
            (key == "temp" || key == "top-k" || key == "top_k" ||
             key == "top-p" || key == "top_p" || key == "greedy"))
        {
            My_Log{My_Log::Level::kWarning}
                    << "[Watermark] ignoring runtime sampler override '" << key
                    << "' while token-level watermark (custom sampler) is active\n";
            return GENIE_STATUS_SUCCESS;
        }
        return GenieSamplerConfig_setParam(m_SamplerConfigHandle, key.c_str(), value.c_str());
    }

    int ApplyParams() override
    {

        if (watermark_active_)
        {
            return GENIE_STATUS_SUCCESS;
        }
        return GenieSampler_applyConfig(m_SamplerHandle, m_SamplerConfigHandle);
    }

    json HandleProfile() override;

    size_t TokenLength(const std::string &text) override;

    bool SetStopSequence(const std::string &stop_sequences) override;

    void applyLora(const std::string &engineRole,
                   const std::string &loraAdapterName) override;

    void setLoraStrength(const std::string &engineRole,
                         const std::unordered_map<std::string, float> &alphaValue) override;

    void Reset() override;

    void ClearConversationState() override;

    struct QInterfaceImpl;

    class ConfigFixer;

private:
    void inference_thread();

    size_t TokenLengthUnlocked(const std::string &text);

    void ResetUnlocked();

    bool AbortInference();

    void ReleaseHandles();

    void ReleaseWatermarkState();

    static GenieLog_Level_t get_genie_log_level();

    bool GenerateTextToken(const std::string &text, const int32_t *&buf, uint32_t &len);

    GenieDialogConfig_Handle_t m_ConfigHandle = nullptr;
    GenieDialog_Handle_t m_DialogHandle = nullptr;
    GenieSamplerConfig_Handle_t m_SamplerConfigHandle = nullptr;
    GenieSampler_Handle_t m_SamplerHandle = nullptr;
    GenieProfile_Handle_t m_ProfileHandle = nullptr;
    GenieLog_Handle_t m_LogHandle = nullptr;

    std::unique_ptr<std::thread> m_stream_thread{nullptr};
    std::mutex m_request_lock;

    std::atomic<bool> m_request_ready{false};
    std::condition_variable m_request_cond;
    std::atomic<bool> m_thread_exit{false};
    std::atomic<bool> m_inference_busy{false};
    std::atomic<bool> inference_succeed_{true};
    std::atomic<bool> busy_after_timeout_{false};
    std::mutex profile_cache_mutex_;
    json last_profile_;

    std::string m_stream_answer;
    std::mutex m_stream_lock;
    std::condition_variable m_stream_cond;
    QInterfaceImpl *inf_impl_{};

    bool watermark_active_{false};
    QnnWatermarkState *wm_state_{nullptr};

    std::atomic<bool> stopped_by_output_limit_{false};
    std::atomic<bool> last_stopped_by_output_limit_{false};
    std::string m_model_dir_;
    std::string kv_path_;

public:
    bool was_stopped_by_output_limit() const override { return last_stopped_by_output_limit_; }

    const std::string &get_model_name() const { return model_config_.get_model_name(); }

    const char *get_query_type_label(const ModelInput &model_input) const
    {
        const std::string &prompt = model_input.text_;
        if (prompt.find("You are a security classifier") != std::string::npos)
            return "SECURITY_CHECK";
        if (prompt.find("You are a task complexity classifier") != std::string::npos)
            return "COMPLEXITY_CHECK";
        if (prompt.find("You are a redaction assistant") != std::string::npos)
            return "DESENSITIZE";

        if (prompt.find("You are a compression assistant") != std::string::npos)
            return "SUMMARIZATION";
        if (model_input.agent_type_ == "main")
            return "MAINAGENT_INFERENCE";
        return "SUBAGENT_INFERENCE";
    }

    static constexpr int HEARTBEAT_INTERVAL_MS = 5000;
};

#endif
