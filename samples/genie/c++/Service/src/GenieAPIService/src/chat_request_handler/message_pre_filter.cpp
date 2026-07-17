//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "message_pre_filter.h"
#include "../processor/harmony.h"
#include "../gateway/security/security_utils.h"
#include <utils.h>
#include "log.h"
#include <map>
#include <sstream>
#include <algorithm>
#include <regex>

using json = nlohmann::ordered_json;

// ============================================================
// Constructor
// ============================================================

MessagePreFilter::MessagePreFilter(IModelConfig& model_config, ContextBase* context)
    : model_config_(model_config), context_override_(context)
{}

// ============================================================
// CountTokens
// ============================================================

size_t MessagePreFilter::CountTokens(const std::string& text)
{
    // 修复：多模型场景下优先使用 context_override_（per-model 的 ContextBase），
    // 而非 model_config_.get_genie_model_handle()（全局单模型句柄）。
    // 在多模型场景下，get_genie_model_handle() 返回的是最后加载的单模型句柄，
    // 会导致所有请求使用同一个模型进行 token 计数，破坏多模型路由的正确性。
    if (context_override_) {
        return context_override_->TokenLength(text);
    }
    auto handle = model_config_.get_genie_model_handle().lock();
    if (handle) {
        return handle->TokenLength(text);
    }
    // 如果无法获取 handle，使用粗略估算（1 token ≈ 4 字符）
    return text.length() / 4;
}

// ============================================================
// ShouldKeepFull
// ============================================================

bool MessagePreFilter::ShouldKeepFull(const GenieChatMessage& msg) const
{
    // 工具响应消息始终保持完整
    if (msg.role == "tool") {
        return true;
    }

    // 包含工具调用的 assistant 消息保持完整
    if (msg.role == "assistant") {
        // Harmony 格式：to=functions.xxx
        if (msg.content.find("to=functions.") != std::string::npos) {
            return true;
        }

        // 普通格式：<tool_call> 标记
        if (msg.content.find("<tool_call>") != std::string::npos) {
            return true;
        }
    }

    return false;
}

// ============================================================
// FitMessagesToContext
// ============================================================

OptimizedMessages MessagePreFilter::FitMessagesToContext(
    const std::vector<GenieChatMessage>& all_messages,
    const std::string& system_prompt,
    size_t context_size,
    const MessageCompressionConfig& config)
{
    OptimizedMessages result;
    result.success = false;
    result.dropped_count = 0;

    My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Input: " << all_messages.size()
                                   << " messages, Context: " << context_size << " tokens" << std::endl;
    My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Note: Message count, token pre-filtering, and content compression already done in PreFilterMessages()" << std::endl;

    // 步骤 1: 计算系统提示词的 tokens
    size_t system_tokens = CountTokens(system_prompt);

    // 检查系统提示词是否过大
    if (system_tokens > context_size) {
        result.success = false;
        result.error_message = "System prompt alone (" + std::to_string(system_tokens) +
                              " tokens) exceeds context size (" +
                              std::to_string(context_size) + " tokens). " +
                              "Please reduce system prompt or increase context size.";
        My_Log{My_Log::Level::kError} << result.error_message << std::endl;
        return result;
    }

    My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 1: System tokens: " << system_tokens << std::endl;

    size_t available_tokens = context_size - system_tokens;

    // 步骤 2: 计算总 tokens
    MessageCategoryStats stats;
    stats.system_tokens = system_tokens;
    size_t total_tokens = system_tokens;

    for (const auto& msg : all_messages) {
        size_t msg_tokens = CountTokens(msg.content);
        total_tokens += msg_tokens;
        stats.total_tokens += msg_tokens;

        if (model_config_.getenablePromptDebug()) {
            if (msg.role == "user") {
                stats.user_count++;
                stats.user_tokens += msg_tokens;
            } else if (msg.role == "assistant") {
                stats.assistant_count++;
                stats.assistant_tokens += msg_tokens;
            } else if (msg.role == "tool") {
                stats.tool_count++;
                stats.tool_tokens += msg_tokens;
            }
        }
    }

    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 2: Messages: " << stats.total_tokens
                                   << " tokens, Total: " << total_tokens
                                   << " tokens, Context: " << context_size << " tokens" << std::endl;
    }

    // 步骤 3: 如果仍然超出 contextSize，逐个丢弃旧消息
    // 保护策略：最后一条 user 消息及其之后的消息不可丢弃
    // assistant+tool 配对检测：丢弃 assistant 时，同时丢弃其后所有连续的 tool 消息
    // 两阶段丢弃策略：
    //   第一阶段：优先丢弃 recent_boundary 之前的旧消息
    //   第二阶段：若第一阶段仍不满足，再丢弃 recent_boundary 之后（但 last_user_index 之前）的消息
    std::vector<GenieChatMessage> final_messages = all_messages;

    if (total_tokens > context_size) {
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3: Dropping old messages to fit context..." << std::endl;

        // 找到最后一条 user 消息的位置
        int last_user_index = -1;
        for (int i = (int)final_messages.size() - 1; i >= 0; i--) {
            if (final_messages[i].role == "user") {
                last_user_index = i;
                break;
            }
        }

        // 计算 recent_boundary：keep_recent_full 条消息之前的边界
        auto compute_recent_boundary = [&]() -> int {
            if (last_user_index < 0) return 0;
            int boundary = last_user_index - (int)config.keep_recent_full;
            return (boundary > 0) ? boundary : 0;
        };

        // 判断索引 idx 是否受保护（不可丢弃）
        // 保护条件：idx >= last_user_index（最后一条 user 及其之后的消息）
        auto is_fit_protected = [&](int idx) -> bool {
            if (last_user_index < 0) return false;
            return idx >= last_user_index;
        };

        // 尝试从 [0, drop_before) 范围内丢弃最旧的一条（或一链）消息
        // 返回是否成功丢弃
        auto try_drop_oldest_before = [&](int drop_before) -> bool {
            if (final_messages.empty() || drop_before <= 0) return false;

            std::string role_to_drop = final_messages[0].role;

            // assistant 消息：级联丢弃其后连续的 tool 消息
            if (role_to_drop == "assistant") {
                size_t tool_count = 0;
                for (size_t k = 1; k < final_messages.size(); k++) {
                    if (final_messages[k].role == "tool") tool_count++;
                    else break;
                }
                size_t drop_count = 1 + tool_count;
                // 整条 assistant+tool 链超出 drop_before 边界时，不截断删除（避免孤立 tool 消息）
                if ((int)drop_count > drop_before) return false;
                if (drop_count == 0) return false;

                size_t dropped_tokens = 0;
                for (size_t k = 0; k < drop_count; k++)
                    dropped_tokens += CountTokens(final_messages[k].content);

                final_messages.erase(final_messages.begin(), final_messages.begin() + drop_count);
                total_tokens -= dropped_tokens;
                result.dropped_count += drop_count;

                if (last_user_index >= (int)drop_count) last_user_index -= (int)drop_count;
                else last_user_index = -1;

                My_Log{My_Log::Level::kDebug}
                    << "[FitMessagesToContext]   Dropped assistant+" << tool_count
                    << " tool(s) (saved " << dropped_tokens
                    << " tokens, remaining: " << total_tokens << ")" << std::endl;
                return true;
            }

            // 孤立 tool 消息警告
            if (role_to_drop == "tool") {
                My_Log{My_Log::Level::kWarning}
                    << "[FitMessagesToContext] Found orphaned tool message at position 0, dropping it" << std::endl;
            }

            // 其他消息：直接丢弃
            size_t dropped_tokens = CountTokens(final_messages[0].content);
            final_messages.erase(final_messages.begin());
            total_tokens -= dropped_tokens;
            result.dropped_count++;

            if (last_user_index >= 1) last_user_index -= 1;
            else last_user_index = -1;

            My_Log{My_Log::Level::kDebug}
                << "[FitMessagesToContext]   Dropped oldest message (role: " << role_to_drop
                << ", saved " << dropped_tokens
                << " tokens, remaining: " << total_tokens << ")" << std::endl;
            return true;
        };

        // 第一阶段：优先丢弃 recent_boundary 之前的旧消息
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3a: Dropping old messages (before recent_boundary)..." << std::endl;
        while (total_tokens > context_size) {
            if (final_messages.empty()) break;
            int recent_boundary = compute_recent_boundary();
            if (recent_boundary <= 0) break;  // 没有旧消息可丢弃，进入第二阶段
            if (!try_drop_oldest_before(recent_boundary)) break;
        }

        // 第二阶段：若仍超出，丢弃 recent_boundary 之后（但 last_user_index 之前）的消息
        if (total_tokens > context_size) {
            My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3b: Dropping recent messages (before last_user_index)..." << std::endl;
        }
        while (total_tokens > context_size) {
            if (final_messages.empty()) break;
            if (is_fit_protected(0)) {
                My_Log{My_Log::Level::kWarning}
                    << "[FitMessagesToContext] All remaining messages are protected (last_user_index="
                    << last_user_index << "), cannot drop further" << std::endl;
                break;
            }
            if (!try_drop_oldest_before(last_user_index >= 0 ? last_user_index : (int)final_messages.size())) break;
        }

        // 检查是否仍然超出（保护集本身超出 context_size）
        if (total_tokens > context_size) {
            // ── Phase 4: 紧急截断（最后一条 tool 消息超大时的兜底机制）──────────────
            // 触发条件：所有常规压缩/丢弃手段均已用尽，仍超出 context_size
            // 策略：对最后一条 tool 消息（SKILL.md 等超大工具返回）进行强制截断
            // 前置检查：若需截断量超过最后一条 tool 消息的 max_truncation_ratio，则放弃截断
            const auto& et_cfg = model_config_.GetPromptOptimizationConfig().emergency_truncation;
            bool phase4_applied = false;

            if (et_cfg.enabled) {
                // 找到最后一条 tool 消息
                int last_tool_idx = -1;
                for (int i = (int)final_messages.size() - 1; i >= 0; i--) {
                    if (final_messages[i].role == "tool") {
                        last_tool_idx = i;
                        break;
                    }
                }

                if (last_tool_idx >= 0) {
                    size_t last_tool_tokens = CountTokens(final_messages[last_tool_idx].content);
                    size_t overflow_tokens = total_tokens - context_size;
                    size_t tokens_to_free = overflow_tokens + static_cast<size_t>(et_cfg.safety_margin_tokens);

                    // 前置检查：截断量是否超过阈值
                    float truncation_ratio = (last_tool_tokens > 0)
                        ? static_cast<float>(tokens_to_free) / static_cast<float>(last_tool_tokens)
                        : 1.0f;

                    My_Log{My_Log::Level::kWarning}
                        << "[FitMessagesToContext] Phase 4: Emergency truncation check: "
                        << "overflow=" << overflow_tokens << " tok"
                        << ", safety_margin=" << et_cfg.safety_margin_tokens << " tok"
                        << ", tokens_to_free=" << tokens_to_free << " tok"
                        << ", last_tool_tokens=" << last_tool_tokens << " tok"
                        << ", truncation_ratio=" << truncation_ratio
                        << " (max=" << et_cfg.max_truncation_ratio << ")" << std::endl;

                    if (truncation_ratio > et_cfg.max_truncation_ratio) {
                        // 截断量过大，放弃 Phase 4，直接走 local_input_overflow 流程
                        My_Log{My_Log::Level::kWarning}
                            << "[FitMessagesToContext] Phase 4: Truncation ratio " << truncation_ratio
                            << " exceeds max_truncation_ratio " << et_cfg.max_truncation_ratio
                            << ", skipping emergency truncation (will trigger local_input_overflow)" << std::endl;
                    } else {
                        // 执行紧急截断：计算目标字符数
                        // 估算：tokens_to_free 对应的字符数（1 token ≈ 4 chars，保守估算）
                        const std::string& tool_content = final_messages[last_tool_idx].content;
                        size_t target_tokens = (last_tool_tokens > tokens_to_free)
                            ? (last_tool_tokens - tokens_to_free)
                            : 0;
                        // 目标字符数：按 token/char 比例估算，再留 10% 余量
                        size_t target_chars = (tool_content.length() > 0 && last_tool_tokens > 0)
                            ? static_cast<size_t>(
                                static_cast<float>(tool_content.length()) *
                                static_cast<float>(target_tokens) /
                                static_cast<float>(last_tool_tokens) * 0.9f)
                            : 0;

                        if (target_chars > 0 && target_chars < tool_content.length()) {
                            // UTF-8 安全截断
                            std::string truncated = safe_utf8_truncate(
                                tool_content, target_chars,
                                "\n...[Tool response truncated by emergency truncation due to context overflow]");
                            final_messages[last_tool_idx].content = truncated;

                            // 重新计算 total_tokens
                            size_t new_tool_tokens = CountTokens(truncated);
                            size_t old_total = total_tokens;
                            total_tokens = total_tokens - last_tool_tokens + new_tool_tokens;

                            My_Log{My_Log::Level::kWarning}
                                << "[FitMessagesToContext] Phase 4: Emergency truncation applied: "
                                << "tool msg[" << last_tool_idx << "] "
                                << tool_content.length() << " chars/" << last_tool_tokens << " tok"
                                << " -> " << truncated.length() << " chars/" << new_tool_tokens << " tok"
                                << ", total_tokens: " << old_total << " -> " << total_tokens
                                << " (context_size=" << context_size << ")" << std::endl;

                            if (total_tokens <= context_size) {
                                phase4_applied = true;
                                My_Log{My_Log::Level::kWarning}
                                    << "[FitMessagesToContext] Phase 4: Emergency truncation succeeded, "
                                    << "context overflow resolved" << std::endl;
                            } else {
                                // 截断后仍超出（token 估算误差），恢复原内容，走 local_input_overflow
                                final_messages[last_tool_idx].content = tool_content;
                                total_tokens = old_total;
                                My_Log{My_Log::Level::kWarning}
                                    << "[FitMessagesToContext] Phase 4: Emergency truncation insufficient "
                                    << "(still " << total_tokens << " > " << context_size
                                    << "), reverting and triggering local_input_overflow" << std::endl;
                            }
                        } else {
                            My_Log{My_Log::Level::kWarning}
                                << "[FitMessagesToContext] Phase 4: target_chars=" << target_chars
                                << " invalid (tool_content.length=" << tool_content.length()
                                << "), skipping emergency truncation" << std::endl;
                        }
                    }
                } else {
                    My_Log{My_Log::Level::kWarning}
                        << "[FitMessagesToContext] Phase 4: No tool message found in protected set, "
                        << "skipping emergency truncation" << std::endl;
                }
            }

            // Phase 4 未能解决溢出，进入错误流程
            if (!phase4_applied) {
            result.success = false;
            result.error_message = "Protected messages (last user message and all subsequent messages) "
                                  "exceed context size (" + std::to_string(context_size) +
                                  " tokens). Current total: " + std::to_string(total_tokens) +
                                  " tokens. Cannot compress further.";
            My_Log{My_Log::Level::kError} << result.error_message << std::endl;

            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Message details (oldest to newest):" << std::endl;
            size_t accumulated_tokens = 0;
            for (size_t i = 0; i < final_messages.size(); ++i) {
                const auto& msg = final_messages[i];
                size_t msg_tokens = CountTokens(msg.content);
                accumulated_tokens += msg_tokens;

                std::string content_preview = msg.content;
                if (content_preview.length() > 200) {
                    content_preview = content_preview.substr(0, 200) + "...";
                }
                std::replace(content_preview.begin(), content_preview.end(), '\n', ' ');
                std::replace(content_preview.begin(), content_preview.end(), '\r', ' ');

                My_Log{My_Log::Level::kError}
                    << "  [" << (i + 1) << "/" << final_messages.size() << "] "
                    << "Role: " << msg.role << ", "
                    << "Tokens: " << msg_tokens << ", "
                    << "Length: " << msg.content.length() << ", "
                    << "Content: \"" << content_preview << "\"" << std::endl;
            }
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Total messages tokens: " << accumulated_tokens << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] System tokens: " << system_tokens << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Total tokens (system + messages): " << (system_tokens + accumulated_tokens) << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Context size limit: " << context_size << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Exceeded by: " << (system_tokens + accumulated_tokens - context_size) << " tokens" << std::endl;

            return result;
            } // if (!phase4_applied)
        }
    } else {
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3: Total tokens within context size" << std::endl;
    }

    // 步骤 4: 最终检查
    if (total_tokens > context_size) {
        // 即使丢弃所有历史消息，仍然超出 contextSize
        result.success = false;
        result.error_message = "Context size exceeded even after dropping all history. "
                              "System prompt (" + std::to_string(system_tokens) +
                              " tokens) + remaining messages exceed context size (" +
                              std::to_string(context_size) + " tokens).";
        My_Log{My_Log::Level::kError} << result.error_message << std::endl;
        return result;
    }

    // 成功
    result.messages = final_messages;
    result.total_tokens = total_tokens;
    result.success = true;

    My_Log{My_Log::Level::kInfo} << "[FitMessagesToContext] Success - Messages: " << final_messages.size()
                                  << ", Tokens: " << total_tokens
                                  << ", Available: " << (context_size - total_tokens)
                                  << ", Dropped: " << result.dropped_count << std::endl;

    return result;
}

// ============================================================
// IsToolRelatedMessage
// ============================================================

bool MessagePreFilter::IsToolRelatedMessage(const json& element, const std::string& role)
{
    if (role == "tool") {
        return true;
    }

    if (role == "assistant") {
        // 优先检查 tool_calls 字段（OpenAI 标准格式）
        if (element.contains("tool_calls") && !element["tool_calls"].is_null() &&
            element["tool_calls"].is_array() && !element["tool_calls"].empty()) {
            return true;
        }

        // 检查 content 中的工具调用标记（仅当 content 存在且为字符串时）
        if (element.contains("content") && element["content"].is_string()) {
            std::string content = element["content"].get<std::string>();
            if (!content.empty() &&
                (content.find("to=functions.") != std::string::npos ||
                 content.find("<tool_call>") != std::string::npos)) {
                return true;
            }
        }
    }

    return false;
}

// ============================================================
// RemoveToolResponseRedundancy
// ============================================================

// 移除 Tool 返回内容中的冗余安全警告
// 单次扫描 + 输出缓冲区策略（O(n)）：
//   1. 遇到 "SECURITY NOTICE:" 时，跳过直到 "<<<EXTERNAL_UNTRUSTED_CONTENT>>>" 结束
//   2. 遇到独立的 "<<<EXTERNAL_UNTRUSTED_CONTENT>>>" 时，直接跳过
//   3. 遇到 "<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>" 时，直接跳过
//   4. 其余字符原样追加到输出缓冲区
//   5. 最后对输出做连续换行符压缩和首尾空白清理
std::string MessagePreFilter::RemoveToolResponseRedundancy(const std::string& content)
{
    static const std::string security_notice_start = "SECURITY NOTICE:";
    static const std::string start_marker          = "<<<EXTERNAL_UNTRUSTED_CONTENT>>>";
    static const std::string end_marker            = "<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>";

    // 快速路径：若内容中不含任何标记，直接返回原字符串，避免不必要的内存分配
    if (content.find(security_notice_start) == std::string::npos &&
        content.find(start_marker)          == std::string::npos &&
        content.find(end_marker)            == std::string::npos)
    {
        return content;
    }

    std::string result;
    result.reserve(content.size());

    size_t pos = 0;
    const size_t len = content.size();

    while (pos < len) {
        // ── 检查 "SECURITY NOTICE:" ──────────────────────────────────────
        if (content.compare(pos, security_notice_start.size(), security_notice_start) == 0) {
            // 向后查找对应的 start_marker
            size_t sm_pos = content.find(start_marker, pos + security_notice_start.size());
            if (sm_pos != std::string::npos) {
                // 跳过从 "SECURITY NOTICE:" 到 start_marker 末尾的整块内容
                pos = sm_pos + start_marker.size();
            } else {
                // 只有头没有尾，只跳过 "SECURITY NOTICE:" 本身
                pos += security_notice_start.size();
            }
            continue;
        }

        // ── 检查 "<<<EXTERNAL_UNTRUSTED_CONTENT>>>" ──────────────────────
        if (content.compare(pos, start_marker.size(), start_marker) == 0) {
            pos += start_marker.size();
            continue;
        }

        // ── 检查 "<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>" ──────────────────
        if (content.compare(pos, end_marker.size(), end_marker) == 0) {
            pos += end_marker.size();
            continue;
        }

        // ── 普通字符，原样追加 ───────────────────────────────────────────
        result += content[pos];
        ++pos;
    }

    // 清理多余的空白行（3个或更多连续换行符压缩为2个）
    {
        std::string cleaned;
        cleaned.reserve(result.size());
        size_t i = 0;
        while (i < result.size()) {
            if (result[i] == '\n') {
                size_t nl_count = 0;
                while (i < result.size() && result[i] == '\n') {
                    ++nl_count;
                    ++i;
                }
                cleaned += '\n';
                if (nl_count >= 2) cleaned += '\n';
            } else {
                cleaned += result[i];
                ++i;
            }
        }
        result = std::move(cleaned);
    }

    // 清理首尾空白
    return SecurityUtils::TrimWhitespace(result);
}

std::string MessagePreFilter::RemoveUntrustedMetadataBlocks(const std::string& content)
{
    if (content.find("Sender (untrusted metadata):") == std::string::npos &&
        content.find("<environment_details>") == std::string::npos) {
        return content;
    }

    std::string result = content;

    static const std::regex sender_block_pattern(
        R"((?:^|\r?\n)[ \t]*Sender \(untrusted metadata\):[ \t]*(?:\r?\n|$)(?:[ \t]*```json[ \t]*(?:\r?\n|$)[\s\S]*?[ \t]*```[ \t]*)?)",
        std::regex::ECMAScript);
    result = std::regex_replace(result, sender_block_pattern, "\n");

    static const std::regex environment_block_pattern(
        R"((?:^|\r?\n)[ \t]*<environment_details>[\s\S]*?</environment_details>[ \t]*)",
        std::regex::ECMAScript);
    result = std::regex_replace(result, environment_block_pattern, "\n");

    result = std::regex_replace(result, std::regex(R"(\n{3,})"), "\n\n");

    return SecurityUtils::TrimWhitespace(result);
}

std::string MessagePreFilter::CleanMessageContent(const std::string& content)
{
    std::string cleaned = RemoveUntrustedMetadataBlocks(content);

    static const std::regex timestamp_prefix_pattern(
        R"(^[ \t]*\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+GMT[+-]\d+\][ \t]*)",
        std::regex::ECMAScript);
    cleaned = std::regex_replace(cleaned, timestamp_prefix_pattern, "");

    return SecurityUtils::TrimWhitespace(cleaned);
}

// ============================================================
// SmartSelectMessages
// ============================================================

// 智能选择消息，确保：
// 1. 至少保留一条 user 消息
// 2. tool 消息前有对应的 assistant 消息
// 3. 保持消息的原始顺序
json MessagePreFilter::SmartSelectMessages(const json& msg, size_t max_messages)
{
    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kDebug} << "[SmartSelect] Input: " << msg.size()
                                       << " messages, max: " << max_messages << std::endl;
    }

    // 步骤 1: 简单保留最新的消息（保持原始顺序）
    json selected = json::array();
    // selected[i] 对应 msg[selected_orig_indices[i]]
    std::vector<size_t> selected_orig_indices;

    // 保留 system 消息
    bool has_system_msg = false;
    for (size_t i = 0; i < msg.size(); i++) {
        if (msg[i]["role"] == "system") {
            selected.push_back(msg[i]);
            selected_orig_indices.push_back(i);
            has_system_msg = true;
            break;
        }
    }

    // 计算起始索引（保留最新的非 system 消息）
    // 若有 system 消息，system 消息占用 1 个槽位，非 system 消息最多保留 max_messages-1 条；
    // 若无 system 消息，所有槽位都可用于非 system 消息，最多保留 max_messages 条
    size_t keep_count = has_system_msg
        ? (max_messages > 1 ? max_messages - 1 : 0)
        : max_messages;

    // 先收集非 system 消息的原始索引
    std::vector<size_t> non_system_indices;
    for (size_t i = 0; i < msg.size(); i++) {
        if (msg[i]["role"] != "system") {
            non_system_indices.push_back(i);
        }
    }

    // 基于非 system 消息数量计算跳过数量
    size_t skip_count = (non_system_indices.size() > keep_count)
                        ? (non_system_indices.size() - keep_count) : 0;

    // 保留最新的 keep_count 条非 system 消息（同时记录原始索引）
    for (size_t k = skip_count; k < non_system_indices.size(); k++) {
        selected.push_back(msg[non_system_indices[k]]);
        selected_orig_indices.push_back(non_system_indices[k]);
    }

    // 步骤 2: 检查是否有 user 消息
    bool has_user = false;

    // 在选中的消息中查找 user 消息
    for (size_t i = 0; i < selected.size(); i++) {
        if (selected[i]["role"] == "user") {
            has_user = true;
            break;
        }
    }

    // 如果没有 user 消息，往前找最新的一条
    if (!has_user) {
        int last_user_index = -1;
        for (int i = msg.size() - 1; i >= 0; i--) {
            if (msg[i]["role"] == "user") {
                last_user_index = i;
                break;
            }
        }

        if (last_user_index >= 0) {
            // 有 system 消息且 selected 非空时插入到位置 1，否则插入到位置 0（头部）
            size_t insert_pos = (has_system_msg && !selected.empty()) ? 1 : 0;
            selected.insert(selected.begin() + insert_pos, msg[last_user_index]);
            selected_orig_indices.insert(selected_orig_indices.begin() + insert_pos, (size_t)last_user_index);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kWarning} << "[SmartSelect] Force added last user message from index "
                                                 << last_user_index << std::endl;
            }
        }
    }

    // 步骤 3: 确保 tool 消息前有对应的 assistant 消息
    // 构建 tool → assistant 依赖关系
    std::map<size_t, int> tool_needs_assistant;  // selected中的tool索引 → 原始msg中的assistant索引

    for (size_t i = 0; i < selected.size(); i++) {
        if (selected[i]["role"] == "tool") {
            // 在 selected 中向前查找 assistant
            bool found_in_selected = false;
            for (int j = i - 1; j >= 0; j--) {
                auto role = get_json_value(selected[j], "role", BLANK_STRING);
                if (role == "assistant" && IsToolRelatedMessage(selected[j], role)) {
                    found_in_selected = true;
                    break;
                }
            }

            if (!found_in_selected) {
                size_t orig_i = selected_orig_indices[i];
                // 向前查找 assistant
                for (int j = (int)orig_i - 1; j >= 0; j--) {
                    auto role = get_json_value(msg[j], "role", BLANK_STRING);
                    if (role == "assistant" && IsToolRelatedMessage(msg[j], role)) {
                        tool_needs_assistant[i] = j;
                        if (model_config_.getenablePromptDebug()) {
                            My_Log{My_Log::Level::kDebug} << "[SmartSelect] Tool at selected[" << i
                                                           << "] needs assistant from msg[" << j << "]" << std::endl;
                        }
                        break;
                    }
                }
            }
        }
    }

    // 插入缺失的 assistant 消息（保持顺序）
    // std::map 按 key（tool_pos）升序迭代，保证从前往后处理，offset 单调递增
    size_t insert_offset = 0;
    for (const auto& pair : tool_needs_assistant) {
        size_t tool_pos = pair.first + insert_offset;
        int assistant_orig_index = pair.second;

        // 在 tool_pos 之前插入 assistant
        selected.insert(selected.begin() + tool_pos, msg[assistant_orig_index]);
        selected_orig_indices.insert(selected_orig_indices.begin() + tool_pos,
                                     (size_t)assistant_orig_index);
        insert_offset++;  // 每插入一条消息，后续索引 +1
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kWarning} << "[SmartSelect] Inserted missing assistant before tool at position "
                                             << tool_pos << " (offset=" << insert_offset << ")" << std::endl;
        }
    }

    // 步骤 3 补充 assistant 消息后，selected 可能超过 max_messages 约束
    // 若超出约束，从最旧的非保护消息中删除消息，直到满足约束
    // 保护规则：system、最后一条 user、最后一条 tool、最后一个 assistant tool_call、最后一条 assistant final 不可删除

    // 一次性扫描 selected，预计算各类消息的最后索引
    auto compute_last_indices = [&]() -> std::array<size_t, 4> {
        // [0]=last_user, [1]=last_tool, [2]=last_assistant_toolcall, [3]=last_assistant_final
        std::array<size_t, 4> idx = {std::string::npos, std::string::npos,
                                     std::string::npos, std::string::npos};
        for (size_t k = 0; k < selected.size(); k++) {
            auto r = get_json_value(selected[k], "role", BLANK_STRING);
            if (r == "user")  idx[0] = k;
            else if (r == "tool") idx[1] = k;
            else if (r == "assistant" && IsToolRelatedMessage(selected[k], r))  idx[2] = k;
            else if (r == "assistant" && !IsToolRelatedMessage(selected[k], r)) idx[3] = k;
        }
        return idx;
    };

    bool skip_overflow_loop = false;
    if (selected.size() > max_messages) {
        auto pre_idx = compute_last_indices();
        size_t system_count = 0;
        size_t protected_non_system = 0;
        for (size_t k = 0; k < selected.size(); k++) {
            auto r = get_json_value(selected[k], "role", BLANK_STRING);
            if (r == "system") { system_count++; continue; }
            if (r == "user"      && k == pre_idx[0]) { protected_non_system++; continue; }
            if (r == "tool"      && k == pre_idx[1]) { protected_non_system++; continue; }
            if (r == "assistant" &&  IsToolRelatedMessage(selected[k], r) && k == pre_idx[2]) { protected_non_system++; continue; }
            if (r == "assistant" && !IsToolRelatedMessage(selected[k], r) && k == pre_idx[3]) { protected_non_system++; continue; }
        }
        size_t non_system_count = selected.size() - system_count;
        if (protected_non_system >= non_system_count) {
            skip_overflow_loop = true;
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: All "
                    << non_system_count << " non-system messages are protected, "
                    << "skipping overflow loop (selected=" << selected.size()
                    << " > max_messages=" << max_messages << ")" << std::endl;
            }
        }
    }

    while (!skip_overflow_loop && selected.size() > max_messages) {
        // 每轮删除前重新计算最后索引（O(n)）
        auto last_idx = compute_last_indices();
        size_t last_user            = last_idx[0];
        size_t last_tool            = last_idx[1];
        size_t last_asst_toolcall   = last_idx[2];
        size_t last_asst_final      = last_idx[3];

        // 找到最旧的可删除消息（从前往后，跳过 system 和保护消息）
        bool removed = false;
        for (size_t i = 0; i < selected.size(); i++) {
            auto role = get_json_value(selected[i], "role", BLANK_STRING);
            if (role == "system") continue;

            if (role == "user"      && i == last_user)          continue;
            if (role == "tool"      && i == last_tool)          continue;
            if (role == "assistant" && IsToolRelatedMessage(selected[i], role)  && i == last_asst_toolcall) continue;
            if (role == "assistant" && !IsToolRelatedMessage(selected[i], role) && i == last_asst_final)    continue;

            // 找到可删除的消息
            if (role == "assistant" && IsToolRelatedMessage(selected[i], role)) {
                // 级联删除：删除 assistant tool_call 及其后续连续 tool 消息
                size_t erase_end = i + 1;
                while (erase_end < selected.size() &&
                       get_json_value(selected[erase_end], "role", BLANK_STRING) == "tool") {
                    erase_end++;
                }
                // 检查要删除的链中是否包含受保护的最后一条 tool 消息
                bool chain_has_protected_tool = (last_tool != std::string::npos &&
                                                 last_tool > i && last_tool < erase_end);
                if (chain_has_protected_tool) {
                    if (model_config_.getenablePromptDebug()) {
                        My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Skipping assistant+tool chain at ["
                                                         << i << "] because chain contains protected last tool message" << std::endl;
                    }
                    continue;
                }
                size_t drop_count = erase_end - i;
                // nlohmann::json v3.10.0 的 ordered_map 未提供 erase(iterator, iterator) 重载，
                // 因此逐个删除同一位置的元素来等效实现区间删除，而不是直接对 json 数组做范围 erase。
                for (size_t d = 0; d < drop_count; d++) {
                    selected.erase(selected.begin() + i);
                }
                selected_orig_indices.erase(selected_orig_indices.begin() + i,
                                            selected_orig_indices.begin() + erase_end);
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Removed assistant+tool chain at ["
                                                     << i << "] (" << drop_count << " msgs) to maintain max_messages="
                                                     << max_messages << std::endl;
                }
            } else {
                // 对于 tool 消息，检查其前面紧邻的 assistant tool_call 是否受保护
                // 若受保护，则不能单独删除该 tool 消息，否则 assistant 会成为孤立的工具调用请求
                if (role == "tool") {
                    bool prev_assistant_protected = false;
                    for (int j = (int)i - 1; j >= 0; j--) {
                        auto r = get_json_value(selected[j], "role", BLANK_STRING);
                        if (r == "assistant" && IsToolRelatedMessage(selected[j], r)) {
                            // 找到对应的 assistant，检查是否是受保护的最后一个 assistant tool_call
                            if (last_asst_toolcall != std::string::npos && (size_t)j == last_asst_toolcall) {
                                prev_assistant_protected = true;
                            }
                            break;
                        }
                        // 遇到非 tool/assistant 消息则停止向前查找
                        if (r != "tool") break;
                    }
                    if (prev_assistant_protected) {
                        if (model_config_.getenablePromptDebug()) {
                            My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Skipping tool msg["
                                                             << i << "] because its preceding assistant is protected"
                                                             << std::endl;
                        }
                        continue;
                    }
                }
                selected.erase(selected.begin() + i);
                selected_orig_indices.erase(selected_orig_indices.begin() + i);
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Removed msg[" << i
                                                     << "] role=" << role << " to maintain max_messages="
                                                     << max_messages << std::endl;
                }
            }
            removed = true;
            break;
        }
        // 如果没有可删除的消息（全部受保护），退出循环
        if (!removed) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: All messages protected, "
                                                 << "allowing " << selected.size() << " > max_messages="
                                                 << max_messages << std::endl;
            }
            break;
        }
    }

    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kDebug} << "[SmartSelect] Output: " << selected.size() << " messages" << std::endl;
    }
    return selected;
}

// ============================================================
// ValidateToolCallSequence
// ============================================================

// 验证工具调用序列，确保每个 tool 消息前都有对应的 assistant 消息
json MessagePreFilter::ValidateToolCallSequence(const json& msg)
{
    json validated = json::array();

    for (size_t i = 0; i < msg.size(); i++) {
        const auto& element = msg[i];
        auto role = get_json_value(element, "role", BLANK_STRING);

        if (role == "tool") {
            // 检查前面是否有工具调用类型的 assistant 消息
            bool has_assistant = false;
            for (int j = validated.size() - 1; j >= 0; j--) {
                auto r = get_json_value(validated[j], "role", BLANK_STRING);
                if (r == "assistant" && IsToolRelatedMessage(validated[j], r)) {
                    has_assistant = true;
                    break;
                }
            }

            if (!has_assistant) {
                // 孤立的 tool 消息，记录警告并跳过
                My_Log{My_Log::Level::kWarning}
                    << "[ValidateToolCall] Skipping orphaned tool message at index " << i << std::endl;
                continue;
            }
        }

        // 检查 assistant tool_call 消息后是否紧跟至少一条 tool 响应
        // 若 assistant tool_call 后面没有 tool 响应，则该 assistant 消息也应被跳过
        if (role == "assistant" && IsToolRelatedMessage(element, role)) {
            bool has_tool_response = (i + 1 < msg.size() &&
                                      get_json_value(msg[i + 1], "role", BLANK_STRING) == "tool");
            if (!has_tool_response) {
                My_Log{My_Log::Level::kWarning}
                    << "[ValidateToolCall] Skipping orphaned assistant tool_call message at index " << i
                    << " (no following tool response)" << std::endl;
                continue;
            }
        }

        validated.push_back(element);
    }

    if (validated.size() < msg.size()) {
        My_Log{My_Log::Level::kWarning}
            << "[ValidateToolCall] Removed " << (msg.size() - validated.size())
            << " orphaned messages (tool without assistant, or assistant tool_call without tool response)"
            << std::endl;
    }

    return validated;
}

// ============================================================
// SafeEraseToolChain
// ============================================================

// 级联删除工具调用链：给定一个 assistant tool_call 消息的索引，删除它及其后所有连续的 tool responses
// 返回实际删除的消息数量
size_t MessagePreFilter::SafeEraseToolChain(json& messages, size_t assistant_idx)
{
    // 统计紧跟在 assistant 后面的连续 tool 消息数量
    size_t tool_count = 0;
    for (size_t k = assistant_idx + 1; k < messages.size(); k++) {
        auto role = get_json_value(messages[k], "role", BLANK_STRING);
        if (role == "tool") {
            tool_count++;
        } else {
            break;
        }
    }
    size_t drop_count = 1 + tool_count;
    // nlohmann::json v3.10.0 的 ordered_map 未提供 erase(iterator, iterator) 重载，
    // 因此逐个删除同一位置的元素来等效实现区间删除。
    for (size_t d = 0; d < drop_count; d++) {
        messages.erase(messages.begin() + assistant_idx);
    }
    return drop_count;
}

// ============================================================
// DropOldMessagesBatch
// ============================================================

// 批量删除旧消息直到满足预算（Phase 1 专用）
// 贪心 + 批量删除策略（O(n)）
void MessagePreFilter::DropOldMessagesBatch(
    json& filtered_msg,
    const std::vector<bool>& old_flags,
    const std::function<bool(const json&, size_t)>& is_protected,
    std::shared_ptr<ContextBase> handle,
    size_t& current_tokens,
    size_t available_tokens,
    const std::function<void()>& refresh_prot_idx)
{
    if (current_tokens <= available_tokens) return;  // 已满足预算，无需删除

    // 更新保护索引缓存（批量删除前只需调用一次）
    if (refresh_prot_idx) refresh_prot_idx();

    // 步骤 1：预计算每条消息的 token 数，并确定删除集合
    size_t n = filtered_msg.size();
    std::vector<bool> to_delete(n, false);
    size_t tokens_to_free = current_tokens - available_tokens;  // 需要释放的 token 数
    size_t freed_tokens = 0;

    // 从前往后扫描，贪心选择最旧的可删除消息
    size_t i = 0;
    while (i < n && freed_tokens < tokens_to_free) {
        auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);

        // 跳过 system 消息
        if (role == "system") { i++; continue; }

        // 跳过非旧消息（old_flags[i] == false）
        if (!old_flags[i]) { i++; continue; }

        // 跳过受保护的消息
        if (is_protected(filtered_msg, i)) { i++; continue; }

        if (role == "assistant" && IsToolRelatedMessage(filtered_msg[i], role)) {
            // assistant tool_call：级联删除其后的连续 tool responses
            size_t chain_end = i + 1;
            while (chain_end < n &&
                   get_json_value(filtered_msg[chain_end], "role", BLANK_STRING) == "tool") {
                chain_end++;
            }
            // 累计整条链的 token 数
            size_t chain_tokens = 0;
            for (size_t k = i; k < chain_end; k++) {
                chain_tokens += handle->TokenLength(
                    get_json_value(filtered_msg[k], "content", BLANK_STRING));
                to_delete[k] = true;
            }
            freed_tokens += chain_tokens;
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter]   Phase 1"
                    << ": Batch-drop assistant+tool chain at ["
                    << i << "] (" << (chain_end - i) << " msgs, "
                    << chain_tokens << " tok)" << std::endl;
            }
            prefilter_stats_.dropped_by_token_limit += (chain_end - i);
            prefilter_stats_.total_dropped += (chain_end - i);
            i = chain_end;  // 跳过整条链
        } else {
            // 普通消息：直接标记删除
            size_t msg_tokens = handle->TokenLength(
                get_json_value(filtered_msg[i], "content", BLANK_STRING));
            to_delete[i] = true;
            freed_tokens += msg_tokens;
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter]   Phase 1"
                    << ": Batch-drop msg[" << i
                    << "] role=" << role
                    << " (" << msg_tokens << " tok)" << std::endl;
            }
            prefilter_stats_.dropped_by_token_limit++;
            prefilter_stats_.total_dropped++;
            i++;
        }
    }

    // 步骤 2：批量删除（从后往前，避免索引偏移）
    for (size_t k = n; k-- > 0; ) {
        if (to_delete[k]) {
            filtered_msg.erase(filtered_msg.begin() + k);
        }
    }

    // 步骤 3：更新 current_tokens（饱和减法防止下溢）
    if (current_tokens >= freed_tokens) {
        current_tokens -= freed_tokens;
    } else {
        current_tokens = 0;
    }
}

// ============================================================
// CompressMessages
// ============================================================

// 通用辅助函数：压缩指定角色的消息内容
// 统一处理 Phase 2（旧 user/assistant）、Phase 3（新 user/assistant）、Phase 4（tool）的压缩逻辑
size_t MessagePreFilter::CompressMessages(
    json& filtered_msg,
    const std::vector<bool>& old_flags,
    const std::function<bool(const json&)>& within_budget,
    const std::function<bool(const json&, size_t)>& is_truncate_protected,
    const std::function<std::string(const std::string&, size_t)>& truncate_content,
    const std::string& target_role,
    bool only_old,
    size_t max_len,
    size_t tool_min_length,
    const std::string& phase_name,
    std::shared_ptr<ContextBase> handle,
    size_t* current_tokens,
    size_t available_tokens,
    bool is_harmony)
{
    bool is_tool_phase = (target_role == "tool");
    size_t compressed_count = 0;

    // 当 available_tokens == 0 时，system prompt 已占满整个 context，
    // 消息内容压缩无法解决根本问题，由 FitMessagesToContext 作为最终兜底处理
    if (available_tokens == 0) {
        return 0;
    }

    bool use_incremental = (handle != nullptr && current_tokens != nullptr);

    for (size_t i = 0; i < filtered_msg.size(); i++) {
        auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);

        // ── 角色过滤 ──────────────────────────────────────────────────────
        if (is_tool_phase) {
            if (role != "tool") continue;
            if (only_old && !old_flags[i]) continue;
            if (!only_old && old_flags[i]) continue;
        } else {
            // user_assistant 模式：跳过 system 和 tool
            if (role == "system" || role == "tool") continue;
            if (only_old && !old_flags[i]) continue;
            if (!only_old && old_flags[i]) continue;
        }

        // ── 截断保护检查 ──────────────────────────────────────────────────
        if (is_truncate_protected(filtered_msg, i)) {
            if (is_tool_phase && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter]   " << phase_name
                    << ": tool msg[" << i << "] is truncate-protected (last tool msg), skipping" << std::endl;
            }
            continue;
        }

        // ── 执行截断 ──────────────────────────────────────────────────────
        bool is_old = old_flags[i];
        std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
        std::string truncated = truncate_content(content, max_len);
        if (truncated.length() < content.length()) {
            // tool 消息：新消息压缩后不得低于 tool_min_length
            if (is_tool_phase && !is_old && tool_min_length > 0 &&
                truncated.length() < tool_min_length) {
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kWarning} << "[PreFilter]   " << phase_name
                        << ": tool msg[" << i << "] would be " << truncated.length()
                        << " chars (< min " << tool_min_length << "), skipping" << std::endl;
                }
                continue;
            }

            filtered_msg[i]["content"] = truncated;
            compressed_count++;

            if (model_config_.getenablePromptDebug()) {
                if (is_tool_phase) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter]   " << phase_name
                        << ": tool msg[" << i << "] (" << (is_old ? "old" : "recent") << ") "
                        << content.length() << "->" << truncated.length() << " chars" << std::endl;
                } else {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter]   " << phase_name
                        << ": msg[" << i << "] role=" << role << " "
                        << content.length() << "->" << truncated.length() << " chars" << std::endl;
                }
            }

            // 满足预算后提前退出，避免不必要的压缩（有意为之的保守策略）
            if (use_incremental) {
                // 增量 token 计数时，tool 消息需要加入与 calc_msg_tokens 相同的包装开销
                std::string old_for_count = content;
                std::string new_for_count = truncated;
                if (is_tool_phase) {
                    if (!is_harmony) {
                        old_for_count = "<tool_response>\n" + content + "\n</tool_response>\n";
                        new_for_count = "<tool_response>\n" + truncated + "\n</tool_response>\n";
                    } else {
                        old_for_count = HarmonyProcessor::BuildToolMessage("unknown_tool", content);
                        new_for_count = HarmonyProcessor::BuildToolMessage("unknown_tool", truncated);
                    }
                } else if (is_harmony && role == "user") {
                    old_for_count = HarmonyProcessor::BuildUserMessage(content);
                    new_for_count = HarmonyProcessor::BuildUserMessage(truncated);
                }
                size_t old_tokens = handle->TokenLength(old_for_count);
                size_t new_tokens = handle->TokenLength(new_for_count);
                if (*current_tokens >= old_tokens) {
                    *current_tokens = *current_tokens - old_tokens + new_tokens;
                } else {
                    My_Log{My_Log::Level::kWarning}
                        << "[CompressMessages] current_tokens underflow guard triggered at msg["
                        << i << "] old=" << old_tokens << " cur=" << *current_tokens << std::endl;
                    *current_tokens = (new_tokens >= old_tokens)
                                      ? (*current_tokens + (new_tokens - old_tokens))
                                      : 0;
                }
                if (*current_tokens <= available_tokens) break;
            } else {
                // 回退：全量 token 计算（O(n) per compression，整体 O(n²)）
                if (within_budget(filtered_msg)) break;
            }
        }
    }
    return compressed_count;
}

// ============================================================
// PreFilterMessages
// ============================================================

// 消息预过滤函数（分级压缩算法）
// 所有阈值均从 service_config.json 的 prompt_optimization 节加载
nlohmann::ordered_json MessagePreFilter::PreFilterMessages(
    json& msg,
    int contextSize,
    const std::string& system_prompt_for_token_calc,
    bool is_harmony,
    const std::unordered_map<std::string, std::string>* tool_call_id_to_name)
{
    const auto& cfg = model_config_.GetPromptOptimizationConfig();

    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kInfo}.original(true) << std::endl;
        My_Log{My_Log::Level::kInfo} << "\n========== PreFilterMessages (Tiered Compression) ==========" << std::endl;
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Input: " << msg.size() << " messages" << std::endl;
    }

    // 重置统计信息
    prefilter_stats_.Reset();

    // ── Step 1: 消息数量限制（cfg.max_messages_limit）──────────────────────
    const size_t max_messages = cfg.max_messages_limit;
    json filtered_msg = json::array();

    if (msg.size() > max_messages) {
        filtered_msg = SmartSelectMessages(msg, max_messages);
        size_t dropped = msg.size() - filtered_msg.size();
        prefilter_stats_.dropped_by_smart_select = dropped;
        prefilter_stats_.total_dropped += dropped;
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 1: Smart limited to " << filtered_msg.size()
                                           << " messages (dropped " << dropped << ")" << std::endl;
        }
    } else {
        filtered_msg = msg;
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 1: All messages kept (total: " << msg.size() << ")" << std::endl;
        }
    }

    // 获取 model handle
    // 修复：多模型场景下优先使用 context_override_（per-model 的 ContextBase），
    // 而非 model_config_.get_genie_model_handle()（全局单模型句柄）
    std::shared_ptr<ContextBase> handle;
    if (context_override_) {
        // 使用非拥有 shared_ptr 包装 context_override_（生命周期由调用方保证）
        handle = std::shared_ptr<ContextBase>(context_override_, [](ContextBase*){});
    } else {
        handle = model_config_.get_genie_model_handle().lock();
    }
    if (!handle) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Skipped (no model handle)" << std::endl;
        }
        return filtered_msg;
    }

    // 计算 system tokens 和 available_tokens
    size_t system_tokens = 0;
    if (!system_prompt_for_token_calc.empty()) {
        system_tokens = handle->TokenLength(system_prompt_for_token_calc);
    }
    size_t available_tokens = (static_cast<size_t>(contextSize) > system_tokens)
                              ? (static_cast<size_t>(contextSize) - system_tokens)
                              : 0;

    // ── [DIAG] 诊断打印辅助 Lambda ────────────────────────────────────────
    // 打印最终会发送给模型的所有内容（system prompt + 压缩后的 messages）
    // 包括：user/assistant/tool 消息的完整内容、assistant 的 tool_calls、tool 的返回结果
    // 每条消息附带 token 数和字符数，便于分析哪部分占用 tokens 较多
    auto print_diag_dump = [&](const json& final_msgs) {
        // 1. 打印 system prompt（一次性完整打印）
        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] [DIAG] ========== System Prompt ("
            << system_tokens << " tok, " << system_prompt_for_token_calc.size() << " chars) ==========\n"
            << system_prompt_for_token_calc << std::endl;

        // 2. 逐条打印所有 messages（含 content + tool_calls）
        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] [DIAG] ========== Messages (" << final_msgs.size() << " total) ==========" << std::endl;

        for (size_t diag_i = 0; diag_i < final_msgs.size(); ++diag_i) {
            const auto& diag_msg = final_msgs[diag_i];
            std::string diag_role = get_json_value(diag_msg, "role", BLANK_STRING);

            // 跳过 system 消息：system prompt 已在上方单独打印（优化后版本），
            // 此处打印的是原始客户端发送的 system 消息，内容不同且冗余
            if (diag_role == "system") continue;

            // 提取 content（可能为空，如 assistant tool_call 消息）
            std::string diag_content;
            if (diag_msg.contains("content") && !diag_msg["content"].is_null()) {
                if (diag_msg["content"].is_string()) {
                    diag_content = diag_msg["content"].get<std::string>();
                } else {
                    diag_content = diag_msg["content"].dump();
                }
            }

            // 提取 tool_calls（assistant 发起工具调用时）
            std::string diag_tool_calls;
            if (diag_msg.contains("tool_calls") && !diag_msg["tool_calls"].is_null() &&
                diag_msg["tool_calls"].is_array() && !diag_msg["tool_calls"].empty()) {
                diag_tool_calls = diag_msg["tool_calls"].dump(2);
            }

            // 计算 token 数（content + tool_calls 合并计算）
            std::string diag_full = diag_content;
            if (!diag_tool_calls.empty()) {
                if (!diag_full.empty()) diag_full += "\n";
                diag_full += diag_tool_calls;
            }
            size_t diag_tok = handle->TokenLength(diag_full);

            // 打印消息头（role、token 数、字符数）
            std::string diag_header = "[PreFilter] [DIAG] --- msg[" + std::to_string(diag_i)
                + "] role=" + diag_role
                + " (" + std::to_string(diag_tok) + " tok, " + std::to_string(diag_full.size()) + " chars)";
            if (!diag_tool_calls.empty()) diag_header += " [has tool_calls]";
            diag_header += " ---";
            My_Log{My_Log::Level::kWarning} << diag_header << "\n";

            // 打印 content（非空时）
            if (!diag_content.empty()) {
                My_Log{My_Log::Level::kWarning} << diag_content << std::endl;
            }
            // 打印 tool_calls（非空时）
            if (!diag_tool_calls.empty()) {
                My_Log{My_Log::Level::kWarning} << "[tool_calls]:\n" << diag_tool_calls << std::endl;
            }
        }

        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] [DIAG] ========== End of Diagnostic Dump ==========" << std::endl;
    };

    // 当 system prompt 已占满或超出 context 时，直接跳过所有 Phase，由 FitMessagesToContext 处理
    if (available_tokens == 0) {
        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] System prompt (" << system_tokens
            << " tok) exceeds context size (" << contextSize
            << " tok), skipping all phases, FitMessagesToContext will handle" << std::endl;
        // 打印诊断信息（system prompt 超出时，messages 尚未压缩，但仍有参考价值）
        print_diag_dump(filtered_msg);
        return filtered_msg;
    }

    // ── 辅助 Lambda ──────────────────────────────────────────────────────

    // 统一 token 计算口径：只计算 content 字段的 tokens
    // tool 消息需要加入格式包装开销（普通路径：OptimizeToolResponse 包装；Harmony 路径：BuildToolMessage 包装）
    auto calc_msg_tokens = [&](const json& messages) -> size_t {
        size_t total = 0;
        for (const auto& element : messages) {
            auto role = get_json_value(element, "role", BLANK_STRING);
            if (role == "system") continue;
            std::string content = get_json_value(element, "content", BLANK_STRING);
            if (!is_harmony && role == "tool") {
                // 普通路径：加入 OptimizeToolResponse 包装开销
                content = "<tool_response>\n" + content + "\n</tool_response>\n";
            } else if (is_harmony && role == "tool") {
                // Harmony 路径：使用 HarmonyProcessor::BuildToolMessage() 生成估算内容
                std::string actual_tool_name = "unknown_tool";
                if (element.contains("name") && element["name"].is_string()) {
                    actual_tool_name = element["name"].get<std::string>();
                } else if (element.contains("tool_call_id") && element["tool_call_id"].is_string()) {
                    std::string call_id = element["tool_call_id"].get<std::string>();
                    if (tool_call_id_to_name) {
                        auto it = tool_call_id_to_name->find(call_id);
                        if (it != tool_call_id_to_name->end()) {
                            actual_tool_name = it->second;
                        }
                    }
                }
                content = HarmonyProcessor::BuildToolMessage(actual_tool_name, content);
            } else if (is_harmony && role == "user") {
                // Harmony 路径：user 消息在传入 FitMessagesToContext 之前会被包装为 Harmony 格式
                content = HarmonyProcessor::BuildUserMessage(content);
            }
            total += handle->TokenLength(content);
        }
        return total;
    };

    auto within_budget = [&](const json& messages) -> bool {
        return calc_msg_tokens(messages) <= available_tokens;
    };

    // 预计算各类消息的最后索引（O(n)），供 is_protected / is_truncate_protected 使用
    // 返回值：{last_user, last_asst_final, last_asst_toolcall, last_tool}
    auto compute_protection_indices = [&](const json& messages)
        -> std::array<size_t, 4>
    {
        // [0]=last_user, [1]=last_asst_final, [2]=last_asst_toolcall, [3]=last_tool
        std::array<size_t, 4> idx = {
            std::string::npos, std::string::npos,
            std::string::npos, std::string::npos
        };
        for (size_t k = 0; k < messages.size(); k++) {
            auto r = get_json_value(messages[k], "role", BLANK_STRING);
            if (r == "user")
                idx[0] = k;
            else if (r == "tool")
                idx[3] = k;
            else if (r == "assistant" && !IsToolRelatedMessage(messages[k], r))
                idx[1] = k;
            else if (r == "assistant" &&  IsToolRelatedMessage(messages[k], r))
                idx[2] = k;
        }
        return idx;
    };

    // 判断消息是否受"丢弃保护"（不可被 Phase 1/5 删除）
    std::array<size_t, 4> current_prot_idx = compute_protection_indices(filtered_msg);

    auto is_protected = [&](const json& messages, size_t idx) -> bool {
        const auto& element = messages[idx];
        auto role = get_json_value(element, "role", BLANK_STRING);
        if (role == "user")      return idx == current_prot_idx[0];
        if (role == "tool")      return idx == current_prot_idx[3];
        if (role == "assistant" && !IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[1];
        if (role == "assistant" &&  IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[2];
        return false;
    };

    // 判断消息是否受"截断保护"（不可被 Phase 2/3/4 截断内容）
    auto is_truncate_protected = [&](const json& messages, size_t idx) -> bool {
        const auto& element = messages[idx];
        auto role = get_json_value(element, "role", BLANK_STRING);
        if (role == "user")      return idx == current_prot_idx[0];
        if (role == "tool")      return idx == current_prot_idx[3];
        if (role == "assistant" && !IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[1];
        if (role == "assistant" &&  IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[2];
        return false;
    };

    // 预计算每条消息是否是"旧消息"的标志数组
    auto precompute_old_flags = [&](const json& messages) -> std::vector<bool> {
        size_t non_system_count = 0;
        for (const auto& e : messages) {
            if (e["role"] != "system") non_system_count++;
        }
        size_t old_cutoff = (non_system_count > cfg.recent_window)
                            ? (non_system_count - cfg.recent_window)
                            : 0;
        std::vector<bool> flags(messages.size(), false);
        size_t non_system_idx = 0;
        for (size_t i = 0; i < messages.size(); i++) {
            if (messages[i]["role"] == "system") continue;
            if (non_system_idx < old_cutoff) flags[i] = true;
            non_system_idx++;
        }
        return flags;
    };

    // 语义感知截断（只有节省足够字符才截断）
    // 优先在段落/行边界截断
    auto truncate_content = [&](const std::string& content, size_t max_len) -> std::string {
        if (content.length() <= max_len + cfg.min_compress_threshold) return content;

        // 优先在段落边界（\n\n）截断
        size_t boundary = content.rfind("\n\n", max_len);
        if (boundary != std::string::npos && boundary >= max_len / 2) {
            return content.substr(0, boundary) + "\n...[truncated]";
        }

        // 其次在行边界（\n）截断
        boundary = content.rfind('\n', max_len);
        if (boundary != std::string::npos && boundary >= max_len / 2) {
            return content.substr(0, boundary) + "\n...[truncated]";
        }

        // 兜底：UTF-8 安全字符截断
        return safe_utf8_truncate(content, max_len, "...");
    };

    // JSON 感知的 tool 消息截断函数
    // tool 消息通常是 JSON 格式的搜索结果、文件内容或 API 响应
    // 策略（按优先级）：
    //   1. JSON 数组：只保留前 N 个元素，追加 "[M more items truncated]" 说明
    //   2. JSON 对象：截断到 max_len 字符，追加 "...[JSON truncated]" 说明
    //   3. 非 JSON 内容：回退到通用语义感知截断（truncate_content）
    auto truncate_tool_content = [&](const std::string& content, size_t max_len) -> std::string {
        if (content.length() <= max_len + cfg.min_compress_threshold) return content;

        // 尝试 JSON 解析
        try {
            auto j = json::parse(content);

            if (j.is_array() && j.size() > 1) {
                // JSON 数组：用二分搜索确定最大可保留元素数
                size_t lo = 0, hi = j.size();
                while (lo + 1 < hi) {
                    size_t mid = lo + (hi - lo) / 2;
                    json tmp = json::array();
                    for (size_t k = 0; k < mid; k++) tmp.push_back(j[k]);
                    if (tmp.dump().length() <= max_len) {
                        lo = mid;
                    } else {
                        hi = mid;
                    }
                }
                if (lo >= 1) {
                    json truncated_arr = json::array();
                    for (size_t k = 0; k < lo; k++) truncated_arr.push_back(j[k]);
                    size_t dropped = j.size() - lo;
                    return truncated_arr.dump() + "\n...[" + std::to_string(dropped) + " more items truncated]";
                }
                // lo == 0：即使只保留 1 个元素仍超出，回退到字符截断
                std::string single = json::array({j[0]}).dump();
                if (single.length() <= max_len) {
                    return single + "\n...[" + std::to_string(j.size() - 1) + " more items truncated]";
                }
                return truncate_content(single, max_len);
            }

            if (j.is_object()) {
                // JSON 对象：序列化后字符截断，追加说明
                std::string serialized = j.dump(2);
                if (serialized.length() > max_len) {
                    size_t boundary = serialized.rfind('\n', max_len);
                    if (boundary != std::string::npos && boundary >= max_len / 2) {
                        return serialized.substr(0, boundary) + "\n...[JSON truncated]";
                    }
                    return safe_utf8_truncate(serialized, max_len, "...[JSON truncated]");
                }
                return serialized;
            }
        } catch (...) {
            // JSON 解析失败（非 JSON 内容），回退到通用语义感知截断
        }

        // 非 JSON 内容：使用通用语义感知截断
        return truncate_content(content, max_len);
    };

    // 初始 token 统计
    size_t initial_tokens = calc_msg_tokens(filtered_msg);
    My_Log{My_Log::Level::kInfo} << "[PreFilter] Budget: "
                                  << "sys_prompt=" << system_tokens << " tok"
                                  << ", msg_history=" << initial_tokens << " tok"
                                  << ", used=" << (system_tokens + initial_tokens) << " tok"
                                  << ", context=" << contextSize << " tok"
                                  << " (" << (contextSize > 0
                                        ? (int)(100.0 * (system_tokens + initial_tokens) / contextSize)
                                        : 0)
                                  << "% used)" << std::endl;

    bool budget_satisfied = false;

    // ── Phase 0: 无损清理（始终执行）──────────────────────────────────────────────
    // 冗余安全警告（SECURITY NOTICE: ... <<<EXTERNAL_UNTRUSTED_CONTENT>>>）是外部工具
    // 注入的格式噪声，无论预算是否充足，都应清理
    size_t after_phase0_tokens = initial_tokens;
    {
        size_t cleaned_count = 0;
        size_t metadata_filtered_count = 0;
        for (size_t i = 0; i < filtered_msg.size(); i++) {
            auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);
            if (role != "user") continue;
            std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
            std::string cleaned = CleanMessageContent(content);
            if (cleaned != content) {
                filtered_msg[i]["content"] = cleaned;
                metadata_filtered_count++;
            }
        }
        for (size_t i = 0; i < filtered_msg.size(); i++) {
            auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);
            if (role != "tool") continue;
            std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
            std::string cleaned = RemoveToolResponseRedundancy(content);
            if (cleaned.length() < content.length()) {
                filtered_msg[i]["content"] = cleaned;
                cleaned_count++;
            }
        }
        if (cleaned_count > 0) {
            after_phase0_tokens = calc_msg_tokens(filtered_msg);
            budget_satisfied = (after_phase0_tokens <= available_tokens);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Removed redundancy from "
                                               << cleaned_count << " tool messages" << std::endl;
                if (metadata_filtered_count > 0) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Filtered untrusted metadata from "
                                                   << metadata_filtered_count << " user messages" << std::endl;
                }
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Tokens after cleanup: "
                                               << after_phase0_tokens << " tok"
                                               << " (was " << initial_tokens << " tok, saved "
                                               << (initial_tokens - after_phase0_tokens) << " tok)" << std::endl;
            }
        } else {
            if (metadata_filtered_count > 0) {
                after_phase0_tokens = calc_msg_tokens(filtered_msg);
                budget_satisfied = (after_phase0_tokens <= available_tokens);
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Filtered untrusted metadata from "
                                                   << metadata_filtered_count << " user messages" << std::endl;
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Tokens after cleanup: "
                                                   << after_phase0_tokens << " tok"
                                                   << " (was " << initial_tokens << " tok, saved "
                                                   << (initial_tokens - after_phase0_tokens) << " tok)" << std::endl;
                }
            }
            if (model_config_.getenablePromptDebug()) {
                if (metadata_filtered_count == 0) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: No redundancy found in tool messages or user metadata blocks" << std::endl;
                }
            }
            if (metadata_filtered_count == 0) {
                budget_satisfied = (initial_tokens <= available_tokens);
            }
        }
        if (budget_satisfied && model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Within budget" << std::endl;
        }
    }

    // ── Phase 1: 旧消息处理（recent_window 之外）──────────────────────────
    // 按以下子步骤顺序执行，优先使用破坏性最小的手段：
    //   Step A：压缩旧 user/assistant 消息内容（old_compress_len）
    //   Step B：若仍超预算，删除旧消息（DropOldMessagesBatch）
    //   Step C：若仍超预算，压缩旧 tool 响应内容（old_compress_len）
    size_t compress_current_tokens = after_phase0_tokens;
    if (!budget_satisfied) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1: Processing old messages (recent_window="
                                           << cfg.recent_window << ")..." << std::endl;
        }

        // ── Phase 1 Step A: 压缩旧 user/assistant 消息内容 ──────────────
        {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step A: Compressing old user/assistant messages (max "
                                               << cfg.old_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_1a = precompute_old_flags(filtered_msg);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_1a, within_budget, is_truncate_protected, truncate_content,
                "user_assistant", /*only_old=*/true, cfg.old_compress_len,
                /*tool_min_length=*/0, "Phase 1A",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step A: Compressed " << compressed_count << " messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step A: Within budget" << std::endl;
            }
        }

        // ── Phase 1 Step B: 删除旧消息（仍超预算时）────────────────────
        if (!budget_satisfied) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step B: Dropping old messages (batch mode)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_1b = precompute_old_flags(filtered_msg);
            DropOldMessagesBatch(filtered_msg, old_flags_1b, is_protected,
                                 handle, compress_current_tokens, available_tokens,
                                 [&]() { current_prot_idx = compute_protection_indices(filtered_msg); });
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step B: Tokens after drop: "
                                               << compress_current_tokens << " tok" << std::endl;
                if (budget_satisfied) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step B: Within budget" << std::endl;
                }
            }
        }

        // ── Phase 1 Step C: 压缩旧 tool 响应（仍超预算时）──────────────
        if (!budget_satisfied) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step C: Compressing old tool responses (max="
                                               << cfg.old_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_1c = precompute_old_flags(filtered_msg);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_1c, within_budget, is_truncate_protected,
                truncate_tool_content,
                /*target_role=*/"tool", /*only_old=*/true,
                /*max_len=*/cfg.old_compress_len,
                /*tool_min_length=*/0, "Phase 1C",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step C: Compressed " << compressed_count << " old tool messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step C: Within budget" << std::endl;
            }
        }

        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1: Tokens after all steps: "
                                           << compress_current_tokens << " tok"
                                           << " (was " << initial_tokens << " tok, saved "
                                           << (initial_tokens > compress_current_tokens
                                               ? initial_tokens - compress_current_tokens : 0)
                                           << " tok)" << std::endl;
            if (!budget_satisfied) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1: Still over budget, proceeding to Phase 2" << std::endl;
            }
        }
    }

    // ── Phase 2: 新消息处理（recent_window 之内）──────────────────────────
    // 按以下子步骤顺序执行：
    //   Step A：压缩新 user/assistant 消息内容（recent_compress_len）
    //   Step B：压缩新 tool 响应内容（tool_compress_len）
    if (!budget_satisfied) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2: Processing recent messages..." << std::endl;
        }

        // ── Phase 2 Step A: 压缩新 user/assistant 消息内容 ──────────────
        {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step A: Compressing recent user/assistant messages (max "
                                               << cfg.recent_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_2a = precompute_old_flags(filtered_msg);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_2a, within_budget, is_truncate_protected, truncate_content,
                "user_assistant", /*only_old=*/false, cfg.recent_compress_len,
                /*tool_min_length=*/0, "Phase 2A",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step A: Compressed " << compressed_count << " messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step A: Within budget" << std::endl;
            }
        }

        // ── Phase 2 Step B: 压缩新 tool 响应（仍超预算时）──────────────
        if (!budget_satisfied) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step B: Compressing recent tool responses (max="
                                               << cfg.tool_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_2b = precompute_old_flags(filtered_msg);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_2b, within_budget, is_truncate_protected,
                truncate_tool_content,
                /*target_role=*/"tool", /*only_old=*/false,
                /*max_len=*/cfg.tool_compress_len,
                /*tool_min_length=*/cfg.tool_min_length, "Phase 2B",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step B: Compressed " << compressed_count << " recent tool messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step B: Within budget" << std::endl;
            }
        }
    }

    // ── Phase 3: 丢弃新消息（最后手段，跳过保护集）──────────────────────
    if (!budget_satisfied) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kWarning} << "[PreFilter] Phase 3: Dropping recent messages (last resort, batch mode)..." << std::endl;
        }
        current_prot_idx = compute_protection_indices(filtered_msg);
        std::vector<bool> all_flags(filtered_msg.size(), true);
        DropOldMessagesBatch(filtered_msg, all_flags, is_protected,
                             handle, compress_current_tokens, available_tokens,
                             [&]() { current_prot_idx = compute_protection_indices(filtered_msg); });
        budget_satisfied = (compress_current_tokens <= available_tokens);
        if (model_config_.getenablePromptDebug()) {
            if (budget_satisfied) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 3: Within budget" << std::endl;
            } else {
                My_Log{My_Log::Level::kWarning} << "[PreFilter] Phase 3: Still over budget, FitMessagesToContext will handle remainder" << std::endl;
            }
        }
    }

    // ── 最终统计 ──────────────────────────────────────────────────────────
    {
        size_t final_tokens = compress_current_tokens;
        size_t total_after = system_tokens + final_tokens;
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Final: "
                                      << "msg_history=" << final_tokens << " tok"
                                      << ", used=" << total_after << "/" << contextSize << " tok"
                                      << " (" << (contextSize > 0
                                            ? (int)(100.0 * total_after / contextSize)
                                            : 0)
                                      << "% used)"
                                      << ", msgs=" << filtered_msg.size() << std::endl;
    }

    // ── 步骤 4: 验证工具调用序列的完整性 ──────────────────────────────────
    json validated_msg = ValidateToolCallSequence(filtered_msg);
    if (model_config_.getenablePromptDebug()) {
        if (validated_msg.size() < filtered_msg.size()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 4: Removed "
                                           << (filtered_msg.size() - validated_msg.size())
                                           << " orphaned tool messages" << std::endl;
        } else {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 4: Tool call sequence validation passed" << std::endl;
        }
    }

    // ── 步骤 5: 最终验证 - 确保至少有一条 user 消息 ──────────────────────
    bool has_final_user = false;
    for (const auto& element : validated_msg) {
        if (element["role"] == "user") { has_final_user = true; break; }
    }

    if (!has_final_user) {
        My_Log{My_Log::Level::kError} << "[PreFilter] Step 5: CRITICAL - No user message in final result!" << std::endl;
        for (int i = (int)msg.size() - 1; i >= 0; i--) {
            if (msg[i]["role"] == "user") {
                size_t insert_pos = 0;
                if (!validated_msg.empty() && validated_msg[0]["role"] == "system") {
                    insert_pos = 1;
                }
                validated_msg.insert(validated_msg.begin() + insert_pos, msg[i]);
                My_Log{My_Log::Level::kWarning} << "[PreFilter] Step 5: Force added last user message" << std::endl;
                break;
            }
        }
    } else {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 5: Final validation passed" << std::endl;
        }
    }

    // ── [DIAG] 打印最终压缩后的完整内容（system prompt + 所有 messages）──────
    // 触发条件：仍超出预算（budget_satisfied == false），即将进入 FitMessagesToContext 处理
    // 目的：帮助分析哪部分内容占用 tokens 较多，用于决策进一步删减和压缩
    if (!budget_satisfied) {
        print_diag_dump(validated_msg);
    }

    return validated_msg;
}
