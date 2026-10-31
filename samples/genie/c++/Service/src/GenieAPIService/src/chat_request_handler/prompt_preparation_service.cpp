//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "prompt_preparation_service.h"
#include "log.h"
#include "utils.h"
#include <sstream>
#include <algorithm>

PromptPreparationService::PromptPreparationService(IModelConfig &model_config)
    : model_config_(model_config)
{
}

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

json PromptPreparationService::ExtractNonSystemMessages(const json &messages)
{
    json result = json::array();
    for (const auto &msg : messages)
    {
        if (!msg.is_object()) continue;
        std::string role = msg.value("role", "");
        if (role == "system" || role == "developer") continue;
        result.push_back(msg);
    }
    return result;
}

json PromptPreparationService::RebuildMessagesForCloud(
    const std::string &optimized_system_prompt,
    const json &non_system_messages)
{
    json result = json::array();

    if (!optimized_system_prompt.empty())
    {
        json sys_msg;
        sys_msg["role"] = "system";
        sys_msg["content"] = optimized_system_prompt;
        result.push_back(sys_msg);
    }

    for (const auto &msg : non_system_messages)
    {
        result.push_back(msg);
    }

    return result;
}

std::string PromptPreparationService::AdaptSystemPromptForCloud(const std::string &system_prompt)
{
    if (system_prompt.empty()) return system_prompt;

    std::string result = system_prompt;

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

    {

        const std::string tool_call_open = "<tool_call>";
        const std::string tool_call_close = "</tool_call>";
        size_t pos = 0;
        while ((pos = result.find(tool_call_open, pos)) != std::string::npos)
        {
            size_t end_pos = result.find(tool_call_close, pos);
            if (end_pos == std::string::npos) break;

            size_t content_start = pos + tool_call_open.size();
            std::string tool_call_content = result.substr(content_start, end_pos - content_start);

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

static size_t EstimateTokenCount(const json &messages, const std::string &system_prompt)
{
    size_t total = system_prompt.size() / 4;
    for (const auto &msg : messages)
    {
        if (!msg.is_object()) continue;

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

        if (msg.contains("tool_calls"))
            total += msg["tool_calls"].dump().size() / 4;

        total += 10;
    }
    return total;
}

static json TrimMessagesForCloud(const json &messages,
                                  const std::string &system_prompt,
                                  int available_context,
                                  bool &trimmed)
{
    trimmed = false;
    if (messages.empty()) return messages;

    size_t estimated = EstimateTokenCount(messages, system_prompt);
    if (static_cast<int>(estimated) <= available_context)
        return messages;

    json result = messages;
    while (static_cast<int>(EstimateTokenCount(result, system_prompt)) > available_context)
    {
        if (result.empty() || (int)result.size() <= 1) break;

        int current_last_user = -1;
        for (int i = (int)result.size() - 1; i >= 0; --i)
        {
            if (result[i].is_object() && result[i].value("role", "") == "user")
            {
                current_last_user = i;
                break;
            }
        }

        if (current_last_user <= 0) break;

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

PreparedPromptResult PromptPreparationService::PrepareForCloud(const json &request,
                                                                int context_size)
{
    PreparedPromptResult result;
    PromptOptimizer optimizer{model_config_, nullptr, context_size};

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

        std::string raw_system_prompt = ExtractSystemPrompt(messages);

        bool is_main_agent = (raw_system_prompt.find("agent=main") != std::string::npos);
        std::string agent_type = is_main_agent ? "main" : "sub";
        My_Log{My_Log::Level::kInfo}
            << "[PromptPrep] agent_type=" << agent_type
            << ", context_size=" << context_size << std::endl;

        std::string optimized_system;
        if (!raw_system_prompt.empty())
        {
            if (is_main_agent)
                optimized_system = optimizer.OptimizeSystemPrompt(raw_system_prompt, request);
            else
                optimized_system = optimizer.OptimizeSubagentSystemPrompt(raw_system_prompt, request);

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

        if (!optimized_system.empty())
        {
            optimized_system = AdaptSystemPromptForCloud(optimized_system);
            My_Log{My_Log::Level::kInfo}
                << "[PromptPrep] System prompt adapted for cloud API: "
                << optimized_system.size() << " chars" << std::endl;
        }

        json non_system_msgs = ExtractNonSystemMessages(messages);

        const auto &po_cfg = model_config_.GetPromptOptimizationConfig();
        int reserved_output = static_cast<int>(context_size * po_cfg.output_reserve_ratio);
        int available_context = context_size - reserved_output;

        My_Log{My_Log::Level::kInfo}
            << "[PromptPrep] Context budget: total=" << context_size
            << ", reserved=" << reserved_output
            << ", available=" << available_context << std::endl;

        bool messages_trimmed = false;
        json trimmed_msgs = non_system_msgs;
        if (available_context > 0)
        {
            trimmed_msgs = TrimMessagesForCloud(
                non_system_msgs, optimized_system, available_context, messages_trimmed);
        }
        result.messages_trimmed = messages_trimmed;

        json rebuilt_messages = RebuildMessagesForCloud(optimized_system, trimmed_msgs);

        json prepared = request;
        prepared["messages"] = rebuilt_messages;

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
