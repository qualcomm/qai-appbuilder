//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_tools.h"
#include "log.h"
#include "utils.h"
#include "../chat_request_handler/prompt_optimizer.h"
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <limits>
#include <regex>
#include <unordered_map>
#include <vector>

std::string ResponseTools::generate_uuid4()
{
    static std::random_device rd;
    static std::mt19937 gen(rd());
    static std::uniform_int_distribution<> dis(0, 15);
    static std::uniform_int_distribution<> dis2(8, 11);

    auto generate_hex = [&](int count)
    {
        std::stringstream ss;
        for (int i = 0; i < count; ++i)
        {
            ss << std::hex << dis(gen);
        }
        return ss.str();
    };

    std::stringstream ss;
    ss << generate_hex(8) << "-"
       << generate_hex(4) << "-"
       << "4" << generate_hex(3) << "-"
       << dis2(gen) << generate_hex(3) << "-"
       << generate_hex(12);

    return "chatcmpl-" + ss.str();
}

bool ResponseTools::post_stream_data(httplib::DataSink &sink, const char *event, const std::string &data, bool done)
{
    std::string str;
    str = std::string(event) + ": " + data + "\n\n";

    sink.write(str.c_str(), str.size());
    if (done)
    {
        sink.done();
    }
    return true;
}

bool ResponseTools::status_content_visible = true;

bool ResponseTools::log_inference_stream = false;

std::string ResponseTools::statusDataJson(const std::string &status, const std::string &message,
                                          const json &extra_payload)
{
    std::string id = generate_uuid4();
    int created = timer.GetSystemTime();

    const std::string delta_content = status_content_visible ? (message + "\n") : "";
    json choice = {
        {"index",          0},
        {"finish_reason",  nullptr},
        {"delta",          {{"content", delta_content}}},
        {"status",         status},
        {"status_message", message}
    };

    if (extra_payload.is_object()) {
        for (const auto &item : extra_payload.items()) {
            choice[item.key()] = item.value();
        }
    }
    json data = {
        {"id",      id},
        {"object",  "chat.completion.chunk"},
        {"created", created},
        {"model",   ""},
        {"choices", json::array({choice})}
    };
    return json_to_str(data);
}

std::string ResponseTools::responseDataJson(const std::string &content,
                                            const std::string &finish_reason,
                                            bool stream,
                                            const std::string &tool_calls_str,
                                            size_t prompt_tokens,
                                            size_t completion_tokens,
                                            const std::string &reasoning_content)
{
    std::string id = generate_uuid4();
    std::string object = stream ? "chat.completion.chunk" : "chat.completion";
    std::string content_name = stream ? "delta" : "message";
    int created = timer.GetSystemTime();
    json tool_calls = json(nullptr);

    if (!tool_calls_str.empty())
    {
        tool_calls = format_tool_calls(tool_calls_str);
    }

    json data = {
            {"id", id},
            {"object", object},
            {"model", ""},
            {"created", created},
            {"choices", {{
                                 {"index", 0},
                                 {"finish_reason", finish_reason},
                                 {content_name, {
                                                        {"content", content},
                                                        {"role", "assistant"},
                                                        {"tool_calls", tool_calls}
                                                }
                                 }}}},
            {"usage", {
                         {"prompt_tokens", prompt_tokens},
                         {"completion_tokens", completion_tokens},
                         {"total_tokens", prompt_tokens + completion_tokens}
            }}
    };

    if (!reasoning_content.empty())
    {
        data["choices"][0][content_name]["reasoning_content"] = reasoning_content;
    }
    return json_to_str(data);
}

std::string ResponseTools::reasoningDataJson(const std::string &reasoning)
{
    json data = {
        {"id", generate_uuid4()},
        {"object", "chat.completion.chunk"},
        {"model", ""},
        {"created", timer.GetSystemTime()},
        {"choices", json::array({{
            {"index", 0},
            {"finish_reason", nullptr},
            {"delta", {{"content", ""}, {"role", "assistant"}, {"reasoning_content", reasoning}}}
        }})}
    };
    return json_to_str(data);
}

std::string ResponseTools::ToolCallFailureReasonToString(ToolCallFailureReason reason)
{
    switch (reason)
    {
        case ToolCallFailureReason::kNone:                  return "none";
        case ToolCallFailureReason::kUnknownToolName:        return "unknown_tool_name";
        case ToolCallFailureReason::kMissingRequiredArgs:    return "missing_required_args";
        case ToolCallFailureReason::kTruncated:              return "truncated";
        case ToolCallFailureReason::kAmbiguousMultipleCalls: return "ambiguous_multiple_calls";
        case ToolCallFailureReason::kUnparseable:            return "unparseable";
    }
    return "unknown";
}

std::string ResponseTools::remove_tool_call_content(const std::string &input)
{
    static std::regex tool_call_block(R"(<tool_call>[\s\S]*?<\/tool_call>\s*)");
    static std::regex name_line(R"((\s*\{ *"name": [^\n]*\n?))");
    std::string result = std::regex_replace(input, tool_call_block, "");
    result = std::regex_replace(result, name_line, "");
    result = remove_empty_lines(result);

    std::string lower_result = result;
    std::transform(lower_result.begin(), lower_result.end(), lower_result.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (lower_result.find("<tool_call") != std::string::npos)
    {
        return "";
    }

    return result;
}

std::string ResponseTools::remove_empty_lines(const std::string &input)
{
    return std::regex_replace(input, std::regex(R"((^\s*\n)+)"), "");
}

std::string ResponseTools::json_to_str(const json &data)
{
    return data.dump(-1, ' ', false, json::error_handler_t::replace);
}

json ResponseTools::format_tool_calls(const std::string &tool_calls_str)
{
    std::istringstream iss(tool_calls_str);
    std::string line, name, arguments;

    json call;
    json tool_calls = json::array();

    while (std::getline(iss, line))
    {
        if (line.empty() || line.find("{\"name\":") != 0)
            continue;
        try
        {
            call = json::parse(line);
        }
        catch (const std::exception &e)
        {
            My_Log{} << "parse handled tool calls message failed:" << e.what()
                     << "  message: " << line << std::endl;
            continue;
        }

        std::string args_value;
        if (call.contains("arguments")) {
            if (call["arguments"].is_string()) {

                args_value = call["arguments"].get<std::string>();
            } else {

                args_value = call["arguments"].dump();
            }
        }

        json tool_call = {
                {"id",       generate_uuid4()},
                {"type",     "function"},
                {"function", {
                                     {"name", call["name"]},
                                     {"arguments", args_value}
                             }}
        };
        tool_calls.push_back(tool_call);
    }

    return tool_calls;
}

void ResponseTools::ApplyBareJsonHoldBack(BareJsonHoldState &state,
                                          const std::string &response_buffer_after_append,
                                          const std::string &message,
                                          std::string &chunk)
{
    if (state.bare_json_hold_released || chunk.empty())
    {
        return;
    }

    auto judge_post_think_candidate = [&state](const std::string &candidate) -> bool
    {
        state.post_think_judged = true;
        size_t ts = candidate.find_first_not_of(" \t\r\n");
        std::string trimmed = (ts == std::string::npos) ? std::string() : candidate.substr(ts);
        bool looks_like_bare_json_start = !trimmed.empty() &&
            (trimmed[0] == '{' ||
             trimmed.compare(0, std::min<size_t>(3, trimmed.size()), "```") == 0);
        if (looks_like_bare_json_start)
        {
            state.bare_json_hold = true;
            state.held_visible = candidate;
        }
        return looks_like_bare_json_start;
    };

    if (!state.think_closed)
    {
        size_t think_pos = response_buffer_after_append.rfind("</think>");
        if (think_pos == std::string::npos)
        {

            return;
        }

        state.think_closed = true;
        size_t after_tag = think_pos + 8;

        size_t buffer_len_before_this_message = response_buffer_after_append.length() - message.length();
        std::string post_think_part;
        if (after_tag > buffer_len_before_this_message
            && (after_tag - buffer_len_before_this_message) < chunk.size())
        {
            size_t split_in_chunk = after_tag - buffer_len_before_this_message;
            post_think_part = chunk.substr(split_in_chunk);
            chunk = chunk.substr(0, split_in_chunk);
        }
        else if (after_tag <= buffer_len_before_this_message)
        {

            post_think_part = chunk;
            chunk.clear();
        }

        if (!post_think_part.empty())
        {
            if (!judge_post_think_candidate(post_think_part))
            {
                chunk += post_think_part;
            }
        }
        return;
    }

    if (!state.post_think_judged)
    {

        if (!chunk.empty())
        {
            if (judge_post_think_candidate(chunk))
            {
                chunk.clear();
            }

        }

        return;
    }

    if (state.bare_json_hold)
    {
        state.held_visible += chunk;
        chunk.clear();

        static constexpr size_t kBareJsonHoldSoftLimit = 16 * 1024;
        static constexpr size_t kBareJsonHoldHardLimit = 64 * 1024;
        bool give_up = state.held_visible.size() > kBareJsonHoldHardLimit ||
            (state.held_visible.size() > kBareJsonHoldSoftLimit
             && state.held_visible.find("\"name\"") == std::string::npos);
        if (give_up)
        {

            chunk = state.held_visible;
            state.held_visible.clear();
            state.bare_json_hold = false;
            state.bare_json_hold_released = true;
        }
        return;
    }

}
