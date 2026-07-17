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

// ── 单例 ──────────────────────────────────────────────────────────────────────

SummaryCache& SummaryCache::GetInstance()
{
    static SummaryCache instance;
    return instance;
}

// ── Configure ─────────────────────────────────────────────────────────────────

void SummaryCache::Configure(const PromptOptimizationConfig::LongTextSummaryCacheConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    enabled_           = cfg.enabled;
    max_entries_       = cfg.max_entries > 0 ? cfg.max_entries : 500;
    max_memory_bytes_  = cfg.max_memory_mb > 0
                         ? static_cast<size_t>(cfg.max_memory_mb) * 1024 * 1024
                         : 50ULL * 1024 * 1024;
    ttl_minutes_       = cfg.ttl_minutes > 0 ? cfg.ttl_minutes : 60;

    // 配置变更后清空旧缓存，避免旧条目以错误的 TTL 继续存活
    cache_.clear();
    lru_list_.clear();
    total_memory_bytes_ = 0;
}

// ── MakeKey ───────────────────────────────────────────────────────────────────

SummaryCache::CacheKey SummaryCache::MakeKey(const std::string& content) const
{
    CacheKey key;
    key.hash_val    = std::hash<std::string>{}(content);
    key.original_len = content.size();
    return key;
}

// ── Lookup ────────────────────────────────────────────────────────────────────

bool SummaryCache::Lookup(const std::string& content, std::string& summary)
{
    if (!enabled_) return false;

    std::lock_guard<std::mutex> lock(mutex_);

    // 先清理过期条目
    EvictExpired();

    CacheKey key = MakeKey(content);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return false;

    // 碰撞保护：再次验证 original_len
    if (it->second.first.original_len != content.size())
        return false;

    // 命中：将该条目移到 LRU 链表头部（最近访问）
    lru_list_.erase(it->second.second);
    lru_list_.push_front(key);
    it->second.second = lru_list_.begin();

    summary = it->second.first.summary;
    return true;
}

// ── Put ───────────────────────────────────────────────────────────────────────

void SummaryCache::Put(const std::string& content, const std::string& summary)
{
    if (!enabled_) return;

    // 保护规则：摘要为空或摘要不比原文短，则不写入
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

    // 若已存在则先移除旧条目（更新语义）
    auto it = cache_.find(key);
    if (it != cache_.end())
    {
        total_memory_bytes_ -= it->second.first.memory_bytes;
        lru_list_.erase(it->second.second);
        cache_.erase(it);
    }

    // 淘汰超出限制的条目
    EvictIfNeeded();

    // 写入新条目
    Entry entry;
    entry.summary      = summary;
    entry.original_len = content.size();
    entry.created      = std::chrono::steady_clock::now();
    // 内存估算：key 结构 + 摘要字符串 + 原文长度字段 + 链表节点开销
    entry.memory_bytes = sizeof(CacheKey) + summary.size() + sizeof(Entry) + 64;

    lru_list_.push_front(key);
    cache_[key] = {std::move(entry), lru_list_.begin()};
    total_memory_bytes_ += cache_[key].first.memory_bytes;
}

// ── Clear ─────────────────────────────────────────────────────────────────────

void SummaryCache::Clear()
{
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.clear();
    lru_list_.clear();
    total_memory_bytes_ = 0;
}

// ── Size ──────────────────────────────────────────────────────────────────────

size_t SummaryCache::Size() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return cache_.size();
}

// ── EvictExpired ──────────────────────────────────────────────────────────────

void SummaryCache::EvictExpired()
{
    // 注意：调用方已持有 mutex_，此处不再加锁
    auto now = std::chrono::steady_clock::now();
    auto ttl = std::chrono::minutes(ttl_minutes_);

    // 从 LRU 链表尾部（最久未访问）向前扫描，移除过期条目
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

// ── EvictIfNeeded ─────────────────────────────────────────────────────────────

void SummaryCache::EvictIfNeeded()
{
    // 注意：调用方已持有 mutex_，此处不再加锁
    // 按 LRU 顺序从尾部淘汰，直到满足数量和内存约束
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
