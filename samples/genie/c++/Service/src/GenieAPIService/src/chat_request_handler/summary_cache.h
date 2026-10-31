//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SUMMARY_CACHE_H
#define SUMMARY_CACHE_H

#include "../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <chrono>
#include <cstddef>

class SummaryCache
{
public:

    static SummaryCache& GetInstance();

    void Configure(const PromptOptimizationConfig::LongTextSummaryCacheConfig& cfg);

    bool Lookup(const std::string& content, std::string& summary);

    void Put(const std::string& content, const std::string& summary);

    void Clear();

    size_t Size() const;

private:
    SummaryCache() = default;
    ~SummaryCache() = default;
    SummaryCache(const SummaryCache&) = delete;
    SummaryCache& operator=(const SummaryCache&) = delete;

    struct Entry {
        std::string summary;
        size_t original_len = 0;
        std::chrono::steady_clock::time_point created;
        size_t memory_bytes = 0;
    };

    struct CacheKey {
        size_t hash_val = 0;
        size_t original_len = 0;

        bool operator==(const CacheKey& other) const {
            return hash_val == other.hash_val && original_len == other.original_len;
        }
    };

    struct CacheKeyHash {
        size_t operator()(const CacheKey& k) const {

            size_t h = k.hash_val;
            h ^= k.original_len + 0x9e3779b9 + (h << 6) + (h >> 2);
            return h;
        }
    };

    CacheKey MakeKey(const std::string& content) const;

    void EvictExpired();

    void EvictIfNeeded();

    mutable std::mutex mutex_;

    std::list<CacheKey> lru_list_;

    std::unordered_map<CacheKey, std::pair<Entry, std::list<CacheKey>::iterator>, CacheKeyHash> cache_;

    size_t total_memory_bytes_ = 0;

    bool enabled_ = false;
    size_t max_entries_ = 500;
    size_t max_memory_bytes_ = 50ULL * 1024 * 1024;
    int ttl_minutes_ = 60;
};

#endif
