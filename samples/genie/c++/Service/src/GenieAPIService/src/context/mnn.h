//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _MNNBUILDER_H
#define _MNNBUILDER_H

#include "context_base.h"

#include <atomic>

class MNNContext : public ContextBase
{
public:
    explicit MNNContext(const ModelInstanceConfig &config);

    ~MNNContext() override;

    bool Query(const ModelInput &, const Callback& callback,
               PrefillHeartbeatCallback prefill_heartbeat = nullptr) override;

    bool Stop() override;

    json HandleProfile() override;

    size_t TokenLength(const std::string &text) override;

    bool was_stopped_by_output_limit() const override { return last_stopped_by_output_limit_; }

    static uint64_t EstimateMnnMemoryRequirement(const std::string &model_path);

    static uint64_t GetAvailablePhysicalMemoryBytes();

private:
    class Impl;
    Impl* impl_;
    std::atomic<bool> m_stop = false;
    std::atomic<bool> stopped_by_output_limit_{false};
    std::atomic<bool> last_stopped_by_output_limit_{false};
    std::mutex profile_cache_mutex_;
    json last_profile_;
};

#endif
