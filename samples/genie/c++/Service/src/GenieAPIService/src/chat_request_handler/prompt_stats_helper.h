//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PROMPT_STATS_HELPER_H
#define PROMPT_STATS_HELPER_H

#include "log.h"
#include "utils.h"
#include "../chat_history/chat_history.h"
#include <string>
#include <sstream>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

static const std::string BLANK_STRING = "";

struct MessageCategoryStats {
    size_t system_tokens = 0;
    size_t user_count = 0;
    size_t user_tokens = 0;
    size_t assistant_count = 0;
    size_t assistant_tokens = 0;
    size_t tool_count = 0;
    size_t tool_tokens = 0;
    size_t tools_definition_tokens = 0;
    size_t total_tokens = 0;

    size_t full_kept_count = 0;
    size_t truncated_count = 0;
    size_t total_kept_count = 0;

    size_t dropped_by_smart_select = 0;
    size_t dropped_by_token_limit = 0;
    size_t total_dropped = 0;

    size_t messages_dropped_prefix_len = 0;
    std::string prefix_reset_reason = "none";

    void Reset() {
        system_tokens = 0;
        user_count = 0;
        user_tokens = 0;
        assistant_count = 0;
        assistant_tokens = 0;
        tool_count = 0;
        tool_tokens = 0;
        tools_definition_tokens = 0;
        total_tokens = 0;
        full_kept_count = 0;
        truncated_count = 0;
        total_kept_count = 0;
        dropped_by_smart_select = 0;
        dropped_by_token_limit = 0;
        total_dropped = 0;
        messages_dropped_prefix_len = 0;
        prefix_reset_reason = "none";
    }
};

namespace PromptStatsHelper {

inline void PrintStatsHeader(const std::string& title) {
    My_Log{}.original(true) << "\n";
    My_Log{My_Log::Level::kInfo} << "╔═══════════════════════════════════════════════════════════════════════════╗" << std::endl;

    size_t total_width = 75;
    size_t title_len = 0;

    for (size_t i = 0; i < title.length(); ) {
        unsigned char c = static_cast<unsigned char>(title[i]);
        if (c < 0x80) {

            title_len += 1;
            i += 1;
        } else if ((c & 0xE0) == 0xC0) {

            title_len += 2;
            i += 2;
        } else if ((c & 0xF0) == 0xE0) {

            title_len += 2;
            i += 3;
        } else if ((c & 0xF8) == 0xF0) {

            title_len += 2;
            i += 4;
        } else {

            i += 1;
        }
    }

    size_t left_padding = (total_width - title_len) / 2;
    size_t right_padding = total_width - title_len - left_padding;

    if (left_padding > 1000 || right_padding > 1000) {
        My_Log{My_Log::Level::kError} << "[PrintStatsHeader] Padding too large! left: " << left_padding
                                       << ", right: " << right_padding << ", title_len: " << title_len << std::endl;
        left_padding = std::min(left_padding, (size_t)10);
        right_padding = std::min(right_padding, (size_t)10);
    }

    My_Log{My_Log::Level::kInfo} << "║" << std::string(left_padding, ' ') << title
                                  << std::string(right_padding, ' ') << "║" << std::endl;
    My_Log{My_Log::Level::kInfo} << "╚═══════════════════════════════════════════════════════════════════════════╝" << std::endl;
}

inline void PrintMessageCategoryStats(const MessageCategoryStats& stats, bool is_before_optimization = false) {
    My_Log{My_Log::Level::kInfo} << "  消息分类统计:" << std::endl;
    if (stats.system_tokens > 0) {
        My_Log{My_Log::Level::kInfo} << "    - System Prompt:  " << stats.system_tokens << " tokens" << std::endl;
    }

    if (stats.tools_definition_tokens > 0 && is_before_optimization) {

        My_Log{My_Log::Level::kInfo} << "    - Tools 定义:     " << stats.tools_definition_tokens << " tokens" << std::endl;
    }
    if (stats.user_count > 0) {
        My_Log{My_Log::Level::kInfo} << "    - User 消息:      " << stats.user_count << " 条, "
                                      << stats.user_tokens << " tokens" << std::endl;
    }
    if (stats.assistant_count > 0) {
        My_Log{My_Log::Level::kInfo} << "    - Assistant 消息: " << stats.assistant_count << " 条, "
                                      << stats.assistant_tokens << " tokens" << std::endl;
    }
    if (stats.tool_count > 0) {
        My_Log{My_Log::Level::kInfo} << "    - Tool 消息:      " << stats.tool_count << " 条, "
                                      << stats.tool_tokens << " tokens" << std::endl;
    }
    My_Log{My_Log::Level::kInfo} << "    ────────────────────────────────────────" << std::endl;

    size_t grand_total;
    if (is_before_optimization) {

        grand_total = stats.system_tokens + stats.tools_definition_tokens + stats.total_tokens;
    } else {

        grand_total = stats.system_tokens + stats.total_tokens;
    }
    My_Log{My_Log::Level::kInfo} << "    总计:               " << grand_total << " tokens" << std::endl;
}

inline void PrintCompressionStats(const MessageCategoryStats& stats) {
    if (stats.truncated_count > 0 || stats.full_kept_count > 0) {
        My_Log{My_Log::Level::kInfo} << "\n  压缩统计:" << std::endl;
        My_Log{My_Log::Level::kInfo} << "    - 完整保留:       " << stats.full_kept_count << " 条" << std::endl;
        My_Log{My_Log::Level::kInfo} << "    - 已截断:         " << stats.truncated_count << " 条" << std::endl;
        My_Log{My_Log::Level::kInfo} << "    - 总计保留:       " << stats.total_kept_count << " 条" << std::endl;
    }
}

inline void PrintDroppedStats(const MessageCategoryStats& stats) {
    if (stats.total_dropped > 0) {
        My_Log{My_Log::Level::kInfo} << "\n  删除统计:" << std::endl;
        if (stats.dropped_by_smart_select > 0) {
            My_Log{My_Log::Level::kInfo} << "    - SmartSelect 删除: " << stats.dropped_by_smart_select << " 条" << std::endl;
        }
        if (stats.dropped_by_token_limit > 0) {
            My_Log{My_Log::Level::kInfo} << "    - Token 限制删除:   " << stats.dropped_by_token_limit << " 条" << std::endl;
        }
        My_Log{My_Log::Level::kInfo} << "    - 总计删除:         " << stats.total_dropped << " 条" << std::endl;
    }
}

inline void PrintContextUsage(size_t message_tokens, size_t context_limit, size_t system_and_tools_tokens = 0, size_t full_context_size = 0) {
    My_Log{My_Log::Level::kInfo} << "\n    ────────────────────────────────────────" << std::endl;
    size_t total_used = system_and_tools_tokens + message_tokens;
    My_Log{My_Log::Level::kInfo} << "    总计使用:         " << total_used << " tokens" << std::endl;
    My_Log{My_Log::Level::kInfo} << "    Context 限制:     " << context_limit << " tokens" << std::endl;

    if (total_used <= context_limit) {

        size_t effective_context = (full_context_size > 0) ? full_context_size : context_limit;
        My_Log{My_Log::Level::kInfo} << "    剩余可用:         " << (effective_context - total_used) << " tokens" << std::endl;
    } else {
        My_Log{My_Log::Level::kWarning} << "    超出:             " << (total_used - context_limit) << " tokens" << std::endl;
    }
}

inline void PrintFinalPromptStats(size_t tokens, size_t length, size_t context_limit, bool has_tools, size_t full_context_size = 0) {
    My_Log{My_Log::Level::kInfo} << "    最终提示词:       " << tokens << " tokens, " << length << " chars" << std::endl;
    My_Log{My_Log::Level::kInfo} << "\n  Context 使用情况:" << std::endl;
    My_Log{My_Log::Level::kInfo} << "    - Context 限制:   " << context_limit << " tokens" << std::endl;
    My_Log{My_Log::Level::kInfo} << "    - 已使用:         " << tokens << " tokens" << std::endl;

    size_t effective_context = (full_context_size > 0) ? full_context_size : context_limit;
    if (tokens <= effective_context) {
        My_Log{My_Log::Level::kInfo} << "    - 剩余可用:       " << (effective_context - tokens) << " tokens" << std::endl;
    } else {
        My_Log{My_Log::Level::kWarning} << "    - 超出:           " << (tokens - effective_context) << " tokens" << std::endl;
    }
    My_Log{My_Log::Level::kInfo} << "    - 工具调用:       " << (has_tools ? "是" : "否") << "\n" << std::endl;
}

template<typename HandleType>
inline void PrintBeforeOptimizationStats(
    const nlohmann::ordered_json& messages,
    const std::string& system_prompt,
    HandleType handle,
    int context_size,
    const nlohmann::ordered_json* tools = nullptr,
    size_t full_context_size = 0)
{
    MessageCategoryStats before_stats;
    before_stats.system_tokens = handle->TokenLength(system_prompt);

    if (tools && tools->is_array() && !tools->empty()) {
        std::string tools_str = tools->dump();
        before_stats.tools_definition_tokens = handle->TokenLength(tools_str);
    }

    for (const auto &element: messages) {
        std::string role = element.value("role", "");
        if (role == "system") continue;

        std::string content = get_json_value(element, "content", BLANK_STRING);
        size_t msg_tokens = handle->TokenLength(content);

        if (role == "assistant" && element.contains("tool_calls") &&
            element["tool_calls"].is_array() && !element["tool_calls"].empty()) {
            std::string tool_calls_str = element["tool_calls"].dump();
            msg_tokens += handle->TokenLength(tool_calls_str);
        }

        before_stats.total_tokens += msg_tokens;

        if (role == "user") {
            before_stats.user_count++;
            before_stats.user_tokens += msg_tokens;
        } else if (role == "assistant") {
            before_stats.assistant_count++;
            before_stats.assistant_tokens += msg_tokens;
        } else if (role == "tool") {
            before_stats.tool_count++;
            before_stats.tool_tokens += msg_tokens;
        }
    }

    PrintStatsHeader("优化前原始数据统计信息（OptimizeSystemPrompt）还没调用");
    PrintMessageCategoryStats(before_stats, true);
    PrintContextUsage(before_stats.total_tokens, context_size,
                     before_stats.system_tokens + before_stats.tools_definition_tokens, full_context_size);
}

template<typename HandleType>
inline void PrintAfterPreFilterStats(
    const nlohmann::ordered_json& messages,
    HandleType handle,
    size_t system_tokens,
    int context_size,
    size_t tools_tokens = 0,
    const MessageCategoryStats* prefilter_stats = nullptr,
    size_t full_context_size = 0,
    bool is_harmony = false)
{
    MessageCategoryStats after_stats;
    after_stats.system_tokens = system_tokens;
    after_stats.tools_definition_tokens = tools_tokens;

    if (prefilter_stats) {
        after_stats.dropped_by_smart_select = prefilter_stats->dropped_by_smart_select;
        after_stats.dropped_by_token_limit = prefilter_stats->dropped_by_token_limit;
        after_stats.total_dropped = prefilter_stats->total_dropped;
    }

    size_t full_kept = 0;
    size_t truncated = 0;

    for (const auto &element: messages) {
        std::string role = element.value("role", "");
        if (role == "system") continue;

        std::string content = get_json_value(element, "content", BLANK_STRING);

        std::string content_for_count = content;
        if (!is_harmony && role == "tool") {
            content_for_count = "<tool_response>\n" + content + "\n</tool_response>\n";
        }
        size_t msg_tokens = handle->TokenLength(content_for_count);

        if (role == "assistant" && element.contains("tool_calls") &&
            element["tool_calls"].is_array() && !element["tool_calls"].empty()) {
            std::string tool_calls_str = element["tool_calls"].dump();
            msg_tokens += handle->TokenLength(tool_calls_str);
        }

        after_stats.total_tokens += msg_tokens;

        if (role == "user") {
            after_stats.user_count++;
            after_stats.user_tokens += msg_tokens;
        } else if (role == "assistant") {
            after_stats.assistant_count++;
            after_stats.assistant_tokens += msg_tokens;
        } else if (role == "tool") {
            after_stats.tool_count++;
            after_stats.tool_tokens += msg_tokens;
        }

        if (content.length() >= 3 && content.substr(content.length() - 3) == "...") {
            truncated++;
        } else {
            full_kept++;
        }
    }

    after_stats.full_kept_count = full_kept;
    after_stats.truncated_count = truncated;
    after_stats.total_kept_count = messages.size();

    PrintStatsHeader("PreFilter 优化后统计信息");
    PrintMessageCategoryStats(after_stats, false);
    PrintCompressionStats(after_stats);
    PrintDroppedStats(after_stats);
    PrintContextUsage(after_stats.total_tokens, context_size, system_tokens, full_context_size);
}

inline void PrintAfterFitStats(
    const MessageCategoryStats& stats,
    size_t context_size,
    size_t full_context_size = 0)
{
    PrintStatsHeader("FitMessagesToContext 优化后统计信息");
    PrintMessageCategoryStats(stats, false);
    PrintContextUsage(stats.total_tokens, context_size, stats.system_tokens, full_context_size);
}

inline void PrintFinalStats(
    size_t tokens,
    size_t length,
    size_t context_limit,
    bool has_tools,
    size_t full_context_size = 0)
{
    PrintStatsHeader("最终提示词统计信息");
    PrintFinalPromptStats(tokens, length, context_limit, has_tools, full_context_size);
}

template<typename HandleType, typename OptimizerType>
inline void PrintBeforeOptimizationStatsForHarmony(
    const nlohmann::ordered_json& messages,
    OptimizerType& optimizer,
    HandleType handle,
    int context_size,
    const std::string& knowledge_cutoff,
    const std::string& current_date,
    const std::string& reasoning_level,
    bool has_tools,
    const std::string& instructions,
    const nlohmann::ordered_json& tools,
    size_t full_context_size = 0)
{

    std::string temp_system = optimizer.OptimizeHarmonySystemMessage(
        knowledge_cutoff, current_date, reasoning_level, has_tools);
    std::string temp_developer;
    if (has_tools || !instructions.empty()) {
        temp_developer = optimizer.OptimizeHarmonyDeveloperMessage(instructions, tools);
    }

    MessageCategoryStats before_stats;
    before_stats.system_tokens = handle->TokenLength(temp_system + temp_developer);

    if (tools.is_array() && !tools.empty()) {
        std::string tools_str = tools.dump();
        before_stats.tools_definition_tokens = handle->TokenLength(tools_str);
    }

    for (const auto &element: messages) {
        std::string role = element.value("role", "");
        if (role == "system") continue;

        std::string content = get_json_value(element, "content", BLANK_STRING);
        size_t msg_tokens = handle->TokenLength(content);

        if (role == "assistant" && element.contains("tool_calls") &&
            element["tool_calls"].is_array() && !element["tool_calls"].empty()) {
            std::string tool_calls_str = element["tool_calls"].dump();
            msg_tokens += handle->TokenLength(tool_calls_str);
        }

        before_stats.total_tokens += msg_tokens;

        if (role == "user") {
            before_stats.user_count++;
            before_stats.user_tokens += msg_tokens;
        } else if (role == "assistant") {
            before_stats.assistant_count++;
            before_stats.assistant_tokens += msg_tokens;
        } else if (role == "tool") {
            before_stats.tool_count++;
            before_stats.tool_tokens += msg_tokens;
        }
    }

    PrintStatsHeader("优化前原始数据统计信息（OptimizeSystemPrompt）还没调用");
    PrintMessageCategoryStats(before_stats, true);

    PrintContextUsage(before_stats.total_tokens, context_size,
                     before_stats.system_tokens + before_stats.tools_definition_tokens, full_context_size);
}

template<typename HandleType>
inline void PrintAfterFitStatsFromOptimized(
    const std::vector<GenieChatMessage>& messages,
    HandleType model_handle,
    size_t system_tokens,
    size_t tools_tokens,
    int contextSize,
    size_t full_context_size = 0)
{
    MessageCategoryStats fit_stats;
    fit_stats.system_tokens = system_tokens;
    fit_stats.tools_definition_tokens = tools_tokens;
    for (const auto& opt_msg : messages) {
        if (opt_msg.role == "system") continue;
        size_t msg_tokens = model_handle->TokenLength(opt_msg.content);
        fit_stats.total_tokens += msg_tokens;
        if (opt_msg.role == "user") {
            fit_stats.user_count++;
            fit_stats.user_tokens += msg_tokens;
        } else if (opt_msg.role == "assistant") {
            fit_stats.assistant_count++;
            fit_stats.assistant_tokens += msg_tokens;
        } else if (opt_msg.role == "tool") {
            fit_stats.tool_count++;
            fit_stats.tool_tokens += msg_tokens;
        }
    }
    PrintAfterFitStats(fit_stats, contextSize, full_context_size);
}

}

#endif
