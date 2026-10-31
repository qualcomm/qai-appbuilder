//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_MEMO_STORE_H
#define TASK_MEMO_STORE_H

#include "../model/model_config.h"
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <optional>
#include <cstddef>

using json = nlohmann::ordered_json;

class TaskMemoStore
{
public:
    static TaskMemoStore& GetInstance();

    void Configure(const PromptOptimizationConfig::TaskMemoStoreConfig& cfg);

    std::optional<json> Lookup(const std::string& fingerprint, const std::string& first_msg_key);

    void Put(const std::string& fingerprint, const std::string& first_msg_key, const json& memo);

    void Clear();

    size_t Size() const;

private:
    TaskMemoStore() = default;
    ~TaskMemoStore() = default;
    TaskMemoStore(const TaskMemoStore&) = delete;
    TaskMemoStore& operator=(const TaskMemoStore&) = delete;

    struct Entry {
        json memo;
        size_t memory_bytes = 0;
    };

    void EvictIfNeeded();

    void InsertOneKey(const std::string& key, const json& memo, size_t memory_bytes);

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;
    size_t total_memory_bytes_ = 0;

    size_t max_entries_ = 200;
    size_t max_memory_bytes_ = 20ULL * 1024 * 1024;
};

#endif
