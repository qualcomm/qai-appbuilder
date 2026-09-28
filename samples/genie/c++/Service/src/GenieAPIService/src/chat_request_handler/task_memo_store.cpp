//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "task_memo_store.h"

TaskMemoStore& TaskMemoStore::GetInstance()
{
    static TaskMemoStore instance;
    return instance;
}

void TaskMemoStore::Configure(const PromptOptimizationConfig::TaskMemoStoreConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    max_entries_      = cfg.max_entries > 0 ? cfg.max_entries : 200;
    max_memory_bytes_ = cfg.max_memory_mb > 0
                        ? static_cast<size_t>(cfg.max_memory_mb) * 1024 * 1024
                        : 20ULL * 1024 * 1024;
    ttl_minutes_      = cfg.ttl_minutes > 0 ? cfg.ttl_minutes : 120;

    cache_.clear();
    lru_list_.clear();
    total_memory_bytes_ = 0;
}

std::optional<json> TaskMemoStore::Lookup(const std::string& fingerprint, const std::string& first_msg_key)
{
    std::lock_guard<std::mutex> lock(mutex_);
    EvictExpired();

    for (const auto& key : {fingerprint, first_msg_key})
    {
        if (key.empty())
            continue;
        auto it = cache_.find(key);
        if (it == cache_.end())
            continue;

        lru_list_.erase(it->second.second);
        lru_list_.push_front(key);
        it->second.second = lru_list_.begin();
        return it->second.first.memo;
    }
    return std::nullopt;
}

void TaskMemoStore::Put(const std::string& fingerprint, const std::string& first_msg_key, const json& memo)
{
    if (fingerprint.empty() && first_msg_key.empty())
        return;

    std::lock_guard<std::mutex> lock(mutex_);
    size_t memory_bytes = memo.dump().size() + sizeof(Entry) + 64;

    for (const auto& key : {fingerprint, first_msg_key})
    {
        if (!key.empty())
            InsertOneKey(key, memo, memory_bytes);
    }
}

void TaskMemoStore::InsertOneKey(const std::string& key, const json& memo, size_t memory_bytes)
{
    auto it = cache_.find(key);
    if (it != cache_.end())
    {
        total_memory_bytes_ -= it->second.first.memory_bytes;
        lru_list_.erase(it->second.second);
        cache_.erase(it);
    }

    EvictIfNeeded();

    Entry entry;
    entry.memo          = memo;
    entry.created       = std::chrono::steady_clock::now();
    entry.memory_bytes  = memory_bytes;

    lru_list_.push_front(key);
    cache_[key] = {std::move(entry), lru_list_.begin()};
    total_memory_bytes_ += cache_[key].first.memory_bytes;
}

void TaskMemoStore::Clear()
{
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.clear();
    lru_list_.clear();
    total_memory_bytes_ = 0;
}

size_t TaskMemoStore::Size() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return cache_.size();
}

void TaskMemoStore::EvictExpired()
{
    auto now = std::chrono::steady_clock::now();
    auto ttl = std::chrono::minutes(ttl_minutes_);

    auto it = lru_list_.end();
    while (it != lru_list_.begin())
    {
        --it;
        auto cache_it = cache_.find(*it);
        if (cache_it == cache_.end())
        {
            it = lru_list_.erase(it);
            continue;
        }
        if (now - cache_it->second.first.created >= ttl)
        {
            total_memory_bytes_ -= cache_it->second.first.memory_bytes;
            cache_.erase(cache_it);
            it = lru_list_.erase(it);
        }
    }
}

void TaskMemoStore::EvictIfNeeded()
{
    while (!lru_list_.empty() &&
           (cache_.size() >= max_entries_ || total_memory_bytes_ >= max_memory_bytes_))
    {
        const std::string& oldest_key = lru_list_.back();
        auto it = cache_.find(oldest_key);
        if (it != cache_.end())
        {
            total_memory_bytes_ -= it->second.first.memory_bytes;
            cache_.erase(it);
        }
        lru_list_.pop_back();
    }
}
