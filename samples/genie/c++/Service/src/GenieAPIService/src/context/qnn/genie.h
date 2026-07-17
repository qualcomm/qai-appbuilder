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
#include "../context_base.h"

class GenieContext : public ContextBase
{
public:
    explicit GenieContext(const ModelInstanceConfig &config);

    ~GenieContext() override;

    bool Query(const ModelInput &, const Callback &,
               PrefillHeartbeatCallback prefill_heartbeat = nullptr) override;

    bool Stop() override;

    int SetParams(const std::string &key, const std::string &value) override
    {
        return GenieSamplerConfig_setParam(m_SamplerConfigHandle, key.c_str(), value.c_str());
    }

    int ApplyParams() override
    {
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

    struct QInterfaceImpl;

    class ConfigFixer;

private:
    void inference_thread();

    static GenieLog_Level_t get_genie_log_level();

    bool GenerateTextToken(const std::string &text, const int32_t *&buf, uint32_t &len);

    GenieDialogConfig_Handle_t m_ConfigHandle = nullptr;
    GenieDialog_Handle_t m_DialogHandle = nullptr;
    GenieSamplerConfig_Handle_t m_SamplerConfigHandle = nullptr;
    GenieSampler_Handle_t m_SamplerHandle = nullptr;
    GenieProfile_Handle_t m_ProfileHandle = nullptr;
    GenieLog_Handle_t m_LogHandle = nullptr;

    // Inference thread.
    std::unique_ptr<std::thread> m_stream_thread{nullptr};
    std::mutex m_request_lock;
    // [修复] 以下四个变量在主线程（Query/析构）和推理线程之间并发读写，
    // 必须声明为 std::atomic<bool> 以避免数据竞争（data race）UB。
    // 在 ARM 弱内存模型（Android/Snapdragon）上，普通 bool 的跨线程读写
    // 可能导致主线程 while(m_inference_busy) 永远读到缓存旧值，造成死循环。
    std::atomic<bool> m_request_ready{false};
    std::condition_variable m_request_cond;
    std::atomic<bool> m_thread_exit{false};
    std::atomic<bool> m_inference_busy{false};
    std::atomic<bool> inference_succeed_{true};

    std::string m_stream_answer;
    std::mutex m_stream_lock;
    std::condition_variable m_stream_cond;  // Condition variable for stream data
    QInterfaceImpl *inf_impl_{};

    // Fix 5: track whether generation was stopped due to output token limit
    bool stopped_by_output_limit_{false};
    std::string m_model_dir_;
    std::string kv_path_;

public:
    // Override ContextBase::was_stopped_by_output_limit()
    bool was_stopped_by_output_limit() const override { return stopped_by_output_limit_; }

    // Expose model name for logging in QInterfaceImpl
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
        // Phase -1 长文本摘要推理：system prompt 固定为 "You are a compression assistant"
        // 需要与 SECURITY_CHECK / COMPLEXITY_CHECK / DESENSITIZE 一样执行 post-query reset，
        // 防止摘要推理的 KV cache 状态污染后续主推理。
        if (prompt.find("You are a compression assistant") != std::string::npos)
            return "SUMMARIZATION";
        // 使用 ModelInput 中由 ModelInputBuilder 预先检测并设置的 agent_type_ 字段
        // "main" = 主 Agent，"sub" = 子 Agent（默认）
        if (model_input.agent_type_ == "main")
            return "MAINAGENT_INFERENCE";
        return "SUBAGENT_INFERENCE";
    }

    // Heartbeat interval: send keep-alive message to client if no output for this duration
    static constexpr int HEARTBEAT_INTERVAL_MS = 5000;
};

#endif
