//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LONG_TEXT_SUMMARIZER_H
#define LONG_TEXT_SUMMARIZER_H

#include "../model/model_config.h"
#include "../model/model_instance_config.h"
#include "summary_cache.h"
#include <nlohmann/json.hpp>
#include <string>
#include <functional>
#include <vector>

using json = nlohmann::ordered_json;

// ============================================================
// LongTextSummarizer：Phase -1 长文本摘要化处理器
//
// 职责：
//   1. 在消息数组中找到目标消息（最后一条 user 文本消息 + 末尾连续 tool 消息链）
//   2. 判断是否超过触发阈值
//   3. 执行缓存查找
//   4. 缓存未命中时执行 Map-Reduce 分块摘要
//   5. 用摘要替换原始 content（仅当摘要比原文短时）
//
// 不负责：
//   - 直接修改模型参数
//   - 处理 response 流
//   - 接管现有 PreFilter / Fit 逻辑
// ============================================================
class LongTextSummarizer
{
public:
    // infer_fn：接受完整 prompt 字符串，返回模型输出文本（失败时返回空串）
    using InferFn = std::function<std::string(const std::string& prompt)>;

    // is_alive_fn：检测客户端连接是否仍然存活。
    // 返回 true = 连接正常，继续摘要；返回 false = 连接已断开，提前终止。
    // 传入 nullptr 表示不检测（非 stream 路径）。
    using IsAliveFn = std::function<bool()>;

    LongTextSummarizer(
        const PromptOptimizationConfig::LongTextSummarizationConfig& config,
        const ModelInstanceConfig& instance_config,
        SummaryCache* cache,
        InferFn infer_fn,
        size_t context_size,
        IsAliveFn is_alive_fn = nullptr
    );

    // 对 messages 数组执行 Phase -1 摘要化处理
    // 直接修改 messages 中目标消息的 content 字段（仅当摘要成功且更短时）
    void ProcessMessages(json& messages);

private:
    // ── 目标消息识别 ──────────────────────────────────────────

    // 规则 A：找到最后一条 role==user 且 content 为字符串的消息索引
    // 返回 -1 表示未找到
    int FindLastUserTextMessage(const json& messages) const;

    // 规则 B：从末尾向前扫描连续 role==tool 的消息，返回起始索引
    // 返回 -1 表示末尾没有 tool 消息
    int FindTrailingToolChainStart(const json& messages) const;

    // ── 摘要执行 ─────────────────────────────────────────────

    // 对单条内容执行 Map-Reduce 摘要
    // 返回摘要文本；失败或无收益时返回空串
    std::string Summarize(const std::string& content, const std::string& source_hint);

    // Map 阶段：对单个分块生成摘要
    std::string MapChunk(const std::string& chunk, int chunk_idx, int total_chunks,
                         const std::string& source_hint);

    // Reduce 阶段：合并多个分块摘要
    std::string ReduceSummaries(const std::vector<std::string>& summaries,
                                const std::string& source_hint);

    // ── Prompt 构造 ───────────────────────────────────────────

    // 构造 Map 阶段的完整推理 prompt
    std::string BuildMapPrompt(const std::string& chunk, int chunk_idx, int total_chunks,
                               const std::string& source_hint) const;

    // 构造 Reduce 阶段的完整推理 prompt
    std::string BuildReducePrompt(const std::string& combined_summaries,
                                  const std::string& source_hint) const;

    // ── 成员变量 ──────────────────────────────────────────────

    const PromptOptimizationConfig::LongTextSummarizationConfig& config_;
    const ModelInstanceConfig& instance_config_;
    SummaryCache* cache_;       // 非拥有指针，生命周期由调用方保证
    InferFn infer_fn_;
    IsAliveFn is_alive_fn_;     // 连接存活检测回调（nullptr = 不检测）
    size_t context_size_;       // 当前请求可用上下文大小（tokens）
    size_t trigger_threshold_;  // 触发阈值 = context_size * trigger_ratio
    size_t chunk_token_limit_;  // 分块大小 = context_size * chunk_ratio
};

#endif // LONG_TEXT_SUMMARIZER_H
