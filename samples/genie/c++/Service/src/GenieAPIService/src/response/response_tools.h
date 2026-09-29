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

// Layer 1 兜底提取失败分类（convertToolCallJson 现有正则修复链全部失败之后使用）。
// 只在"工具名 + 全部必需参数均已确定"时才由 Layer1 采纳恢复结果；否则输出以下分类之一，
// 供后续 Layer2（服务端内部隐形重试，未来步骤实现）/Layer3（分级终态协议，未来步骤实现）消费。
// kNone 表示 Layer1 未被触发（外层链已成功）或已成功恢复，不代表失败。
enum class ToolCallFailureReason
{
    kNone = 0,
    kUnknownToolName,           // 提取出候选对象，但工具名归一化/模糊匹配后仍无法对应任何已知工具
    kMissingRequiredArgs,       // 工具名已确定，但该工具的必需参数未能全部确定
    kTruncated,                 // 候选子串存在明确截断特征（字符串值中途中断，或明显缺少右括号）
    kAmbiguousMultipleCalls,    // 原始文本中混杂 >=2 个结构合理（含 name/tool 字段迹象）的候选对象
    kUnparseable                // 原始文本中找不到任何花括号平衡的候选对象
};

struct ResponseTools
{
    static inline const std::string FN_NAME = "<tool_call>";

    // 将失败分类转换为可读字符串（用于日志，以及未来 Layer2 纠错文案分类）
    static std::string ToolCallFailureReasonToString(ToolCallFailureReason reason);

    static bool post_stream_data(httplib::DataSink &sink, const char *event, const std::string &data, bool done = false);

    static std::string responseDataJson(const std::string &content,
                                        const std::string &finish_reason,
                                        bool stream = true,
                                        const std::string &tool_calls_str = "");

    // 发送任务状态反馈事件（不含结束符，仅用于流式模式）
    // status: 状态标识，如 "preparing" / "inference" / "tool_call" / "writing_code"
    // message: 展示给客户端的可读描述
    // extra_payload: 可选，随 status/status_message 一并合并进 choices[0]（同层级）的额外
    //                字段（如 PromptLedger::ToJson()），默认空对象即完全不改变既有帧结构，
    //                供 status="prompt_optimized" 这类需要携带机器可读账本数据的帧复用
    static std::string statusDataJson(const std::string &status, const std::string &message,
                                      const json &extra_payload = json());

    // 调试开关：true=将 message 同时写入 delta.content（客户端可见）；false=delta.content 为空
    // 对应 service_config.json 中的 debug.status_update_content_visible，默认 true
    static bool status_content_visible;

    // 调试开关：true=启用推理输出流程调试日志（kInfo 级别）
    // 包括：FinalizeFinalChannel 的调用状态、flush 内容字节数与预览、末尾标签前缀裁剪情况等
    // 对应 service_config.json 中的 debug.log_inference_stream，默认 false
    static bool log_inference_stream;

    // out_failure_reason（可选，默认 nullptr）：当外层正则修复链与 Layer1 均未能采纳
    // 出合法工具调用（即最终仍落回 name="unknow"）时，写入结构化失败分类，供调用方
    // （未来 Layer2/3）判断是否值得重试、以及重试/终态文案怎么写。成功恢复或外层链已
    // 成功解析时写入 kNone。调用方不关心时传 nullptr，行为与改动前逐字节一致。
    static std::string convertToolCallJson(const std::string &input, ToolCallFailureReason *out_failure_reason = nullptr);

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

    // Layer 1 兜底：关键内容提取重组。仅在 convertToolCallJson 现有正则修复链
    // （fixBackslashes/repairJson/escapeControlCharsInJsonStrings）全部失败之后调用，
    // 纯本地字符串处理、零推理开销。
    // malformedText：外层链尝试解析失败的原始候选文本（extractJsonFromToolCall 之后、
    //                fixBackslashes 之前，即模型原始输出去除 <tool_call> 标签后的内容）。
    // out_tool_call：成功时写入恢复出的 {"name":..., "arguments":{...}} 对象。
    // out_reason：无论成功失败都写入分类结果（成功时为 kNone）。
    // 返回 true 表示"工具名 + 全部必需参数均已确定"，可采纳 out_tool_call 替换 unknow 兜底；
    // 返回 false 时 out_tool_call 内容未定义，调用方必须改用 out_reason 驱动后续分支。
    static bool TryLayer1Recovery(const std::string &malformedText, json &out_tool_call, ToolCallFailureReason &out_reason);
};

#endif //RESPONSE_TOOLS_H
