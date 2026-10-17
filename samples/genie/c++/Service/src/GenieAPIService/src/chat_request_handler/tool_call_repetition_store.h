//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TOOL_CALL_REPETITION_STORE_H
#define TOOL_CALL_REPETITION_STORE_H

#include "../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <chrono>
#include <cstddef>

class ToolCallRepetitionStore
{
public:
    static ToolCallRepetitionStore& GetInstance();

    void Configure(const ToolCallRepairConfig::RedundantToolCallConfig& cfg);

    static std::string MakeKey(const std::string& session_key, const std::string& model_name);

    int RecordCall(const std::string& key, const std::string& signature);

    int GetRepeatCount(const std::string& key) const;

    void Clear();
    size_t Size() const;

private:
    ToolCallRepetitionStore() = default;
    ~ToolCallRepetitionStore() = default;
    ToolCallRepetitionStore(const ToolCallRepetitionStore&) = delete;
    ToolCallRepetitionStore& operator=(const ToolCallRepetitionStore&) = delete;

    struct Entry
    {
        std::string last_signature;
        int repeat_count = 0;
        std::chrono::steady_clock::time_point last_call_time;
    };

    void EvictIfNeeded();

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;

    size_t max_entries_ = 2000;
    int ttl_seconds_ = 300;
};

#endif
