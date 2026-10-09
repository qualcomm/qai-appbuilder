//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_circuit_breaker_store.h"

ToolCallCircuitBreakerStore& ToolCallCircuitBreakerStore::GetInstance()
{
    static ToolCallCircuitBreakerStore instance;
    return instance;
}

void ToolCallCircuitBreakerStore::Configure(const ToolCallRepairConfig::CircuitBreakerConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    threshold_        = cfg.consecutive_layer3_threshold > 0 ? cfg.consecutive_layer3_threshold : 3;
    cooldown_seconds_ = cfg.cooldown_seconds > 0 ? cfg.cooldown_seconds : 300;
}

std::string ToolCallCircuitBreakerStore::MakeKey(const std::string& session_key, const std::string& model_name)
{
    if (session_key.empty() || model_name.empty())
        return "";
    return session_key + "::" + model_name;
}

int ToolCallCircuitBreakerStore::RecordLayer3Trigger(const std::string& key)
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
        entry.consecutive_layer3_count = 1;
        entry.last_trigger_time = now;
        lru_list_.push_front(key);
        cache_[key] = {entry, lru_list_.begin()};
        return 1;
    }

    // 命中已有条目：先移到 LRU 队首
    lru_list_.erase(it->second.second);
    lru_list_.push_front(key);
    it->second.second = lru_list_.begin();

    Entry& entry = it->second.first;
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - entry.last_trigger_time).count();
    if (elapsed >= cooldown_seconds_)
    {
        // 上一次触发已超出冷却窗口：视为全新的连续序列，重新从 1 计数
        entry.consecutive_layer3_count = 1;
    }
    else
    {
        entry.consecutive_layer3_count += 1;
    }
    entry.last_trigger_time = now;
    return entry.consecutive_layer3_count;
}

void ToolCallCircuitBreakerStore::RecordSuccess(const std::string& key)
{
    if (key.empty())
        return;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it != cache_.end())
    {
        it->second.first.consecutive_layer3_count = 0;
    }
}

bool ToolCallCircuitBreakerStore::ShouldDowngradeToolDeclaration(const std::string& key) const
{
    if (key.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return false;

    const Entry& entry = it->second.first;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - entry.last_trigger_time).count();
    if (elapsed >= cooldown_seconds_)
        return false; // 冷却期已过，自动解除熔断

    return entry.consecutive_layer3_count >= threshold_;
}

int ToolCallCircuitBreakerStore::GetConsecutiveCount(const std::string& key) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return 0;

    const Entry& entry = it->second.first;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - entry.last_trigger_time).count();
    if (elapsed >= cooldown_seconds_)
        return 0; // 冷却期已过，视为这段系统性失败已经过去

    return entry.consecutive_layer3_count;
}

void ToolCallCircuitBreakerStore::Clear()
{
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.clear();
    lru_list_.clear();
}

size_t ToolCallCircuitBreakerStore::Size() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return cache_.size();
}

void ToolCallCircuitBreakerStore::EvictIfNeeded()
{
    while (!lru_list_.empty() && cache_.size() >= max_entries_)
    {
        const std::string& oldest_key = lru_list_.back();
        cache_.erase(oldest_key);
        lru_list_.pop_back();
    }
}
