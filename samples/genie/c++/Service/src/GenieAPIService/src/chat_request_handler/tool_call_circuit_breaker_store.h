//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TOOL_CALL_CIRCUIT_BREAKER_STORE_H
#define TOOL_CALL_CIRCUIT_BREAKER_STORE_H

#include "../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <chrono>
#include <cstddef>

// ============================================================
// ToolCallCircuitBreakerStore：进程内"会话+模型"维度 Layer3 连续触发计数 LRU 缓存（单例）
//
// key   = session_key + "::" + model_name（session_key 建议传
//         TaskMemoBuilder::ComputeFirstMsgKey(messages)——只依赖首条 user 消息内容
//         哈希，跨同一会话的多轮请求保持稳定，与 ComputeFingerprint()（每轮变化，
//         不适用于本场景）不同；模型名用 ModelInstanceConfig::get_model_name()）
// value = 连续触发 Layer3 的次数 + 最近一次触发时间
//
// 语义：替换原 ResponseDispatcher::consecutive_unknow_tool_calls_（生命周期与单次
// HTTP 请求的 ResponseDispatcher 实例绑定，无法跨请求可靠累积，是已知的设计缺陷）。
// 只在真正走到 Layer3 终态（Layer0/1/2 均未能恢复出合法工具调用）时计数递增；
// Layer0/1/2 任一层成功 → RecordSuccess() 清零。连续次数达到
// circuit_breaker.consecutive_layer3_threshold 且仍在 cooldown_seconds 窗口内时，
// ShouldDowngradeToolDeclaration() 返回 true，由 ModelInputBuilder::Build() 据此
// 跳过本次请求的工具声明注入（相当于告知模型"当前不支持工具调用"）。
//
// 结构对齐 TaskMemoStore（LRU + TTL，单 mutex 保护）。与 TaskMemoStore 的区别：
// 条目本身是 POD（int + timestamp），不需要按内存字节数淘汰，只按条目数上限淘汰。
// ============================================================
class ToolCallCircuitBreakerStore
{
public:
    static ToolCallCircuitBreakerStore& GetInstance();

    void Configure(const ToolCallRepairConfig::CircuitBreakerConfig& cfg);

    // 拼接 session_key 与 model_name 得到复合 key；任一为空则返回空字符串
    // （调用方应据此跳过熔断计数——无法可靠归属会话/模型时不参与熔断判定）。
    static std::string MakeKey(const std::string& session_key, const std::string& model_name);

    // Layer3 真正触发（Layer0/1/2 均未能恢复出合法工具调用）时调用。
    // 若上一次触发距今已超过 cooldown_seconds，视为新的连续序列（计数重置为 1）。
    // 返回值：调用后的最新连续计数（供日志使用）。key 为空时不计数，返回 0。
    int RecordLayer3Trigger(const std::string& key);

    // Layer0/1/2 任一层成功恢复出合法工具调用时调用：清零该 key 的连续计数，
    // 打断"连续触发"的判定序列。key 为空时不做任何事。
    void RecordSuccess(const std::string& key);

    // 查询该 key 当前是否应降级 system prompt（跳过工具声明注入）：
    // 连续计数达到阈值，且距最近一次触发未超过 cooldown_seconds。
    // key 为空、或查无记录时返回 false（不熔断，与改动前行为一致）。
    bool ShouldDowngradeToolDeclaration(const std::string& key) const;

    // 只读旁路接口：查询该 key 当前的连续 Layer3 触发计数，供 TaskMemoBuilder 等
    // 外部只读消费方使用。不 mutate 任何状态（不触碰 LRU 顺序、不修改条目）。
    // 语义与 ShouldDowngradeToolDeclaration() 保持一致：key 为空、查无记录、或已超过
    // cooldown_seconds（视为"这段系统性失败已经过去"）时统一返回 0。
    int GetConsecutiveCount(const std::string& key) const;

    void Clear();

    size_t Size() const;

private:
    ToolCallCircuitBreakerStore() = default;
    ~ToolCallCircuitBreakerStore() = default;
    ToolCallCircuitBreakerStore(const ToolCallCircuitBreakerStore&) = delete;
    ToolCallCircuitBreakerStore& operator=(const ToolCallCircuitBreakerStore&) = delete;

    struct Entry {
        int consecutive_layer3_count = 0;
        std::chrono::steady_clock::time_point last_trigger_time;
    };

    void EvictIfNeeded();

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;

    size_t max_entries_ = 2000;
    int threshold_ = 3;
    int cooldown_seconds_ = 300;
};

#endif // TOOL_CALL_CIRCUIT_BREAKER_STORE_H
