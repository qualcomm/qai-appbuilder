//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _BUILDER_BASE_H
#define _BUILDER_BASE_H

#include "../model/model_instance_config.h"
#include <atomic>
#include <cstdint>
#include <mutex>
#include <functional>
#include <iomanip>
#include <sstream>
#include <string>

inline std::string FormatFixed2(double v)
{
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(2) << v;
    return oss.str();
}

class ContextBase
{
public:
    using Callback = std::function<bool(std::string &)>;

    using PrefillHeartbeatCallback = std::function<bool()>;

    explicit ContextBase(const ModelInstanceConfig &config) : model_config_{config} {};

    virtual ~ContextBase();

    virtual bool Query(const ModelInput &, const Callback &,
                       PrefillHeartbeatCallback prefill_heartbeat = nullptr) = 0;

    virtual bool Stop();

    bool SetParamsByConfig(const json &j);

    virtual int SetParams(const std::string &key, const std::string &value);

    virtual json HandleProfile() = 0;

    virtual bool SetStopSequence(const std::string &stop_sequences);

    virtual size_t TokenLength(const std::string &text);

    virtual void Reset();

    virtual void ClearConversationState() {}

    bool is_query_busy() const {
        if (query_mutex_.try_lock()) {
            query_mutex_.unlock();
            return false;
        }
        return true;
    }

    virtual void applyLora(const std::string &engineRole, const std::string &loraAdapterName);

    virtual void setLoraStrength(const std::string &engineRole,
                                 const std::unordered_map<std::string, float> &alphaValue);

    virtual bool was_stopped_by_output_limit() const { return false; }

    virtual json GetLastQueryTraceMetrics() const { return nullptr; }

    virtual bool UsesCrossRequestPrefixCache() const { return false; }

    virtual bool SupportsMultimodalInput() const { return false; }

protected:
    virtual int ApplyParams();

    const ModelInstanceConfig &model_config_;

    int max_length_{};

    std::mutex params_mutex_;
    mutable std::mutex query_mutex_;

    std::atomic<bool> destroying_{false};
    std::atomic<int> stop_in_flight_{0};
    std::atomic<int> pending_queries_{0};
    std::atomic<uint64_t> stop_epoch_{0};

    class StopScope
    {
    public:
        explicit StopScope(ContextBase &ctx) : ctx_{ctx}
        {
            ctx_.stop_in_flight_.fetch_add(1);
            admitted_ = !ctx_.destroying_.load();
        }

        ~StopScope() { ctx_.stop_in_flight_.fetch_sub(1); }

        StopScope(const StopScope &) = delete;

        StopScope &operator=(const StopScope &) = delete;

        bool admitted() const { return admitted_; }

    private:
        ContextBase &ctx_;
        bool admitted_{false};
    };

    class QueryScope
    {
    public:
        explicit QueryScope(ContextBase &ctx) : ctx_{ctx}, epoch_{ctx.stop_epoch_.load()}
        {
            ctx_.pending_queries_.fetch_add(1);
            admitted_ = !ctx_.destroying_.load();
        }

        ~QueryScope() { ctx_.pending_queries_.fetch_sub(1); }

        QueryScope(const QueryScope &) = delete;

        QueryScope &operator=(const QueryScope &) = delete;

        bool admitted() const { return admitted_; }

        bool stopped() const { return ctx_.stop_epoch_.load() != epoch_; }

        uint64_t epoch() const { return epoch_; }

    private:
        ContextBase &ctx_;
        uint64_t epoch_;
        bool admitted_{false};
    };

    void MarkDestroying();

    bool WaitForIdle(int timeout_ms);
};

#endif
