//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "repeat_gate.h"

#include <deque>
#include <unordered_map>

namespace RepeatGateLogic
{
namespace
{
    bool IsSpace(char c)
    {
        return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f';
    }

    std::string CollapseWhitespace(const std::string& text)
    {
        std::string out;
        out.reserve(text.size());
        bool pending_space = false;
        for (char c : text)
        {
            if (IsSpace(c))
            {
                pending_space = !out.empty();
                continue;
            }
            if (pending_space)
            {
                out += ' ';
                pending_space = false;
            }
            out += c;
        }
        return out;
    }

    bool MatchesIgnoreCase(const std::string& text, size_t pos, const char* needle)
    {
        size_t i = 0;
        for (; needle[i] != '\0'; ++i)
        {
            if (pos + i >= text.size())
                return false;
            char a = text[pos + i];
            if (a >= 'A' && a <= 'Z')
                a = static_cast<char>(a - 'A' + 'a');
            if (a != needle[i])
                return false;
        }
        return true;
    }

    std::string NeutralizeToolCallTags(const std::string& text)
    {
        std::string out;
        out.reserve(text.size());
        for (size_t i = 0; i < text.size(); ++i)
        {
            if (text[i] == '<')
            {
                const size_t after = (i + 1 < text.size() && text[i + 1] == '/') ? i + 2 : i + 1;
                if (MatchesIgnoreCase(text, after, "tool_call"))
                {
                    out += '(';
                    continue;
                }
            }
            out += text[i];
        }
        return out;
    }
}

std::string MakeSignature(const Json& function_obj)
{
    if (!function_obj.is_object())
        return std::string();

    std::string name;
    if (function_obj.contains("name") && function_obj["name"].is_string())
        name = function_obj["name"].get<std::string>();
    if (name.empty())
        return std::string();

    std::string args = "{}";
    if (function_obj.contains("arguments"))
    {
        const auto& raw = function_obj.at("arguments");
        if (raw.is_string())
        {
            const std::string literal = raw.get<std::string>();
            try
            {
                args = Json::parse(literal).dump();
            }
            catch (...)
            {

                args = literal;
            }
        }
        else if (raw.is_object() || raw.is_array())
        {
            args = raw.dump();
        }
    }
    return name + "|" + args;
}

std::string ExtractContentText(const Json& content)
{
    if (content.is_string())
        return content.get<std::string>();
    if (!content.is_array())
        return std::string();

    std::string joined;
    for (const auto& part : content)
    {
        if (part.is_string())
        {
            joined += part.get<std::string>();
        }
        else if (part.is_object() && part.contains("text") && part["text"].is_string())
        {
            if (!joined.empty())
                joined += "\n";
            joined += part["text"].get<std::string>();
        }
    }
    return joined;
}

std::vector<ExecutedOutcome> CollectExecutedOutcomes(const Json& messages, size_t* orphan_tool_results)
{
    std::vector<ExecutedOutcome> out;
    if (!messages.is_array())
        return out;

    std::unordered_map<std::string, std::string> id_to_signature;
    std::deque<std::string> pending_without_id;

    for (const auto& m : messages)
    {
        if (!m.is_object())
            continue;
        std::string role;
        if (m.contains("role") && m["role"].is_string())
            role = m["role"].get<std::string>();

        if (role == "assistant" && m.contains("tool_calls") && m["tool_calls"].is_array())
        {
            for (const auto& tc : m["tool_calls"])
            {
                if (!tc.is_object() || !tc.contains("function"))
                    continue;
                const std::string signature = MakeSignature(tc["function"]);
                if (signature.empty())
                    continue;
                std::string id;
                if (tc.contains("id") && tc["id"].is_string())
                    id = tc["id"].get<std::string>();
                if (id.empty())
                    pending_without_id.push_back(signature);
                else
                    id_to_signature[id] = signature;
            }
        }
        else if (role == "tool")
        {
            std::string tc_id;
            if (m.contains("tool_call_id") && m["tool_call_id"].is_string())
                tc_id = m["tool_call_id"].get<std::string>();

            std::string signature;
            if (!tc_id.empty())
            {
                auto it = id_to_signature.find(tc_id);
                if (it != id_to_signature.end())
                    signature = it->second;
            }
            if (signature.empty() && !pending_without_id.empty())
            {
                signature = pending_without_id.front();
                pending_without_id.pop_front();
            }
            if (signature.empty())
            {
                if (orphan_tool_results)
                    ++(*orphan_tool_results);
                continue;
            }

            ExecutedOutcome record;
            record.signature = signature;
            if (m.contains("content"))
                record.content = ExtractContentText(m["content"]);
            out.push_back(std::move(record));
        }
    }
    return out;
}

Outcome ClassifyOutcome(const std::string& content)
{
    for (char c : content)
    {
        if (!IsSpace(c))
            return Outcome::kOk;
    }
    return Outcome::kEmpty;
}

std::string MakeOutcomePreview(const std::string& content)
{
    return ToolCallRepetitionStore::TruncatePreviewUtf8(
        NeutralizeToolCallTags(CollapseWhitespace(content)),
        ToolCallRepetitionStore::kMaxOutcomePreviewBytes);
}

std::string BuildHardBlockBody(const std::string& tool_name, int executed_count,
                               const std::string& outcome_preview)
{

    std::string body = "Stopping before repeating an action that cannot produce new information.\n\n";
    body += "The call `";
    body += tool_name.empty() ? std::string("(unnamed tool)") : tool_name;
    body += "` with these exact arguments has already been executed ";
    body += std::to_string(executed_count < 1 ? 1 : executed_count);
    body += " time(s) in this task, and every execution returned the same result";
    if (outcome_preview.empty())
        body += ".\n\n";
    else
        body += ":\n" + outcome_preview + "\n\n";
    body += "That exact call will not be executed again. Anything still missing has to be obtained "
            "another way: a different tool, different arguments, or a different approach. "
            "If it cannot be obtained at all, state plainly what is unknown instead of retrying.";
    return body;
}

std::string BuildHardBlockFinalBody(const std::string& tool_name, int executed_count,
                                    int blocked_count, const std::string& outcome_preview)
{

    std::string body = "Stopping this task at the current point.\n\n";
    body += "The call `";
    body += tool_name.empty() ? std::string("(unnamed tool)") : tool_name;
    body += "` with these exact arguments has already been executed ";
    body += std::to_string(executed_count < 1 ? 1 : executed_count);
    body += " time(s) and was blocked ";
    body += std::to_string(blocked_count < 1 ? 1 : blocked_count);
    body += " time(s) because every execution returned the same result";
    if (outcome_preview.empty())
        body += ".\n\n";
    else
        body += ":\n" + outcome_preview + "\n\n";
    body += "This action can no longer make progress, so it has been stopped. The remaining work "
            "needs a decision from the caller: change the approach, supply the missing information, "
            "or end the task. Do not assume the task is complete.";
    return body;
}

std::string BuildSoftRetryNote(const std::string& outcome_preview, int executed_count)
{
    std::string msg = "Note: this exact tool call (same tool name and arguments) was already executed ";
    msg += std::to_string(executed_count < 1 ? 1 : executed_count);
    msg += " time(s) in this task and the result did not change. Calling it again returns nothing new.";
    if (!outcome_preview.empty())
        msg += "\nIts earlier result: " + outcome_preview;
    msg += "\nUse that result, or call a different tool/arguments to make progress.";
    return msg;
}

bool TryConsumeExtraRetry(bool& extra_retry_used)
{
    if (extra_retry_used)
        return false;
    extra_retry_used = true;
    return true;
}

}
