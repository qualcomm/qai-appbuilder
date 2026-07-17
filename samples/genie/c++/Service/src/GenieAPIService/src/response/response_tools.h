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

    // 发送任务状态反馈事件（不含结束符，仅用于流式模式）
    // status: 状态标识，如 "preparing" / "inference" / "tool_call" / "writing_code"
    // message: 展示给客户端的可读描述
    static std::string statusDataJson(const std::string &status, const std::string &message);

    // 调试开关：true=将 message 同时写入 delta.content（客户端可见）；false=delta.content 为空
    // 对应 service_config.json 中的 debug.status_update_content_visible，默认 true
    static bool status_content_visible;

    // 调试开关：true=启用推理输出流程调试日志（kInfo 级别）
    // 包括：FinalizeFinalChannel 的调用状态、flush 内容字节数与预览、末尾标签前缀裁剪情况等
    // 对应 service_config.json 中的 debug.log_inference_stream，默认 false
    static bool log_inference_stream;

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

    // 修复模型输出的非标准 JSON 格式问题
    // 处理：尾随逗号、Python 风格字面量（None/True/False）等
    static std::string repairJson(const std::string &input);

    // 新增：验证工具名是否在白名单中
    static bool ValidateToolName(
        const std::string& tool_name,
        const std::vector<std::string>& allowed_tools,
        bool enable_whitelist
    );

    // 新增：Skill 自动纠偏
    static std::string AutoCorrectSkillCall(
        const std::string& tool_call_json_str,
        const std::unordered_map<std::string, std::string>& skill_mappings,
        bool enable_correction
    );

    // 修复JSON字符串中的反斜杠问题（public，供 harmony.cpp 等外部调用）
    // smart_mode: true - 智能模式，保留已有的转义序列；false - 简单模式，转义所有反斜杠
    // 用于在 json::parse 之前预处理模型输出的路径字符串，防止 \n \t 等被误解析为控制字符
    static std::string fixBackslashes(const std::string &input, bool smart_mode);

private:
    // 转义 JSON 字符串值内部的字面控制字符（\n \r \t 等）。
    // 仅处理 JSON 字符串值内部的内容，不影响 JSON 结构字符（{} [] : , 等）。
    // 用于修复模型生成的 write/edit 工具调用中 content 字段含字面换行符的问题。
    static std::string escapeControlCharsInJsonStrings(const std::string &input);

    static bool IsSkillName(const std::string& tool_name, const std::unordered_map<std::string, std::string>& skill_mappings);
    static std::string RewriteToReadCall(const std::string& skill_name, const std::unordered_map<std::string, std::string>& skill_mappings);
};

#endif //RESPONSE_TOOLS_H
