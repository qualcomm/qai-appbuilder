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

// 定义空字符串常量
static const std::string BLANK_STRING = "";

// ========== 统计数据结构 ==========

// 消息分类统计
struct MessageCategoryStats {
    size_t system_tokens = 0;
    size_t user_count = 0;
    size_t user_tokens = 0;
    size_t assistant_count = 0;
    size_t assistant_tokens = 0;
    size_t tool_count = 0;
    size_t tool_tokens = 0;
    size_t tools_definition_tokens = 0;  // 工具定义的 tokens(tools 数组)
    size_t total_tokens = 0;
    
    // 压缩统计
    size_t full_kept_count = 0;      // 完整保留的消息数
    size_t truncated_count = 0;      // 被截断的消息数
    size_t total_kept_count = 0;     // 总保留消息数
    
    // PreFilter 删除统计
    size_t dropped_by_smart_select = 0;  // SmartSelect 阶段删除的消息数
    size_t dropped_by_token_limit = 0;   // Token 限制阶段删除的消息数
    size_t total_dropped = 0;            // 总删除消息数
    
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
    }
};

namespace PromptStatsHelper {

// ========== 打印函数 ==========

// 打印统计标题
inline void PrintStatsHeader(const std::string& title) {
    My_Log{}.original(true) << "\n";
    My_Log{My_Log::Level::kInfo} << "╔═══════════════════════════════════════════════════════════════════════════╗" << std::endl;
    
    // 计算标题居中位置
    size_t total_width = 75;
    size_t title_len = 0;
    
    // 计算显示宽度（UTF-8编码：中文字符占2个宽度，英文占1个）
    for (size_t i = 0; i < title.length(); ) {
        unsigned char c = static_cast<unsigned char>(title[i]);
        if (c < 0x80) {
            // ASCII字符（单字节）
            title_len += 1;
            i += 1;
        } else if ((c & 0xE0) == 0xC0) {
            // 2字节UTF-8字符
            title_len += 2;
            i += 2;
        } else if ((c & 0xF0) == 0xE0) {
            // 3字节UTF-8字符（中文通常是这个）
            title_len += 2;
            i += 3;
        } else if ((c & 0xF8) == 0xF0) {
            // 4字节UTF-8字符
            title_len += 2;
            i += 4;
        } else {
            // 无效字符，跳过
            i += 1;
        }
    }
    
    size_t left_padding = (total_width - title_len) / 2;
    size_t right_padding = total_width - title_len - left_padding;
    
    // 添加保护：限制 padding 大小，防止内存分配失败
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

// 打印消息分类统计
// is_before_optimization: true 表示优化前统计，false 表示优化后统计
inline void PrintMessageCategoryStats(const MessageCategoryStats& stats, bool is_before_optimization = false) {
    My_Log{My_Log::Level::kInfo} << "  消息分类统计:" << std::endl;
    if (stats.system_tokens > 0) {
        My_Log{My_Log::Level::kInfo} << "    - System Prompt:  " << stats.system_tokens << " tokens" << std::endl;
    }
    // 优化后不显示 Tools 定义（已整合到 System Prompt 中）
    if (stats.tools_definition_tokens > 0 && is_before_optimization) {
        // 优化前：Tools 是独立的，应该显示并计入总计
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
    
    // 根据是否优化前，决定是否包含 tools_definition_tokens
    size_t grand_total;
    if (is_before_optimization) {
        // 优化前：包含 Tools JSON（此时 Tools 是独立的）
        grand_total = stats.system_tokens + stats.tools_definition_tokens + stats.total_tokens;
    } else {
        // 优化后：不包含 Tools JSON（已整合到 System Prompt 的 Developer 消息中）
        grand_total = stats.system_tokens + stats.total_tokens;
    }
    My_Log{My_Log::Level::kInfo} << "    总计:               " << grand_total << " tokens" << std::endl;
}

// 打印压缩统计
inline void PrintCompressionStats(const MessageCategoryStats& stats) {
    if (stats.truncated_count > 0 || stats.full_kept_count > 0) {
        My_Log{My_Log::Level::kInfo} << "\n  压缩统计:" << std::endl;
        My_Log{My_Log::Level::kInfo} << "    - 完整保留:       " << stats.full_kept_count << " 条" << std::endl;
        My_Log{My_Log::Level::kInfo} << "    - 已截断:         " << stats.truncated_count << " 条" << std::endl;
        My_Log{My_Log::Level::kInfo} << "    - 总计保留:       " << stats.total_kept_count << " 条" << std::endl;
    }
}

// 打印删除统计
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

// 打印 Context 使用情况
// system_and_tools_tokens 参数包含 system prompt + tools 定义的总 tokens
// full_context_size: 完整的模型上下文窗口大小（用于计算真正的可用输出空间）
//   若为 0，则退化为使用 context_limit（向后兼容）
inline void PrintContextUsage(size_t message_tokens, size_t context_limit, size_t system_and_tools_tokens = 0, size_t full_context_size = 0) {
    My_Log{My_Log::Level::kInfo} << "\n    ────────────────────────────────────────" << std::endl;
    size_t total_used = system_and_tools_tokens + message_tokens;
    My_Log{My_Log::Level::kInfo} << "    总计使用:         " << total_used << " tokens" << std::endl;
    My_Log{My_Log::Level::kInfo} << "    Context 限制:     " << context_limit << " tokens" << std::endl;
    
    if (total_used <= context_limit) {
        // 使用完整的 context_size 计算真正的可用输出空间
        // full_context_size - total_used 才是模型真正可以输出的最大 tokens 数
        size_t effective_context = (full_context_size > 0) ? full_context_size : context_limit;
        My_Log{My_Log::Level::kInfo} << "    剩余可用:         " << (effective_context - total_used) << " tokens" << std::endl;
    } else {
        My_Log{My_Log::Level::kWarning} << "    超出:             " << (total_used - context_limit) << " tokens" << std::endl;
    }
}

// 打印最终提示词统计
// context_limit: 提示词构建阶段的限制（已减去 reserved_output，用于提示词压缩）
// full_context_size: 完整的模型上下文窗口大小（用于计算真正的可用输出空间）
//   若为 0，则退化为使用 context_limit（向后兼容）
inline void PrintFinalPromptStats(size_t tokens, size_t length, size_t context_limit, bool has_tools, size_t full_context_size = 0) {
    My_Log{My_Log::Level::kInfo} << "    最终提示词:       " << tokens << " tokens, " << length << " chars" << std::endl;
    My_Log{My_Log::Level::kInfo} << "\n  Context 使用情况:" << std::endl;
    My_Log{My_Log::Level::kInfo} << "    - Context 限制:   " << context_limit << " tokens" << std::endl;
    My_Log{My_Log::Level::kInfo} << "    - 已使用:         " << tokens << " tokens" << std::endl;
    // 使用完整的 context_size 计算真正的可用输出空间
    // full_context_size 是模型实际的上下文窗口大小，剩余可用 = full_context_size - prompt_tokens
    // 这才是模型真正可以输出的最大 tokens 数
    size_t effective_context = (full_context_size > 0) ? full_context_size : context_limit;
    if (tokens <= effective_context) {
        My_Log{My_Log::Level::kInfo} << "    - 剩余可用:       " << (effective_context - tokens) << " tokens" << std::endl;
    } else {
        My_Log{My_Log::Level::kWarning} << "    - 超出:           " << (tokens - effective_context) << " tokens" << std::endl;
    }
    My_Log{My_Log::Level::kInfo} << "    - 工具调用:       " << (has_tools ? "是" : "否") << "\n" << std::endl;
}

// ========== 完整统计打印函数 ==========

// 打印"优化前"统计信息（用于 BuildPrompt 和 BuildHarmonyPrompt）
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
    
    // 统计 tools 定义的 tokens
    if (tools && tools->is_array() && !tools->empty()) {
        std::string tools_str = tools->dump();
        before_stats.tools_definition_tokens = handle->TokenLength(tools_str);
    }
    
    for (const auto &element: messages) {
        std::string role = element.value("role", "");
        if (role == "system") continue;
        
        std::string content = get_json_value(element, "content", BLANK_STRING);
        size_t msg_tokens = handle->TokenLength(content);
        
        // 如果是 assistant 消息，还需要检查 tool_calls 字段
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
    PrintMessageCategoryStats(before_stats, true);  // true 表示优化前统计
    PrintContextUsage(before_stats.total_tokens, context_size,
                     before_stats.system_tokens + before_stats.tools_definition_tokens, full_context_size);
}

// 打印"PreFilter 优化后"统计信息
// [Fix 问题2] 新增 is_harmony 参数：
// BuildPrompt（普通路径）在消息收集阶段会调用 OptimizeToolResponse() 给 tool 消息包裹
// "<tool_response>\n...\n</tool_response>\n" 标签（约 30+ token 开销）。
// 为使 PrintAfterPreFilterStats 的 tool token 统计与 PrintAfterFitStats 的口径一致，
// 普通路径（is_harmony=false）对 tool 消息加入相同的包装开销。
// Harmony 路径（is_harmony=true）不加包装（tool 消息会被转换为 Harmony 格式，由 PrintAfterFitStats 统计）。
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
    
    // 如果提供了 prefilter_stats,复制删除统计信息
    if (prefilter_stats) {
        after_stats.dropped_by_smart_select = prefilter_stats->dropped_by_smart_select;
        after_stats.dropped_by_token_limit = prefilter_stats->dropped_by_token_limit;
        after_stats.total_dropped = prefilter_stats->total_dropped;
    }
    
    // 统计消息和压缩信息
    size_t full_kept = 0;
    size_t truncated = 0;
    
    for (const auto &element: messages) {
        std::string role = element.value("role", "");
        if (role == "system") continue;
        
        std::string content = get_json_value(element, "content", BLANK_STRING);
        
        // [Fix 问题2] 普通路径（!is_harmony）的 tool 消息加入 OptimizeToolResponse 包装开销，
        // 与 PrintAfterFitStats 统计的 optimized.messages 中 tool 消息的 token 数口径一致。
        // 这样"PreFilter 后"和"FitMessages 后"两个统计阶段的 tool token 数可以形成有效对比。
        std::string content_for_count = content;
        if (!is_harmony && role == "tool") {
            content_for_count = "<tool_response>\n" + content + "\n</tool_response>\n";
        }
        size_t msg_tokens = handle->TokenLength(content_for_count);
        
        // 如果是 assistant 消息,还需要检查 tool_calls 字段
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
        
        // 简单判断是否被截断(如果内容以 "..." 结尾)
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
    PrintMessageCategoryStats(after_stats, false);  // false 表示优化后统计
    PrintCompressionStats(after_stats);
    PrintDroppedStats(after_stats);
    PrintContextUsage(after_stats.total_tokens, context_size, system_tokens, full_context_size);  // 不加 tools_tokens
}

// 打印"FitMessagesToContext 优化后"统计信息
inline void PrintAfterFitStats(
    const MessageCategoryStats& stats,
    size_t context_size,
    size_t full_context_size = 0)
{
    PrintStatsHeader("FitMessagesToContext 优化后统计信息");
    PrintMessageCategoryStats(stats, false);  // false 表示优化后统计
    PrintContextUsage(stats.total_tokens, context_size, stats.system_tokens, full_context_size);  // 不加 tools_definition_tokens
}

// 打印"最终提示词"统计信息
// context_limit: 提示词构建阶段的限制（已减去 reserved_output）
// full_context_size: 完整的模型上下文窗口大小（用于计算真正的可用输出空间，0 表示使用 context_limit）
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

// 打印"优化前"统计信息（Harmony 格式专用）
// 需要先构建临时的 system 和 developer 消息来计算 tokens
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
    // 构建临时的 system 消息用于统计
    std::string temp_system = optimizer.OptimizeHarmonySystemMessage(
        knowledge_cutoff, current_date, reasoning_level, has_tools);
    std::string temp_developer;
    if (has_tools || !instructions.empty()) {
        temp_developer = optimizer.OptimizeHarmonyDeveloperMessage(instructions, tools);
    }
    
    MessageCategoryStats before_stats;
    before_stats.system_tokens = handle->TokenLength(temp_system + temp_developer);
    
    // 统计 tools 定义的 tokens（Harmony 格式中 tools 已经包含在 developer 消息中，所以这里不需要单独统计）
    // 但为了统一显示，我们可以单独计算 tools 的 tokens
    if (tools.is_array() && !tools.empty()) {
        std::string tools_str = tools.dump();
        before_stats.tools_definition_tokens = handle->TokenLength(tools_str);
    }
    
    for (const auto &element: messages) {
        std::string role = element.value("role", "");
        if (role == "system") continue;
        
        std::string content = get_json_value(element, "content", BLANK_STRING);
        size_t msg_tokens = handle->TokenLength(content);
        
        // 如果是 assistant 消息，还需要检查 tool_calls 字段
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
    PrintMessageCategoryStats(before_stats, true);  // true 表示优化前统计
    // 注意：优化前统计，tools_definition_tokens 应该计入总计
    PrintContextUsage(before_stats.total_tokens, context_size,
                     before_stats.system_tokens + before_stats.tools_definition_tokens, full_context_size);
}

// 从 GenieChatMessage 向量（FitMessagesToContext 输出）收集统计并打印"FitMessagesToContext 优化后"统计信息
// 封装 BuildPrompt 和 BuildHarmonyPrompt 中完全相同的统计信息计算逻辑，消除重复代码。
// 参数说明：
//   messages:      FitMessagesToContext 输出的消息列表（GenieChatMessage 向量）
//   model_handle:  模型句柄，用于 token 计算
//   system_tokens: 系统提示词的 token 数（已在调用方计算）
//   tools_tokens:  工具定义的 token 数（已在调用方计算，无工具时为 0）
//   contextSize:   可用 context 大小（已减去预留输出空间）
//   full_context_size: 完整的模型上下文窗口大小（用于计算真正的可用输出空间，0 表示使用 contextSize）
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

} // namespace PromptStatsHelper

#endif // PROMPT_STATS_HELPER_H
