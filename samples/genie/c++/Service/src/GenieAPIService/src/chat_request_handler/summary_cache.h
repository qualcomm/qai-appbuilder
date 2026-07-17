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

// ============================================================
// SummaryCache：进程内长文本摘要 LRU 缓存（单例）
//
// key  = hash(content) + original_len（双重碰撞保护）
// value = 摘要文本 + 原文长度 + 访问时间
//
// 淘汰策略：
//   1. LRU（超出 max_entries 时淘汰最久未访问条目）
//   2. TTL（超过 ttl_minutes 的条目视为过期）
//   3. 内存上限（超出 max_memory_mb 时淘汰最旧条目）
//
// 线程安全：使用 std::mutex 全局锁（摘要推理耗时远高于锁开销）
// ============================================================
class SummaryCache
{
public:
    // 获取全局单例
    static SummaryCache& GetInstance();

    // 用最新配置重新初始化缓存（在 ModelManager 加载配置后调用）
    void Configure(const PromptOptimizationConfig::LongTextSummaryCacheConfig& cfg);

    // 查找缓存
    // 返回 true 表示命中，summary 被填充；返回 false 表示未命中
    bool Lookup(const std::string& content, std::string& summary);

    // 写入缓存
    // 若 summary 为空或 summary 长度 >= original 长度，则不写入（保护规则）
    void Put(const std::string& content, const std::string& summary);

    // 清空所有缓存条目（用于测试或配置变更后强制刷新）
    void Clear();

    // 当前缓存条目数（用于日志/诊断）
    size_t Size() const;

private:
    SummaryCache() = default;
    ~SummaryCache() = default;
    SummaryCache(const SummaryCache&) = delete;
    SummaryCache& operator=(const SummaryCache&) = delete;

    // 缓存条目
    struct Entry {
        std::string summary;                            // 摘要文本
        size_t original_len = 0;                        // 原文长度（碰撞保护）
        std::chrono::steady_clock::time_point created;  // 创建时间（TTL 计算）
        size_t memory_bytes = 0;                        // 本条目占用内存估算（字节）
    };

    // 缓存 key：hash + original_len 双重保护
    struct CacheKey {
        size_t hash_val = 0;
        size_t original_len = 0;

        bool operator==(const CacheKey& other) const {
            return hash_val == other.hash_val && original_len == other.original_len;
        }
    };

    struct CacheKeyHash {
        size_t operator()(const CacheKey& k) const {
            // 组合两个 size_t 的哈希
            size_t h = k.hash_val;
            h ^= k.original_len + 0x9e3779b9 + (h << 6) + (h >> 2);
            return h;
        }
    };

    // 计算内容的 hash key
    CacheKey MakeKey(const std::string& content) const;

    // 淘汰过期条目（TTL）
    void EvictExpired();

    // 淘汰最旧条目直到满足内存/数量约束
    void EvictIfNeeded();

    mutable std::mutex mutex_;

    // LRU 链表：front = 最近访问，back = 最久未访问
    std::list<CacheKey> lru_list_;

    // key → (Entry, LRU 链表迭代器)
    std::unordered_map<CacheKey, std::pair<Entry, std::list<CacheKey>::iterator>, CacheKeyHash> cache_;

    // 当前总内存占用（字节）
    size_t total_memory_bytes_ = 0;

    // 配置（由 Configure() 更新）
    bool enabled_ = false;
    size_t max_entries_ = 500;
    size_t max_memory_bytes_ = 50ULL * 1024 * 1024;  // 50 MB
    int ttl_minutes_ = 60;
};

#endif // SUMMARY_CACHE_H
