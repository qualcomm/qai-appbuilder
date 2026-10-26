//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_repetition_store.h"
#include <unordered_set>

ToolCallRepetitionStore& ToolCallRepetitionStore::GetInstance()
{
    static ToolCallRepetitionStore instance;
    return instance;
}

void ToolCallRepetitionStore::Configure(const ToolCallRepairConfig::RedundantToolCallConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    ttl_seconds_ = cfg.ttl_seconds > 0 ? cfg.ttl_seconds : 300;
    non_contiguous_ttl_seconds_ = cfg.non_contiguous_ttl_seconds > 0 ? cfg.non_contiguous_ttl_seconds : 3600;
}

std::string ToolCallRepetitionStore::MakeKey(const std::string& session_key, const std::string& model_name)
{
    if (session_key.empty() || model_name.empty())
        return "";
    return session_key + "::" + model_name;
}

size_t ToolCallRepetitionStore::CountRepeatsInWindow(const std::vector<std::string>& keys, size_t window)
{
    if (window == 0 || keys.empty())
        return 0;
    const size_t begin = keys.size() > window ? keys.size() - window : 0;
    std::unordered_set<std::string> seen(keys.begin(), keys.begin() + begin);
    size_t repeats = 0;
    for (size_t i = begin; i < keys.size(); ++i)
    {
        if (!keys[i].empty() && !seen.insert(keys[i]).second)
            ++repeats;
    }
    return repeats;
}

void ToolCallRepetitionStore::RecordSignatureTotal(Entry& entry, const std::string& signature)
{
    auto sit = entry.signature_totals.find(signature);
    if (sit == entry.signature_totals.end())
    {
        if (entry.signature_totals.size() >= kMaxSignaturesPerEntry && !entry.signature_order.empty())
        {
            entry.signature_totals.erase(entry.signature_order.front());
            entry.signature_order.pop_front();
        }
        entry.signature_totals[signature] = 1;
        entry.signature_order.push_back(signature);
    }
    else
    {
        sit->second += 1;
    }
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
        entry.non_contiguous_last_active_time = now;
        RecordSignatureTotal(entry, signature);
        lru_list_.push_front(key);
        cache_[key] = {entry, lru_list_.begin()};
        return 1;
    }

    lru_list_.erase(it->second.second);
    lru_list_.push_front(key);
    it->second.second = lru_list_.begin();

    Entry& entry = it->second.first;
    auto elapsed_contiguous = std::chrono::duration_cast<std::chrono::seconds>(now - entry.last_call_time).count();
    if (elapsed_contiguous >= ttl_seconds_)
    {
        entry.repeat_count = 1;
        entry.last_signature = signature;
    }
    else if (entry.last_signature != signature)
    {
        entry.repeat_count = 1;
        entry.last_signature = signature;
    }
    else
    {
        entry.repeat_count += 1;
    }
    entry.last_call_time = now;

    auto elapsed_non_contiguous =
            std::chrono::duration_cast<std::chrono::seconds>(now - entry.non_contiguous_last_active_time).count();
    if (elapsed_non_contiguous >= non_contiguous_ttl_seconds_)
    {
        entry.signature_totals.clear();
        entry.signature_order.clear();
    }
    entry.non_contiguous_last_active_time = now;

    RecordSignatureTotal(entry, signature);
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

int ToolCallRepetitionStore::GetNonContiguousRepeatCount(const std::string& key) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return 0;

    const Entry& entry = it->second.first;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - entry.non_contiguous_last_active_time).count();
    if (elapsed >= non_contiguous_ttl_seconds_)
        return 0;

    auto total_it = entry.signature_totals.find(entry.last_signature);
    return total_it == entry.signature_totals.end() ? 0 : total_it->second;
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
