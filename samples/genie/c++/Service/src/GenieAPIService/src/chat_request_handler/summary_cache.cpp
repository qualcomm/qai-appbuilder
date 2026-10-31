//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "summary_cache.h"
#include "log.h"
#include <functional>

SummaryCache& SummaryCache::GetInstance()
{
    static SummaryCache instance;
    return instance;
}

void SummaryCache::Configure(const PromptOptimizationConfig::LongTextSummaryCacheConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    enabled_           = cfg.enabled;
    max_entries_       = cfg.max_entries > 0 ? cfg.max_entries : 500;
    max_memory_bytes_  = cfg.max_memory_mb > 0
                         ? static_cast<size_t>(cfg.max_memory_mb) * 1024 * 1024
                         : 50ULL * 1024 * 1024;
    ttl_minutes_       = cfg.ttl_minutes > 0 ? cfg.ttl_minutes : 60;

    cache_.clear();
    lru_list_.clear();
    total_memory_bytes_ = 0;
}

SummaryCache::CacheKey SummaryCache::MakeKey(const std::string& content) const
{
    CacheKey key;
    key.hash_val    = std::hash<std::string>{}(content);
    key.original_len = content.size();
    return key;
}

bool SummaryCache::Lookup(const std::string& content, std::string& summary)
{
    if (!enabled_) return false;

    std::lock_guard<std::mutex> lock(mutex_);

    EvictExpired();

    CacheKey key = MakeKey(content);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return false;

    if (it->second.first.original_len != content.size())
        return false;

    lru_list_.erase(it->second.second);
    lru_list_.push_front(key);
    it->second.second = lru_list_.begin();

    summary = it->second.first.summary;
    return true;
}

void SummaryCache::Put(const std::string& content, const std::string& summary)
{
    if (!enabled_) return;

    if (summary.empty() || summary.size() >= content.size())
    {
        My_Log{My_Log::Level::kDebug}
            << "[SummaryCache] Skip put: summary.size()=" << summary.size()
            << " >= content.size()=" << content.size()
            << " (no benefit)" << std::endl;
        return;
    }

    std::lock_guard<std::mutex> lock(mutex_);

    CacheKey key = MakeKey(content);

    auto it = cache_.find(key);
    if (it != cache_.end())
    {
        total_memory_bytes_ -= it->second.first.memory_bytes;
        lru_list_.erase(it->second.second);
        cache_.erase(it);
    }

    EvictIfNeeded();

    Entry entry;
    entry.summary      = summary;
    entry.original_len = content.size();
    entry.created      = std::chrono::steady_clock::now();

    entry.memory_bytes = sizeof(CacheKey) + summary.size() + sizeof(Entry) + 64;

    lru_list_.push_front(key);
    cache_[key] = {std::move(entry), lru_list_.begin()};
    total_memory_bytes_ += cache_[key].first.memory_bytes;
}

void SummaryCache::Clear()
{
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.clear();
    lru_list_.clear();
    total_memory_bytes_ = 0;
}

size_t SummaryCache::Size() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return cache_.size();
}

void SummaryCache::EvictExpired()
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

void SummaryCache::EvictIfNeeded()
{

    while (!lru_list_.empty() &&
           (cache_.size() >= max_entries_ || total_memory_bytes_ >= max_memory_bytes_))
    {
        const CacheKey& oldest_key = lru_list_.back();
        auto it = cache_.find(oldest_key);
        if (it != cache_.end())
        {
            total_memory_bytes_ -= it->second.first.memory_bytes;
            cache_.erase(it);
        }
        lru_list_.pop_back();
    }
}
