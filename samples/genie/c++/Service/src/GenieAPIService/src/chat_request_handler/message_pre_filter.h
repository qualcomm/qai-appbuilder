//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MESSAGE_PRE_FILTER_H
#define MESSAGE_PRE_FILTER_H

#include "../model/model_config.h"
#include "../context/context_base.h"
#include "../chat_history/chat_history.h"
#include "prompt_stats_helper.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <unordered_map>
#include <functional>
#include <optional>
#include <memory>
#include <array>

// 消息压缩配置
struct MessageCompressionConfig {
    size_t max_messages = 16;           // 最大消息数量
    size_t keep_recent_full = 6;        // 保持完整的最新消息数
};

// 优化后的消息列表
struct OptimizedMessages {
    std::vector<GenieChatMessage> messages;
    size_t total_tokens;
    size_t dropped_count;
    bool success;
    std::string error_message;

    OptimizedMessages() : total_tokens(0), dropped_count(0), success(false) {}
};

// ========== 消息预过滤器 ==========
// 封装 Prompt 压缩相关的所有逻辑，包括：
//   - 分级压缩算法（Phase 0~5）
//   - 消息数量限制（SmartSelectMessages）
//   - 工具调用序列验证（ValidateToolCallSequence）
//   - 各类辅助函数（SafeEraseToolChain、DropOldMessagesBatch 等）
class MessagePreFilter {
public:
    // 多模型并发场景：构造函数直接接受 ContextBase* 参数，避免后续 SetContext 调用
    explicit MessagePreFilter(IModelConfig& model_config, ContextBase* context = nullptr);

    // 设置 per-model 的 ContextBase（多模型并发场景）
    // 保留此方法用于运行时切换 context（可选）
    void SetContext(ContextBase* context) { context_override_ = context; }

    // ========== 主入口：将消息适配到上下文大小（Token 控制和消息丢弃）==========
    // 注意：消息内容压缩应在 PreFilterMessages 中完成，本函数只负责丢弃整条消息。
    //
    // 保护策略：
    //   - 最后一条 user 消息（last_user_index）必须保留
    //   - last_user_index 之后的消息受保护，不可丢弃
    //   - 只丢弃 last_user_index 之前的旧消息
    //
    // 两阶段丢弃策略：
    //   - 第一阶段：优先丢弃 recent_boundary 之前的旧消息
    //   - 第二阶段：若第一阶段仍不满足，再丢弃 recent_boundary 之后（但 last_user_index 之前）的消息
    OptimizedMessages FitMessagesToContext(
        const std::vector<GenieChatMessage>& all_messages,
        const std::string& system_prompt,
        size_t context_size,
        const MessageCompressionConfig& config = MessageCompressionConfig());

    // ========== 主入口：分级压缩预过滤 ==========
    // 算法设计：预算驱动 + 分级压缩，只在超出预算时才压缩/丢弃消息
    //
    // 保护集（Protected Set，永不丢弃、永不截断）：
    //   - 最后一条 user 消息（当前请求）
    //   - 最后一条 assistant final 消息（上一轮回复）
    //   - 最近一轮工具调用链（最后一个 assistant tool_call + 其后的 tool responses）
    //
    // 分级压缩顺序（从无损到有损）：
    //   Phase 0: 无损清理（始终执行）→ 检查预算
    //   Phase 1: 旧消息处理（recent_window 之外）→ 检查预算
    //     Step A: 压缩旧 user/assistant 消息内容（old_compress_len）
    //     Step B: 若仍超预算，删除旧消息（DropOldMessagesBatch）
    //     Step C: 若仍超预算，压缩旧 tool 响应（old_compress_len）
    //   Phase 2: 新消息处理（recent_window 之内）→ 检查预算
    //     Step A: 压缩新 user/assistant 消息内容（recent_compress_len）
    //     Step B: 若仍超预算，压缩新 tool 响应（tool_compress_len）
    //   Phase 3: 丢弃新消息（最后手段，跳过保护集）
    //   兜底：返回当前结果，让 FitMessagesToContext 处理剩余溢出
    //
    // 参数：
    //   msg:                          待过滤的消息列表（原地修改）
    //   contextSize:                  可用 token 数（已减去预留输出空间）
    //   system_prompt_for_token_calc: 用于 token 预算计算的系统提示词（含工具定义和 /think 标记）
    //   is_harmony:                   是否为 Harmony 路径（影响 tool 消息 token 估算口径）
    //   tool_call_id_to_name:         tool_call_id → 函数名映射（Harmony 路径使用，可为 nullptr）
    nlohmann::ordered_json PreFilterMessages(
        nlohmann::ordered_json& msg,
        int contextSize,
        const std::string& system_prompt_for_token_calc = "",
        bool is_harmony = false,
        const std::unordered_map<std::string, std::string>* tool_call_id_to_name = nullptr);

    // 获取上次 PreFilterMessages 调用的统计信息（只读）
    const MessageCategoryStats& GetStats() const { return prefilter_stats_; }

    // ========== 辅助函数：对任意消息 content 做统一文本清洗（块级元数据 + 时间戳）==========
    std::string CleanMessageContent(const std::string& content);

private:
    // ========== 辅助函数：判断是否是工具相关消息 ==========
    bool IsToolRelatedMessage(const nlohmann::ordered_json& element, const std::string& role);

    // ========== 辅助函数：移除 Tool 返回内容中的冗余安全警告（Phase 0）==========
    std::string RemoveToolResponseRedundancy(const std::string& content);

    // ========== 辅助函数：过滤 user 消息中的非指令元数据块（Phase 0）==========
    std::string RemoveUntrustedMetadataBlocks(const std::string& content);

    // ========== 辅助函数：智能选择消息（Step 1：消息数量限制）==========
    nlohmann::ordered_json SmartSelectMessages(const nlohmann::ordered_json& msg, size_t max_messages);

    // ========== 辅助函数：验证工具调用序列（Step 4）==========
    nlohmann::ordered_json ValidateToolCallSequence(const nlohmann::ordered_json& msg);

    // ========== 辅助函数：级联删除工具调用链 ==========
    size_t SafeEraseToolChain(nlohmann::ordered_json& messages, size_t assistant_idx);

    // ========== 辅助方法：检查消息是否应该保持完整（不压缩）==========
    bool ShouldKeepFull(const GenieChatMessage& msg) const;

    // ========== 辅助方法：计算 token 数量 ==========
    size_t CountTokens(const std::string& text);

    // ========== 辅助函数：批量删除旧消息直到满足预算（Phase 1 专用）==========
    void DropOldMessagesBatch(
        nlohmann::ordered_json& filtered_msg,
        const std::vector<bool>& old_flags,
        const std::function<bool(const nlohmann::ordered_json&, size_t)>& is_protected,
        std::shared_ptr<ContextBase> handle,
        size_t& current_tokens,
        size_t available_tokens,
        const std::function<void()>& refresh_prot_idx);

    // ========== 通用辅助函数：压缩指定角色的消息内容（Phase 1A/1C/2A/2B）==========
    size_t CompressMessages(
        nlohmann::ordered_json& filtered_msg,
        const std::vector<bool>& old_flags,
        const std::function<bool(const nlohmann::ordered_json&)>& within_budget,
        const std::function<bool(const nlohmann::ordered_json&, size_t)>& is_truncate_protected,
        const std::function<std::string(const std::string&, size_t)>& truncate_content,
        const std::string& target_role,
        bool only_old,
        size_t max_len,
        size_t tool_min_length,
        const std::string& phase_name,
        std::shared_ptr<ContextBase> handle = nullptr,
        size_t* current_tokens = nullptr,
        size_t available_tokens = 0,
        bool is_harmony = false);

    IModelConfig& model_config_;
    // 多模型并发场景：per-model 的 ContextBase（优先于 model_config_.get_genie_model_handle()）
    ContextBase* context_override_{nullptr};
    MessageCategoryStats prefilter_stats_;
};

#endif // MESSAGE_PRE_FILTER_H

