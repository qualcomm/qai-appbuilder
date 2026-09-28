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
#include <chrono>
#include <optional>
#include <cstddef>

using json = nlohmann::ordered_json;

// ============================================================
// TaskMemoStore：进程内 Task Memo（结构化任务备忘录）LRU 缓存（单例）
//
// key   = 会话指纹（历史前缀哈希 fingerprint 或首条 user 消息哈希 first_msg_key，
//         技法对齐 GenieRoutingGateway::ComputeHistoryFingerprint/ComputeFirstMsgKey，
//         由 TaskMemoBuilder 本地独立计算，不依赖 GenieRoutingGateway 模块）
// value = Task Memo JSON + 写入时间
//
// 结构对齐 SummaryCache（LRU + TTL + 内存上限）。Put() 同时把同一份 memo 注册到
// fingerprint 与 first_msg_key 两个 key 下（各占一条独立条目，与
// gateway_session.cpp::RegisterSessionEntry 的双 key 语义一致）；Lookup() 按
// fingerprint 优先、未命中再查 first_msg_key 的顺序查找。
// ============================================================
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
        std::chrono::steady_clock::time_point created;
        size_t memory_bytes = 0;
    };

    void EvictExpired();
    void EvictIfNeeded();

    // 无锁插入单个 key（调用方已持有 mutex_），Put() 对 fingerprint/first_msg_key 各调用一次
    void InsertOneKey(const std::string& key, const json& memo, size_t memory_bytes);

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;
    size_t total_memory_bytes_ = 0;

    size_t max_entries_ = 200;
    size_t max_memory_bytes_ = 20ULL * 1024 * 1024;
    int ttl_minutes_ = 120;
};

#endif // TASK_MEMO_STORE_H
