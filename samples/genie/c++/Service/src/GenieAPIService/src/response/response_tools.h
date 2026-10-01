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
    // （Layer2/3）判断是否值得重试、以及重试/终态文案怎么写。成功恢复或外层链已
    // 成功解析时写入 kNone。调用方不关心时传 nullptr，行为与改动前逐字节一致。
    // out_partial_recovery（可选，默认 nullptr）：供 Layer3 分级终态协议使用。仅在
    // Layer1 已经确定出合法工具名、但因必需参数缺失而最终仍判定失败
    // （out_failure_reason==kMissingRequiredArgs）时写入一个"尽力恢复"的部分工具调用
    // {"name":<已确定的真实工具名>, "arguments":<已知参数+缺失必需参数填充空字符串占位>}；
    // 其它所有失败分类（工具名本身就无法确定）下保持为 null（json 默认构造），调用方需
    // 先检查 out_partial_recovery->contains("name") 再使用。调用方不关心时传 nullptr。
    static std::string convertToolCallJson(const std::string &input,
                                           ToolCallFailureReason *out_failure_reason = nullptr,
                                           json *out_partial_recovery = nullptr);

    // Layer -1 兜底：无 <tool_call> 标签时的裸 JSON 工具调用检测（response_tools.md 有完整
    // 设计记录）。修复的缺口：H2 现有裸JSON检测要求 response_buffer trim 后必须以 '{' 开头，
    // 漏掉 "<think>...</think>{...}" 思考前缀、"我将执行:\n{...}" 前置说明文字、
    // ```json 代码围栏包裹这三类真实存在的模型输出形态——这些情形下 isToolResponse 永远
    // 保持 false，Layer1/2/3 全部被跳过，最终 finish_reason="stop" 让客户端误判任务已完成。
    // visible_text：调用方应先用 extractFinalAnswer() 风格逻辑剥掉 <think>...</think> 部分
    //               （本函数内部只负责剥离 ```json/``` 代码围栏，不处理 think 标签，因为
    //               think 标签的剥离规则与 Harmony/General 格式相关，属于调用方的职责）。
    // out_wrapped：命中时写入 wrapJsonInToolCall() 包装后的候选原始文本（未经修复），供调用
    //              方直接送入既有 convertToolCallJson() -> Layer1/2/3 链路，不重复实现修复逻辑。
    // 判定比标签路径更严格：候选必须同时满足"能提取出名字" + "归一化后能匹配到已知工具" +
    // "存在 arguments/params/parameters/args（或 function.arguments）字段"——第三条是关键
    // 区分信号，避免把普通数据 JSON（如 {"name":"Alice","age":3}）误判为工具调用意图；此外
    // 候选不能嵌在大段说明文字中间（避免把"这是一个JSON示例：{...}"误判为工具调用）。
    // 返回 true 表示命中，out_wrapped 有效；返回 false 时 out_wrapped 内容未定义。
    static bool DetectBareToolCall(const std::string &visible_text, std::string &out_wrapped);

    // Layer -1 hold-back 状态机（供 response_dispatcher.cpp 的 genie_callback 复用，也供
    // response_tools_layer1_selftest.cpp 模拟"逐 chunk 到达"场景做离线自测）。字段与生命
    // 周期管理规则见 response_dispatcher.cpp 声明处注释与 response_dispatcher.md「Layer -1
    // hold-back」一节；此处只是把状态搬进一个可被独立测试的结构体，语义不变。
    struct BareJsonHoldState
    {
        bool think_closed = false;
        bool post_think_judged = false;
        bool bare_json_hold = false;
        bool bare_json_hold_released = false;
        std::string held_visible;
    };

    // 对流式生成过程中的一个 chunk 应用 Layer -1 hold-back 规则，原地修改 state 与 chunk
    // （返回后 chunk 即为"实际应该发给客户端"的内容，可能被清空或被之前攒住的 held_visible
    // 顶替）。response_buffer_after_append 必须是调用方已经把本次 message 追加进去之后的
    // 完整缓冲区（用于 rfind("</think>") 定位分割点，与 message.length() 换算出 chunk 在
    // 缓冲区里的分割点配合使用）。纯状态机逻辑，不含任何 I/O（发送/心跳）：心跳保活仍由
    // 调用方根据 state.bare_json_hold 与自己的计时器决定是否发送，不属于本函数职责，这样
    // 才能脱离 sink/SendKeepAlive 依赖被独立单元测试。
    static void ApplyBareJsonHoldBack(BareJsonHoldState &state,
                                      const std::string &response_buffer_after_append,
                                      const std::string &message,
                                      std::string &chunk);

    // 清洗残留的 <tool_call> 标签/JSON 碎片，供 Case A（成功调用附带的多余文本）与 Layer3
    // 情形B（纯文本兜底，输入可能是截断/跨多行的原始 response_buffer）共用。内部含"清洗后仍含
    // <tool_call 子串则强制清空"的最终防线，不能仅凭两条正则本身假设输入已是完整闭合单行标签，
    // 设计取舍与截断样例见 response_tools.md。
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
    // out_partial_recovery（可选，默认 nullptr）：见 convertToolCallJson 同名参数说明——
    // 仅在 out_reason==kMissingRequiredArgs（工具名已确定，必需参数缺失）时写入
    // {"name":..., "arguments":...}（缺失的必需参数填充空字符串占位），供 Layer3 使用。
    static bool TryLayer1Recovery(const std::string &malformedText, json &out_tool_call, ToolCallFailureReason &out_reason,
                                  json *out_partial_recovery = nullptr);
};

#endif //RESPONSE_TOOLS_H
