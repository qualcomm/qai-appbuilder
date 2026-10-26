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

    bool was_stopped_by_output_limit() const override { return stopped_by_output_limit_; }

    json GetLastQueryTraceMetrics() const override;

    static void SetPrefixCacheEnabled(bool enabled);

    static void SetDegenerateRepeatGuardEnabled(bool enabled);

    static void SetDegenerateRepeatGuardMaxPeriod(int max_period);

    // n = prompt_engineering.json 的 llama_prefix_cache.head_checkpoints；
    // from_config = 该键是否**显式**出现（而不是取了默认值）。显式给出后，规划器推导的值不再覆盖它。
    static void SetPrefixHeadCheckpoints(int n, bool from_config);

    // 应用规划器按主机可用内存推导出的检查点份数。显式配置存在时返回 false 且不改动。
    static bool ApplyPlannedHeadCheckpoints(int n);

    bool UsesCrossRequestPrefixCache() const override;

    void ClearConversationState() override;

private:
    class Impl;

    Impl *impl_;
    bool stopped_by_output_limit_ = false;
};

#endif //LLAMA_CPP_H
