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
#include <vector>

#include <GenieCommon.h>
#include <GenieDialog.h>
#include "../context_base.h"

class GenieContext : public ContextBase
{
public:
    explicit GenieContext(const IModelConfig &model_config);

    ~GenieContext() override;

    bool Query(const ModelInput &, const Callback &) override;

    bool Stop() override;

    bool SetParams(const std::string &max_length,
                   const std::string &temp,
                   const std::string &top_k,
                   const std::string &top_p) override;

    json HandleProfile() override;

    size_t TokenLength(const std::string &text) override;

    bool SetStopSequence(const std::string &stop_sequences) override;

    void applyLora(const std::string &engineRole,
                   const std::string &loraAdapterName) override;

    void setLoraStrength(const std::string &engineRole,
                         const std::unordered_map<std::string, float> &alphaValue) override;

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
    bool m_request_ready{false};
    std::condition_variable m_request_cond;
    bool m_thread_exit{false};
    bool m_inference_busy{false};
    bool inference_succeed_{true};

    std::string m_stream_answer;
    std::mutex m_stream_lock;
    QInterfaceImpl *inf_impl_{};
};

#endif
