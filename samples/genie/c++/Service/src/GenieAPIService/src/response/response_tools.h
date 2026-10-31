//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RESPONSE_TOOLS_H
#define RESPONSE_TOOLS_H

#include <nlohmann/json.hpp>
#include <httplib.h>

using json = nlohmann::ordered_json;

enum class ToolCallFailureReason
{
    kNone = 0,
    kUnknownToolName,
    kMissingRequiredArgs,
    kTruncated,
    kAmbiguousMultipleCalls,
    kUnparseable
};

struct ResponseTools
{
    static inline const std::string FN_NAME = "<tool_call>";

    static std::string ToolCallFailureReasonToString(ToolCallFailureReason reason);

    static bool post_stream_data(httplib::DataSink &sink, const char *event, const std::string &data, bool done = false);

    static std::string responseDataJson(const std::string &content,
                                        const std::string &finish_reason,
                                        bool stream = true,
                                        const std::string &tool_calls_str = "",
                                        size_t prompt_tokens = 0,
                                        size_t completion_tokens = 0,
                                        const std::string &reasoning_content = "");

    static std::string reasoningDataJson(const std::string &reasoning);

    static std::string statusDataJson(const std::string &status, const std::string &message,
                                      const json &extra_payload = json());

    static bool status_content_visible;

    static bool log_inference_stream;

    static std::string convertToolCallJson(const std::string &input,
                                           ToolCallFailureReason *out_failure_reason = nullptr,
                                           json *out_partial_recovery = nullptr);

    static bool DetectBareToolCall(const std::string &visible_text, std::string &out_wrapped);

    struct BareJsonHoldState
    {
        bool think_closed = false;
        bool post_think_judged = false;
        bool bare_json_hold = false;
        bool bare_json_hold_released = false;
        std::string held_visible;
    };

    static void ApplyBareJsonHoldBack(BareJsonHoldState &state,
                                      const std::string &response_buffer_after_append,
                                      const std::string &message,
                                      std::string &chunk);

    static std::string remove_tool_call_content(const std::string &input);

    static std::string remove_empty_lines(const std::string &input);

    static std::string json_to_str(const json &data);

    static std::string generate_uuid4();

    static std::string extractJsonFromToolCall(const std::string &input);

    static std::string wrapJsonInToolCall(const std::string &jsonContent)
    {
        return "<tool_call>\n" + jsonContent + "\n</tool_call>";
    }

    static json format_tool_calls(const std::string &tool_calls_str);

    static std::string repairJson(const std::string &input);

    static std::string AutoCorrectSkillCall(
        const std::string& tool_call_json_str,
        const std::unordered_map<std::string, std::string>& skill_mappings,
        bool enable_correction
    );

    static std::string fixBackslashes(const std::string &input, bool smart_mode);

private:

    static std::string escapeControlCharsInJsonStrings(const std::string &input);

    static bool IsSkillName(const std::string& tool_name, const std::unordered_map<std::string, std::string>& skill_mappings);
    static std::string RewriteToReadCall(const std::string& skill_name, const std::unordered_map<std::string, std::string>& skill_mappings);

    static bool TryLayer1Recovery(const std::string &malformedText, json &out_tool_call, ToolCallFailureReason &out_reason,
                                  json *out_partial_recovery = nullptr);
};

#endif
