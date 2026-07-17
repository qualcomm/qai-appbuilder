//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// prompt_preparation_service.cpp
//
// 设计原则（修订版）：
//
//   云端路由（企业云 / 公网云）使用 OpenAI Chat Completions API，
//   消息格式必须严格遵循 OpenAI 标准：
//     - assistant 消息可含 tool_calls 字段（不能序列化为字符串）
//     - tool 消息必须保留 tool_call_id 字段
//     - content 为 null 的消息必须保留（OpenAI 标准允许）
//
//   因此本模块的核心策略是：
//     1. 只优化 system 消息内容（重建 system prompt）
//     2. 其余所有消息（user/assistant/tool）保持原始 OpenAI JSON 格式不变
//     3. 消息压缩（PreFilterMessages）在原始 JSON 格式上操作
//     4. 不使用 GenieChatMessage 向量（该结构仅适用于本地模型文本 prompt）
//     5. FitMessagesToContext 仅用于 token 预算估算，不做格式转换
//
//==============================================================================

#include "prompt_preparation_service.h"
#include "log.h"
#include "utils.h"
#include <sstream>
#include <algorithm>

// ============================================================
// 构造函数
// ============================================================
PromptPreparationService::PromptPreparationService(IModelConfig &model_config)
    : model_config_(model_config),
      optimizer_(model_config, nullptr)   // context=nullptr：云端路径不需要本地 tokenizer
{
}

// ============================================================
// ExtractSystemPrompt：从 messages 中提取 system/developer 消息内容
// 多条 system/developer 消息合并为一个字符串（换行分隔）
// ============================================================
std::string PromptPreparationService::ExtractSystemPrompt(const json &messages)
{
    std::string result;
    for (const auto &msg : messages)
    {
        if (!msg.is_object()) continue;
        std::string role = msg.value("role", "");
        if (role != "system" && role != "developer") continue;

        std::string content;
        if (msg.contains("content") && msg["content"].is_string())
            content = msg["content"].get<std::string>();
        else if (msg.contains("content") && msg["content"].is_array())
        {
            for (const auto &part : msg["content"])
            {
                if (part.contains("type") && part["type"] == "text" && part.contains("text"))
                    content += part["text"].get<std::string>();
            }
        }

        if (!content.empty())
        {
            if (!result.empty()) result += "\n";
            result += content;
        }
    }
    return result;
}

// ============================================================
// ExtractNonSystemMessages：从 messages 中提取非 system/developer 消息
// 保留原始 OpenAI JSON 格式（含 tool_calls、tool_call_id 等字段）
// ============================================================
json PromptPreparationService::ExtractNonSystemMessages(const json &messages)
{
    json result = json::array();
    for (const auto &msg : messages)
    {
        if (!msg.is_object()) continue;
        std::string role = msg.value("role", "");
        if (role == "system" || role == "developer") continue;
        result.push_back(msg);  // 完整保留原始消息，不做任何格式转换
    }
    return result;
}

// ============================================================
// RebuildMessagesForCloud：将优化后的 system prompt 与原始非 system 消息重组
//
// 关键设计：
//   - system 消息使用优化后的内容
//   - 其余消息（user/assistant/tool）完整保留原始 OpenAI JSON 格式
//   - assistant 的 tool_calls 字段、tool 的 tool_call_id 字段均不丢失
// ============================================================
json PromptPreparationService::RebuildMessagesForCloud(
    const std::string &optimized_system_prompt,
    const json &non_system_messages)
{
    json result = json::array();

    // 第一条：优化后的 system 消息
    if (!optimized_system_prompt.empty())
    {
        json sys_msg;
        sys_msg["role"] = "system";
        sys_msg["content"] = optimized_system_prompt;
        result.push_back(sys_msg);
    }

    // 后续：直接追加原始 OpenAI 格式的非 system 消息
    // 不做任何格式转换，保留 tool_calls、tool_call_id、content=null 等字段
    for (const auto &msg : non_system_messages)
    {
        result.push_back(msg);
    }

    return result;
}

// ============================================================
// AdaptSystemPromptForCloud：将本地模型专用格式转换为 OpenAI API 标准格式
//
// BuildSystemContext() 生成的 system prompt 包含本地模型（Qwen3）专用的
// <tool_call> 格式示例，这对云端模型（OpenAI API function calling）是错误的。
//
// 转换规则：
//   1. 将 CRITICAL RULE 中的 <tool_call> 格式示例替换为 OpenAI 标准说明
//   2. 将 "Skills are NOT tools. If a user request matches a skill, you must first call:
//      <tool_call>..." 替换为 OpenAI 标准的工具调用说明
//   3. 将 ## Examples 中的 <tool_call> 格式示例替换为 OpenAI 标准格式
//   4. 保留所有自然语言描述和 Skill Catalog 内容
// ============================================================
std::string PromptPreparationService::AdaptSystemPromptForCloud(const std::string &system_prompt)
{
    if (system_prompt.empty()) return system_prompt;

    std::string result = system_prompt;

    // ── 替换1：CRITICAL RULE 中的 <tool_call> 格式说明 ──────────────────────
    // 原文：
    //   Skills are NOT tools.
    //   If a user request matches a skill, you must first call:
    //   <tool_call>{"name":"read","arguments":{"path":"<SKILL.md path>"}}</tool_call>
    //   Never call a skill name directly.
    //
    // 替换为 OpenAI function calling 风格：
    //   Skills are NOT tools.
    //   If a user request matches a skill, you must first call the read function
    //   with the path argument set to the SKILL.md path.
    //   Never call a skill name directly.
    {
        const std::string old_str =
            "Skills are NOT tools.\n"
            "If a user request matches a skill, you must first call:\n"
            "<tool_call>{\"name\":\"read\",\"arguments\":{\"path\":\"<SKILL.md path>\"}}</tool_call>\n"
            "Never call a skill name directly.";
        const std::string new_str =
            "Skills are NOT tools.\n"
            "If a user request matches a skill, you must first call the read function\n"
            "with the path argument set to the SKILL.md path.\n"
            "Never call a skill name directly.";
        size_t pos = result.find(old_str);
        if (pos != std::string::npos)
            result.replace(pos, old_str.size(), new_str);
    }

    // ── 替换2：Skill Catalog 中的 <tool_call> 格式说明 ──────────────────────
    // 原文：
    //   When a user request matches one of the following skills, do NOT call the skill name.
    //   You MUST first call:
    //   <tool_call>{"name":"read","arguments":{"path":"<SKILL.md path>"}}</tool_call>
    //
    // 替换为：
    //   When a user request matches one of the following skills, do NOT call the skill name.
    //   You MUST first call the read function with the path argument set to the SKILL.md path.
    {
        const std::string old_str =
            "When a user request matches one of the following skills, do NOT call the skill name.\n"
            "You MUST first call:\n"
            "<tool_call>{\"name\":\"read\",\"arguments\":{\"path\":\"<SKILL.md path>\"}}</tool_call>";
        const std::string new_str =
            "When a user request matches one of the following skills, do NOT call the skill name.\n"
            "You MUST first call the read function with the path argument set to the SKILL.md path.";
        size_t pos = result.find(old_str);
        if (pos != std::string::npos)
            result.replace(pos, old_str.size(), new_str);
    }

    // ── 替换3：## Examples 中的 <tool_call> 格式示例 ────────────────────────
    // 将 Example 1 中的 <tool_call> 格式替换为 OpenAI function calling 说明
    // 原文（Tool(correct skill call): <tool_call>...）
    // 替换为：Tool(correct skill call): call read("C:\\...\\SKILL.md")
    {
        // 查找并替换所有 <tool_call>...</tool_call> 块
        // 使用简单的字符串搜索，不使用正则表达式（避免依赖 <regex>）
        const std::string tool_call_open = "<tool_call>";
        const std::string tool_call_close = "</tool_call>";
        size_t pos = 0;
        while ((pos = result.find(tool_call_open, pos)) != std::string::npos)
        {
            size_t end_pos = result.find(tool_call_close, pos);
            if (end_pos == std::string::npos) break;

            // 提取 <tool_call> 内容
            size_t content_start = pos + tool_call_open.size();
            std::string tool_call_content = result.substr(content_start, end_pos - content_start);

            // 尝试从 JSON 中提取函数名和 path 参数，生成可读的替换文本
            std::string replacement;
            size_t name_pos = tool_call_content.find("\"name\":\"");
            size_t path_pos = tool_call_content.find("\"path\":\"");
            if (name_pos != std::string::npos && path_pos != std::string::npos)
            {
                size_t name_start = name_pos + 8;
                size_t name_end = tool_call_content.find("\"", name_start);
                std::string func_name = (name_end != std::string::npos)
                    ? tool_call_content.substr(name_start, name_end - name_start) : "read";

                size_t path_start = path_pos + 8;
                size_t path_end = tool_call_content.find("\"", path_start);
                std::string path_val = (path_end != std::string::npos)
                    ? tool_call_content.substr(path_start, path_end - path_start) : "<SKILL.md path>";

                replacement = "call " + func_name + "(\"" + path_val + "\")";
            }
            else
            {
                replacement = "[function call: " + tool_call_content + "]";
            }

            result.replace(pos, end_pos - pos + tool_call_close.size(), replacement);
            pos += replacement.size();
        }
    }

    // ── 替换4：tools_intro 中的强制性措辞 → 云端模型友好的可选性措辞 ──────────
    // 原文：
    //   You can only call these tools:
    //   - read(path, offset?, limit?)
    //   ...
    //   Never call any other tool name.
    //
    // 问题：
    //   "You can only call these tools" 和 "Never call any other tool name" 让云端模型
    //   认为所有回复都必须通过工具，无法直接输出文本回复简单问候。
    //
    // 替换为：
    //   You have access to the following tools (use them when needed):
    //   - read(path, offset?, limit?)
    //   ...
    //   Only use tools when the task requires it; for simple questions, reply directly.
    {
        const std::string old_prefix = "You can only call these tools:";
        const std::string new_prefix = "You have access to the following tools (use them when needed):";
        size_t pos = result.find(old_prefix);
        if (pos != std::string::npos)
            result.replace(pos, old_prefix.size(), new_prefix);
    }
    {
        const std::string old_suffix = "Never call any other tool name.";
        const std::string new_suffix = "Only use tools when the task requires it; for simple questions or greetings, reply directly without calling any tool.";
        size_t pos = result.find(old_suffix);
        if (pos != std::string::npos)
            result.replace(pos, old_suffix.size(), new_suffix);
    }

    return result;
}

// ============================================================
// EstimateTokenCount：估算消息列表的 token 数（字符数 / 4 近似）
// 云端路径无本地 tokenizer，使用字符数估算
// ============================================================
static size_t EstimateTokenCount(const json &messages, const std::string &system_prompt)
{
    size_t total = system_prompt.size() / 4;
    for (const auto &msg : messages)
    {
        if (!msg.is_object()) continue;
        // 估算 content 字段
        if (msg.contains("content") && msg["content"].is_string())
            total += msg["content"].get<std::string>().size() / 4;
        else if (msg.contains("content") && msg["content"].is_array())
        {
            for (const auto &part : msg["content"])
            {
                if (part.contains("text") && part["text"].is_string())
                    total += part["text"].get<std::string>().size() / 4;
            }
        }
        // 估算 tool_calls 字段
        if (msg.contains("tool_calls"))
            total += msg["tool_calls"].dump().size() / 4;
        // 每条消息固定开销（role + 格式标记）
        total += 10;
    }
    return total;
}

// ============================================================
// TrimMessagesForCloud：按 token 预算裁剪历史消息（保持 OpenAI 格式）
//
// 策略：
//   - 保护最后一条 user 消息及其后的所有消息（不可丢弃）
//   - 从最旧的消息开始丢弃，直到满足 token 预算
//   - 保持 tool_calls / tool 消息的配对完整性（避免孤立的 tool 消息）
// ============================================================
static json TrimMessagesForCloud(const json &messages,
                                  const std::string &system_prompt,
                                  int available_context,
                                  bool &trimmed)
{
    trimmed = false;
    if (messages.empty()) return messages;

    size_t estimated = EstimateTokenCount(messages, system_prompt);
    if (static_cast<int>(estimated) <= available_context)
        return messages;  // 无需裁剪

    // 从头部开始丢弃消息，直到满足预算
    json result = messages;
    while (static_cast<int>(EstimateTokenCount(result, system_prompt)) > available_context)
    {
        if (result.empty() || (int)result.size() <= 1) break;

        // 找到第一条可丢弃的消息（last_user_idx 之前的消息）
        // 注意：last_user_idx 随着消息被丢弃而减小
        int current_last_user = -1;
        for (int i = (int)result.size() - 1; i >= 0; --i)
        {
            if (result[i].is_object() && result[i].value("role", "") == "user")
            {
                current_last_user = i;
                break;
            }
        }

        if (current_last_user <= 0) break;  // 无法继续丢弃

        // 丢弃第一条消息
        json new_result = json::array();
        for (int i = 1; i < (int)result.size(); ++i)
            new_result.push_back(result[i]);
        result = new_result;
        trimmed = true;
    }

    if (trimmed)
    {
        My_Log{My_Log::Level::kInfo}
            << "[PromptPrep] TrimMessagesForCloud: trimmed from "
            << messages.size() << " to " << result.size() << " messages" << std::endl;
    }

    return result;
}

// ============================================================
// PrepareForCloud：对云端请求执行统一提示词优化
//
// 输出格式：标准 OpenAI Chat Completions API 格式
//   - system 消息：优化后的 system prompt（重建 Skill Catalog + 工具规则）
//   - 其余消息：完整保留原始 OpenAI JSON 格式（含 tool_calls、tool_call_id）
// ============================================================
PreparedPromptResult PromptPreparationService::PrepareForCloud(const json &request,
                                                                int context_size)
{
    PreparedPromptResult result;

    try
    {
        if (!request.contains("messages") || !request["messages"].is_array())
        {
            result.failure_reason = "messages field missing or not an array";
            My_Log{My_Log::Level::kError}
                << "[PromptPrep] PrepareForCloud failed: " << result.failure_reason << std::endl;
            return result;
        }

        const json &messages = request["messages"];

        // ── 1. 提取 system prompt ──────────────────────────────────────────────
        std::string raw_system_prompt = ExtractSystemPrompt(messages);

        // ── 2. 检测 agent 类型 ────────────────────────────────────────────────
        bool is_main_agent = (raw_system_prompt.find("agent=main") != std::string::npos);
        std::string agent_type = is_main_agent ? "main" : "sub";
        My_Log{My_Log::Level::kInfo}
            << "[PromptPrep] agent_type=" << agent_type
            << ", context_size=" << context_size << std::endl;

        // ── 3. 重建 system prompt（BuildSystemContext）────────────────────────
        // 只优化 system 消息内容，其余消息保持原始 OpenAI 格式不变
        std::string optimized_system;
        if (!raw_system_prompt.empty())
        {
            if (is_main_agent)
                optimized_system = optimizer_.OptimizeSystemPrompt(raw_system_prompt, request);
            else
                optimized_system = optimizer_.OptimizeSubagentSystemPrompt(raw_system_prompt, request);

            result.prompt_optimized = (optimized_system != raw_system_prompt);
            My_Log{My_Log::Level::kInfo}
                << "[PromptPrep] System prompt: original=" << raw_system_prompt.size()
                << " chars, optimized=" << optimized_system.size() << " chars"
                << (result.prompt_optimized ? " (changed)" : " (unchanged)") << std::endl;
        }
        else
        {
            optimized_system = "";
            My_Log{My_Log::Level::kInfo}
                << "[PromptPrep] No system prompt found, skipping optimization" << std::endl;
        }

        // ── 3.5 云端适配：将本地模型专用格式转换为 OpenAI API 标准格式 ──────────
        // BuildSystemContext() 生成的 system prompt 包含本地模型（Qwen3）专用的
        // <tool_call> 格式示例，这对云端模型（OpenAI API）是错误的。
        // 云端模型使用标准 function calling 机制（tools 字段），不需要在 system prompt
        // 中看到 <tool_call> 格式的示例。
        //
        // 转换规则：
        //   1. 移除 CRITICAL RULE 中的 <tool_call> 格式示例行
        //   2. 将工具调用说明改为 OpenAI function calling 风格
        //   3. 移除 ## Examples 中的 <tool_call> 格式示例（保留自然语言描述）
        if (!optimized_system.empty())
        {
            optimized_system = AdaptSystemPromptForCloud(optimized_system);
            My_Log{My_Log::Level::kInfo}
                << "[PromptPrep] System prompt adapted for cloud API: "
                << optimized_system.size() << " chars" << std::endl;
        }

        // ── 4. 提取非 system 消息（保留完整 OpenAI JSON 格式）────────────────
        json non_system_msgs = ExtractNonSystemMessages(messages);

        // ── 5. 计算 context_size 预算 ─────────────────────────────────────────
        const auto &po_cfg = model_config_.GetPromptOptimizationConfig();
        int reserved_output = static_cast<int>(context_size * po_cfg.output_reserve_ratio);
        int available_context = context_size - reserved_output;

        My_Log{My_Log::Level::kInfo}
            << "[PromptPrep] Context budget: total=" << context_size
            << ", reserved=" << reserved_output
            << ", available=" << available_context << std::endl;

        // ── 6. 按 token 预算裁剪历史消息（保持 OpenAI 格式）─────────────────
        // 注意：云端模型通常有很大的 context_size（如 200000），
        // 大多数情况下不需要裁剪。裁剪时保留完整的 OpenAI 消息格式。
        bool messages_trimmed = false;
        json trimmed_msgs = non_system_msgs;
        if (available_context > 0)
        {
            trimmed_msgs = TrimMessagesForCloud(
                non_system_msgs, optimized_system, available_context, messages_trimmed);
        }
        result.messages_trimmed = messages_trimmed;

        // ── 7. 重组 messages 数组（OpenAI 标准格式）──────────────────────────
        // system 消息使用优化后的内容，其余消息完整保留原始 OpenAI JSON 格式
        json rebuilt_messages = RebuildMessagesForCloud(optimized_system, trimmed_msgs);

        // ── 8. 构建最终请求 ───────────────────────────────────────────────────
        json prepared = request;
        prepared["messages"] = rebuilt_messages;

        // tools 字段保持原样（云端模型使用标准 OpenAI function calling 格式）
        // 不做工具压缩（云端模型通常有更大上下文，且工具定义格式需与 tool_calls 保持一致）

        result.prepared_request = prepared;
        result.optimized_system_prompt = optimized_system;
        result.used_context_size = context_size;
        result.agent_type = agent_type;
        result.success = true;

        My_Log{My_Log::Level::kInfo}
            << "[PromptPrep] PrepareForCloud success: "
            << "messages=" << rebuilt_messages.size()
            << ", system_optimized=" << result.prompt_optimized
            << ", messages_trimmed=" << result.messages_trimmed << std::endl;

        return result;
    }
    catch (const std::exception &e)
    {
        result.failure_reason = std::string("exception: ") + e.what();
        My_Log{My_Log::Level::kError}
            << "[PromptPrep] PrepareForCloud exception: " << e.what()
            << ". Caller should fall back to original request." << std::endl;
        return result;
    }
}
