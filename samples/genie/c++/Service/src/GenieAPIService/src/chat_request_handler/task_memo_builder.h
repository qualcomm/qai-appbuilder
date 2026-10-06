//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_MEMO_BUILDER_H
#define TASK_MEMO_BUILDER_H

#include "../model/model_config.h"
#include "../model/model_instance_config.h"
#include "../chat_history/chat_history.h"
#include "task_memo_store.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <functional>
#include <optional>
#include <utility>

using json = nlohmann::ordered_json;

// ============================================================
// TaskMemoBuilder：结构化任务备忘录（Task Memo）的规则层抽取 + 模型层按需深度总结
//
// 职责：
//   1. 用会话指纹（ComputeFingerprint/ComputeFirstMsgKey，技法对齐
//      GenieRoutingGateway::ComputeHistoryFingerprint/ComputeFirstMsgKey，本地独立
//      实现）从 TaskMemoStore 查表取得上一份权威备忘录；查不到即视为冷启动。
//   2. 规则层从即将被丢弃的消息中抽取确定性事实（工具调用结果/用户陈述/TODO）。
//   3. 满足触发条件时，复用 LongTextSummarizer 同款 InferFn 基建调用模型做一次
//      深度总结，用结构化 JSON 覆盖规则层结果；解析失败时静默回退到规则层结果。
//   4. 更新后写回 TaskMemoStore，供下一轮查表与 BuildSystemContext 渲染。
// ============================================================
class TaskMemoBuilder
{
public:
    using InferFn = std::function<std::string(const std::string& prompt)>;
    using IsAliveFn = std::function<bool()>;

    struct UpdateResult {
        bool active = false;
        double confidence = 0.0;
        size_t refresh_count = 0;
        size_t pages_total = 0;  // 轻量分页目录当前条目数（供 PromptLedger/X-Genie-Prompt-Memo-Pages 响应头透传）
        std::string compact_render;
    };

    TaskMemoBuilder(
        const PromptOptimizationConfig::TaskMemoConfig& config,
        const ModelInstanceConfig& instance_config,
        TaskMemoStore* store,
        InferFn infer_fn,
        IsAliveFn is_alive_fn = nullptr
    );

    static std::string ComputeFingerprint(const json& messages);
    static std::string ComputeFirstMsgKey(const json& messages);

    // 查表（不触发任何更新），供 BuildSystemContext 直接渲染
    std::optional<json> Lookup(const json& raw_messages) const;

    // 消息即将被丢弃前调用：更新备忘录并写回 store
    UpdateResult Update(const json& raw_messages, const std::vector<GenieChatMessage>& dropped_messages);

    // max_chars=0 表示不限；否则按 Completed/Next actions/Facts/Open questions/Tool state
    // 优先级从高到低整块保留，预算不足时从最低优先级开始整块丢弃（不做块内截断）。
    static std::string Render(const json& entry, size_t max_chars = 0);
    static std::string RenderCompact(const json& entry);

private:
    json BuildRuleLayer(const json& prev, const std::vector<GenieChatMessage>& dropped_messages) const;
    bool ShouldTriggerModelLayer(const std::vector<GenieChatMessage>& dropped_messages,
                                  bool has_prev, double prev_confidence) const;
    std::string BuildModelPrompt(const json& prev, const json& rule_layer_result) const;
    bool TryParseModelOutput(const std::string& raw, json& out) const;

    // 原始目标锚点鲁棒抓取：在前 config_.goal_scan_window 条 role=="user" 的消息里，
    // 取第一条长度 ≥ config_.min_goal_signal_chars 的作为锚点（高置信度）；全部不达标
    // 时回退取第一条并标记低置信度。返回 {截断后的锚点文本, 是否高置信度}。
    // 只在 Update() 中"entry 尚未捕获过锚点"时调用一次，捕获后通过 entry 字段透传，
    // 永不重新调用覆盖。
    std::pair<std::string, bool> ExtractGoalAnchor(const json& raw_messages) const;

    const PromptOptimizationConfig::TaskMemoConfig& config_;
    const ModelInstanceConfig& instance_config_;
    TaskMemoStore* store_;
    InferFn infer_fn_;
    IsAliveFn is_alive_fn_;
};

#endif // TASK_MEMO_BUILDER_H
