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
#include "GenieCommon.h"
#include "GenieDialog.h"
#include <vector>
#include "core/context_base.h"

class GenieContext : public ContextBase
{
public:
    explicit GenieContext(const IModelConfig &model_config);

    ~GenieContext() override;

    bool Query(const std::string &prompt, const Callback callback) override;

    bool Stop() override;

    bool SetParams(const std::string max_length,
                   const std::string temp,
                   const std::string top_k,
                   const std::string top_p) override;

    json HandleProfile() override;

    size_t TokenLength(const std::string &text) override;

    bool SetStopSequence(const std::string &stop_sequences) override;

    void applyLora(const std::string engineRole,
                   const std::string loraAdapterName) override;

    void setLoraStrength(const std::string engineRole,
                         const std::unordered_map<std::string, float> &alphaValue) override;


    class Interface;

    Interface *interface_{};

    struct Content
    {
        explicit Content(const QueryType &query_type);

        ~Content();

        union
        {
            std::string prompt_;
            std::vector<uint8_t> embeddings_;
        };

        QueryType query_type_;
    };

private:
    void inference_thread();

    GenieDialogConfig_Handle_t m_ConfigHandle = nullptr;
    GenieDialog_Handle_t m_DialogHandle = nullptr;
    GenieSamplerConfig_Handle_t m_SamplerConfigHandle = NULL;
    GenieSampler_Handle_t m_SamplerHandle = NULL;
    GenieProfile_Handle_t m_ProfileHandle = NULL;
    GenieLog_Handle_t m_LogHandle = nullptr;

    // Inference thread.
    std::unique_ptr<std::thread> m_stream_thread{nullptr};
    std::mutex m_request_lock;
    bool m_request_ready{false};
    std::condition_variable m_request_cond;
    bool m_thread_exit{false};
    bool m_inference_busy{false};
    std::string m_stream_answer;
    std::mutex m_stream_lock;
    Content content;

    class ConfigFixer
    {
    public:
        struct FixedInfo;

        explicit ConfigFixer(const IModelConfig &model_config) : model_config_{model_config}
        {}

        json Execute();

    private:
        bool FixedPath(json &j, FixedInfo &info);

        const IModelConfig &model_config_;
    };
};

#endif
