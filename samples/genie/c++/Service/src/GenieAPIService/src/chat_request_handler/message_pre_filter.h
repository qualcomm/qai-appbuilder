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
#include "task_memo_builder.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <functional>
#include <optional>
#include <memory>
#include <array>

struct MessageCompressionConfig {
    size_t max_messages = 16;
    size_t keep_recent_full = 6;
};

struct OptimizedMessages {
    std::vector<GenieChatMessage> messages;
    size_t total_tokens;
    size_t dropped_count;
    bool success;
    std::string error_message;
    bool emergency_truncated;
    bool memo_active;
    double memo_confidence;
    size_t memo_refresh_count;
    size_t memo_pages_total;

    OptimizedMessages() : total_tokens(0), dropped_count(0), success(false), emergency_truncated(false),
                           memo_active(false), memo_confidence(0.0), memo_refresh_count(0), memo_pages_total(0) {}
};

class MessagePreFilter {
public:
    explicit MessagePreFilter(IModelConfig& model_config, ContextBase* context = nullptr);

    void SetContext(ContextBase* context) { context_override_ = context; }

    OptimizedMessages FitMessagesToContext(
        const std::vector<GenieChatMessage>& all_messages,
        const std::string& system_prompt,
        size_t context_size,
        const MessageCompressionConfig& config = MessageCompressionConfig(),
        const nlohmann::ordered_json* raw_messages_for_memo = nullptr,
        TaskMemoBuilder* memo_builder = nullptr);

    nlohmann::ordered_json PreFilterMessages(
        nlohmann::ordered_json& msg,
        int contextSize,
        const std::string& system_prompt_for_token_calc = "",
        bool is_harmony = false,
        const std::unordered_map<std::string, std::string>* tool_call_id_to_name = nullptr);

    const MessageCategoryStats& GetStats() const { return prefilter_stats_; }

    const std::vector<GenieChatMessage>& GetDroppedMessages() const { return prefilter_dropped_messages_; }

    std::string CleanMessageContent(const std::string& content);

private:
    bool IsToolRelatedMessage(const nlohmann::ordered_json& element, const std::string& role);

    std::string RemoveToolResponseRedundancy(const std::string& content);

    std::string RemoveUntrustedMetadataBlocks(const std::string& content);

    nlohmann::ordered_json SmartSelectMessages(const nlohmann::ordered_json& msg, size_t max_messages);

    nlohmann::ordered_json ValidateToolCallSequence(const nlohmann::ordered_json& msg);

    size_t SafeEraseToolChain(nlohmann::ordered_json& messages, size_t assistant_idx);

    void CaptureDroppedMessages(const nlohmann::ordered_json& original, const nlohmann::ordered_json& kept);

    bool ShouldKeepFull(const GenieChatMessage& msg) const;

    size_t CountTokens(const std::string& text);

    bool PrefixCacheActive();

    void DropOldMessagesBatch(
        nlohmann::ordered_json& filtered_msg,
        const std::vector<bool>& old_flags,
        const std::function<bool(const nlohmann::ordered_json&, size_t)>& is_protected,
        std::shared_ptr<ContextBase> handle,
        size_t& current_tokens,
        size_t available_tokens,
        const std::function<void()>& refresh_prot_idx);

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
    ContextBase* context_override_{nullptr};
    MessageCategoryStats prefilter_stats_;
    std::vector<GenieChatMessage> prefilter_dropped_messages_;
    std::unordered_set<size_t> repeat_protected_raw_;
    std::unordered_set<size_t> repeat_must_keep_raw_;
    size_t repeat_retain_budget_{0};
};

void ClearPreFilterSessionStates();

std::string CanonicalToolCallKey(const nlohmann::ordered_json& tool_call);

#endif // MESSAGE_PRE_FILTER_H
