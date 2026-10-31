//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TOOL_CALL_CIRCUIT_BREAKER_STORE_H
#define TOOL_CALL_CIRCUIT_BREAKER_STORE_H

#include "../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <cstddef>

class ToolCallCircuitBreakerStore
{
public:
    static ToolCallCircuitBreakerStore& GetInstance();

    void Configure(const ToolCallRepairConfig::CircuitBreakerConfig& cfg);

    static std::string MakeKey(const std::string& session_key, const std::string& model_name);

    int RecordLayer3Trigger(const std::string& key);

    void RecordSuccess(const std::string& key);

    bool ShouldDowngradeToolDeclaration(const std::string& key) const;

    int GetConsecutiveCount(const std::string& key) const;

    void Clear();

    size_t Size() const;

private:
    ToolCallCircuitBreakerStore() = default;
    ~ToolCallCircuitBreakerStore() = default;
    ToolCallCircuitBreakerStore(const ToolCallCircuitBreakerStore&) = delete;
    ToolCallCircuitBreakerStore& operator=(const ToolCallCircuitBreakerStore&) = delete;

    struct Entry {
        int consecutive_layer3_count = 0;
    };

    void EvictIfNeeded();

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;

    size_t max_entries_ = 2000;
    int threshold_ = 3;
};

#endif
