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
#include <vector>

class ToolCallRepetitionStore
{
public:
    static ToolCallRepetitionStore& GetInstance();

    void Configure(const ToolCallRepairConfig::RedundantToolCallConfig& cfg);

    static std::string MakeKey(const std::string& session_key, const std::string& model_name);

    static size_t CountRepeatsInWindow(const std::vector<std::string>& keys, size_t window);

    int RecordCall(const std::string& key, const std::string& signature);

    int GetRepeatCount(const std::string& key) const;

    int GetNonContiguousRepeatCount(const std::string& key) const;

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
        std::chrono::steady_clock::time_point non_contiguous_last_active_time;
        std::unordered_map<std::string, int> signature_totals;
        std::list<std::string> signature_order;
    };

    static constexpr size_t kMaxSignaturesPerEntry = 64;

    static void RecordSignatureTotal(Entry& entry, const std::string& signature);

    void EvictIfNeeded();

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;

    size_t max_entries_ = 2000;
    int ttl_seconds_ = 300;
    int non_contiguous_ttl_seconds_ = 3600;
};

#endif
