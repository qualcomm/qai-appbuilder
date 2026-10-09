//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_repetition_store.h"

ToolCallRepetitionStore& ToolCallRepetitionStore::GetInstance()
{
    static ToolCallRepetitionStore instance;
    return instance;
}

void ToolCallRepetitionStore::Configure(const ToolCallRepairConfig::RedundantToolCallConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    ttl_seconds_ = cfg.ttl_seconds > 0 ? cfg.ttl_seconds : 300;
}

std::string ToolCallRepetitionStore::MakeKey(const std::string& session_key, const std::string& model_name)
{
    if (session_key.empty() || model_name.empty())
        return "";
    return session_key + "::" + model_name;
}

int ToolCallRepetitionStore::RecordCall(const std::string& key, const std::string& signature)
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    auto now = std::chrono::steady_clock::now();

    auto it = cache_.find(key);
    if (it == cache_.end())
    {
        EvictIfNeeded();
        Entry entry;
        entry.last_signature = signature;
        entry.repeat_count = 1;
        entry.last_call_time = now;
        lru_list_.push_front(key);
        cache_[key] = {entry, lru_list_.begin()};
        return 1;
    }

    lru_list_.erase(it->second.second);
    lru_list_.push_front(key);
    it->second.second = lru_list_.begin();

    Entry& entry = it->second.first;
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - entry.last_call_time).count();
    if (elapsed >= ttl_seconds_ || entry.last_signature != signature)
    {
        entry.repeat_count = 1;
        entry.last_signature = signature;
    }
    else
    {
        entry.repeat_count += 1;
    }
    entry.last_call_time = now;
    return entry.repeat_count;
}

int ToolCallRepetitionStore::GetRepeatCount(const std::string& key) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return 0;

    const Entry& entry = it->second.first;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - entry.last_call_time).count();
    if (elapsed >= ttl_seconds_)
        return 0;

    return entry.repeat_count;
}

void ToolCallRepetitionStore::Clear()
{
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.clear();
    lru_list_.clear();
}

size_t ToolCallRepetitionStore::Size() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return cache_.size();
}

void ToolCallRepetitionStore::EvictIfNeeded()
{
    while (!lru_list_.empty() && cache_.size() >= max_entries_)
    {
        const std::string& oldest_key = lru_list_.back();
        cache_.erase(oldest_key);
        lru_list_.pop_back();
    }
}
