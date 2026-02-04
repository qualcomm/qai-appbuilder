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

struct ResponseTools
{
    static inline const std::string FN_NAME = "<tool_call>";

    static bool post_stream_data(httplib::DataSink &sink, const char *event, const std::string &data, bool done = false);

    static std::string responseDataJson(const std::string &content,
                                        const std::string &finish_reason,
                                        bool stream = true,
                                        const std::string &tool_calls_str = "");

    static std::string convertToolCallJson(const std::string &input);

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
};

#endif //RESPONSE_TOOLS_H
