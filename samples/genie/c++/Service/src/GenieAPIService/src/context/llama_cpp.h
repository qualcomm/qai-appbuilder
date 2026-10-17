//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_CPP_H
#define LLAMA_CPP_H

#include "context_base.h"

class TargetWeightsLoadError : public std::runtime_error
{
public:
    using std::runtime_error::runtime_error;
};

class LLAMACppBuilder : public ContextBase
{
public:
    explicit LLAMACppBuilder(const ModelInstanceConfig &config);

    ~LLAMACppBuilder() override;

    bool Query(const ModelInput &model_input, const Callback &callback,
               PrefillHeartbeatCallback prefill_heartbeat = nullptr) override;

    bool Stop() override;

    size_t TokenLength(const std::string &text) override;

    json HandleProfile() override;

    int SetParams(const std::string &key, const std::string &value) override;

    // Fix: 报告是否因输出 token 限制而停止生成
    bool was_stopped_by_output_limit() const override { return stopped_by_output_limit_; }

    json GetLastQueryTraceMetrics() const override;

    static void SetPrefixCacheEnabled(bool enabled);

    static void SetPrefixHeadCheckpoints(int n);

    bool UsesCrossRequestPrefixCache() const override;

private:
    class Impl;

    Impl *impl_;
    bool stopped_by_output_limit_ = false;
};

#endif //LLAMA_CPP_H
