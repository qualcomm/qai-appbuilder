//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_COMPLEXITY_EVALUATOR_H
#define TASK_COMPLEXITY_EVALUATOR_H

#include "content_security_types.h"
#include "../../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>

// ============================================================
// TaskComplexityEvaluator：任务复杂性评估器
// 启发式规则（token 数量、tool_call 数量、对话轮数、关键词）；
// 可选本地模型辅助判断（use_local_model_fallback=true 时启用）
// ============================================================
class TaskComplexityEvaluator
{
public:
    explicit TaskComplexityEvaluator(const RoutingConfig::ComplexityConfig &config,
                                      IModelConfig *model_config = nullptr);

    // 主评估入口
    ComplexityResult Evaluate(const json &messages,
                               const json &tools,
                               const InspectionContext &ctx);

private:
    // 启发式评估
    ComplexityResult HeuristicEvaluate(const json &messages, const json &tools);

    // 估算 token 数量（简单字符数估算，1 token ≈ 4 字符）
    int EstimateTokenCount(const json &messages, const json &tools);

    // 统计最新一次 user 请求之后的 tool_calls 数量
    int CountToolCallsAfterLastUser(const json &messages);

    // 检测复杂任务关键词，返回对应的复杂性等级（C0/C1/C2）
    ComplexityLevel CheckComplexKeywords(const json &messages);

    // 从 messages 中提取所有文本内容
    std::string ExtractAllText(const json &messages);

    // 从 messages 中提取最新一条 user 消息的文本内容
    // 用于复杂度评估：只根据用户最新输入判断任务复杂度，避免历史消息干扰
    std::string ExtractLastUserMessage(const json &messages);

    // ---- 本地模型辅助判断 ----

    // 调用本地模型进行辅助复杂性判断
    // task_summary: 待判断的任务摘要（截断至合理长度）
    // timeout_ms: 超时时间（毫秒）
    // 返回 true 表示成功，result 填充模型判断结果
    bool LocalModelEvaluate(const std::string &task_summary,
                             int timeout_ms,
                             ComplexityLevel &result_level,
                             std::string &reason);

    // 提取任务摘要（截断至合理长度）
    std::string ExtractTaskSummary(const json &messages,
                                   const json &tools) const;

    // 计算文本内容哈希（用于 LRU 缓存 key）
    static std::string ComputeHash(const std::string &text);

    // LRU 缓存：内容哈希 → 模型判断结果
    struct CacheEntry {
        ComplexityLevel level;
        std::string reason;
    };

    // LRU 缓存操作（线程安全）
    bool CacheLookup(const std::string &key, CacheEntry &entry);
    void CacheInsert(const std::string &key, const CacheEntry &entry);

    RoutingConfig::ComplexityConfig config_;
    IModelConfig *model_config_;

    // LRU 缓存（最多 64 条，不缓存原文）
    static constexpr size_t MAX_CACHE_SIZE = 64;
    mutable std::mutex cache_mutex_;
    std::list<std::pair<std::string, CacheEntry>> cache_list_;
    std::unordered_map<std::string, std::list<std::pair<std::string, CacheEntry>>::iterator> cache_map_;
};

#endif // TASK_COMPLEXITY_EVALUATOR_H
