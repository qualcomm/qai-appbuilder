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
    ss << generate_hex(8) << "-"      // 8 hex digits
       << generate_hex(4) << "-"      // 4 hex digits
       << "4" << generate_hex(3) << "-" // 4 + 3 hex digits (UUID version 4)
       << dis2(gen) << generate_hex(3) << "-" // 1 special + 3 hex digits
       << generate_hex(12);           // 12 hex digits

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

// 静态成员定义：默认 true（message 同时写入 delta.content，客户端可见）
bool ResponseTools::status_content_visible = true;

// 静态成员定义：默认 false（推理输出流程调试日志默认关闭）
bool ResponseTools::log_inference_stream = false;

std::string ResponseTools::statusDataJson(const std::string &status, const std::string &message)
{
    std::string id = generate_uuid4();
    int created = timer.GetSystemTime();
    // 使用标准 chat.completion.chunk 格式，确保客户端能正确解析而不会断开连接。
    // 状态信息通过 choices[0] 中的自定义字段 status/status_message 传递，
    // finish_reason 为 null，符合 OpenAI SSE 流式协议。
    // delta.content：debug.status_update_content_visible=true 时填充 message（客户端可见），
    //               false 时为空字符串（客户端不显示，仅用于保活连接）。
    const std::string delta_content = status_content_visible ? (message + "\n") : "";
    json data = {
        {"id",      id},
        {"object",  "chat.completion.chunk"},
        {"created", created},
        {"model",   ""},
        {"choices", json::array({{
            {"index",          0},
            {"finish_reason",  nullptr},
            {"delta",          {{"content", delta_content}}},
            {"status",         status},
            {"status_message", message}
        }})}
    };
    return json_to_str(data);
}

std::string ResponseTools::responseDataJson(const std::string &content,
                                            const std::string &finish_reason,
                                            bool stream,
                                            const std::string &tool_calls_str)
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

    /* @formatter:off */
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
                         {"prompt_tokens", 0},
                         {"completion_tokens", 0},
                         {"total_tokens", 0}
            }}
    };
    /* @formatter:on */
    return json_to_str(data);
}

std::string ResponseTools::convertToolCallJson(const std::string &input)
{
    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[DEBUG-TOOL-CALL] Raw model output (input):\n" << input << std::endl;
    }

    std::string jsonStr = extractJsonFromToolCall(input);

    // 预处理:修复未转义的反斜杠(Windows路径问题)
    // 使用智能模式，避免重复转义已经转义的反斜杠
    std::string fixedJsonStr = fixBackslashes(jsonStr, true);

    if (ResponseTools::log_inference_stream)
    {
        My_Log{} << "[DEBUG-TOOL-CALL] ResponseTools::convertToolCallJson: \n" << wrapJsonInToolCall(fixedJsonStr) << std::endl;
    }

    json root;
    try
    {
        root = json::parse(fixedJsonStr);
    }
    catch (const std::exception &e)
    {
        // 外层 JSON 解析失败，尝试修复非标准格式（尾随逗号、Python 字面量等）
        My_Log{} << "[DEBUG-TOOL-CALL] Outer JSON parse failed (" << e.what() << "), trying repairJson..." << std::endl;
        std::string repairedJsonStr = repairJson(fixedJsonStr);
        try
        {
            root = json::parse(repairedJsonStr);
        }
        catch (const std::exception &e2)
        {
            // repairJson 仍然失败，尝试转义字符串值内的字面控制字符（\n \r \t 等）。
            // 背景：模型生成的 write/edit 工具调用中，content 字段可能包含字面换行符
            // （U+000A），这在 JSON 规范中是非法的，必须转义为 \n（两字符序列）。
            // fixBackslashes 只处理反斜杠，不处理控制字符，因此需要额外一步。
            // 策略：只转义 JSON 字符串值内部的控制字符，字符串外部的结构字符不受影响。
            My_Log{} << "[DEBUG-TOOL-CALL] repairJson failed (" << e2.what()
                     << "), trying to escape literal control chars in string values..." << std::endl;
            std::string ctrlFixedStr = escapeControlCharsInJsonStrings(repairedJsonStr);
            try
            {
                root = json::parse(ctrlFixedStr);
            }
            catch (const std::exception &e3)
            {
                My_Log{My_Log::Level::kError} << "parse tool calls's message as json failed:" << e3.what() << std::endl;
                root["name"] = "unknow";
                root["arguments"] = fixedJsonStr;
                goto done;
            }
        }
    }

    if (!root.contains("name") || !root["name"].is_string())
    {
        My_Log{My_Log::Level::kError} << "the tool calls's name as string failed\n";
        root["name"] = "unknow";
    }
    else if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[DEBUG-TOOL-CALL] Parsed tool name: " << root["name"].get<std::string>() << std::endl;
    }

    // 容错处理: 检查 arguments 是否被错误地双重转义为字符串
    if (root.contains("arguments") && root["arguments"].is_string())
    {
        std::string args_str = root["arguments"].get<std::string>();

        // 先对 args_str 进行 JSON 修复（尾随逗号、Python 字面量等），再尝试解析
        std::string repaired_args = repairJson(args_str);

        // 尝试解析（优先使用修复后的版本）
        try
        {
            json args_obj = json::parse(repaired_args);
            if (args_obj.is_object())
            {
                // 如果成功解析为对象，替换原来的字符串
                if (ResponseTools::log_inference_stream)
                {
                    My_Log{My_Log::Level::kInfo}
                        << "[DEBUG-TOOL-CALL] arguments string converted to object:\n" << args_obj.dump(2) << std::endl;
                }
                root["arguments"] = args_obj;
            }
        }
        catch (const std::exception &e)
        {
            // 如果解析失败，可能有两种原因：
            // 原因1：args_str 中包含实际控制字符（如换行符 \n、回车符 \r 等）
            //        这是因为 JSON 库在解析外层 JSON 时已经将 \\n 转换为实际换行符
            //        例如: "{\"content\":\"line1\\nline2\"}" 被解析后 content 包含实际换行符
            // 原因2：args_str 中包含未转义的反斜杠（Windows路径问题）
            //        例如: "{\"path\": \"C:\\Work\\...\"}" 被解析后变成 {"path": "C:\Work\..."}
            My_Log{} << "Arguments parse failed (" << e.what() << "), trying to fix..." << std::endl;

            // 步骤1：只转义控制字符（不转义反斜杠）
            // 适用于原因1：args_str 中的反斜杠已经正确转义（如 C:\\Temp\\），
            // 只需要将实际换行符等控制字符转义为 JSON 转义序列
            std::string ctrl_fixed;
            ctrl_fixed.reserve(args_str.size() * 2);
            for (size_t i = 0; i < args_str.size(); ++i) {
                unsigned char c = static_cast<unsigned char>(args_str[i]);
                if (c == '\n') {
                    ctrl_fixed += "\\n";
                } else if (c == '\r') {
                    ctrl_fixed += "\\r";
                } else if (c == '\t') {
                    ctrl_fixed += "\\t";
                } else if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned int>(c));
                    ctrl_fixed += buf;
                } else {
                    ctrl_fixed += args_str[i];
                }
            }

            try
            {
                json args_obj = json::parse(ctrl_fixed);
                if (args_obj.is_object())
                {
                    My_Log{} << "Successfully fixed control chars and parsed arguments" << std::endl;
                    root["arguments"] = args_obj;
                }
            }
            catch (const std::exception &e2)
            {
                // 步骤2：如果还是失败，尝试简单模式（转义所有反斜杠）
                // 适用于原因2：args_str 中包含单个反斜杠（如 C:\Work\...）
                My_Log{} << "Control char fix failed (" << e2.what() << "), trying backslash fix..." << std::endl;

                std::string fixed_args_str = fixBackslashes(args_str, false);

                try
                {
                    json args_obj = json::parse(fixed_args_str);
                    if (args_obj.is_object())
                    {
                        My_Log{} << "Successfully fixed backslashes and parsed arguments" << std::endl;
                        root["arguments"] = args_obj;
                    }
                }
                catch (const std::exception &e3)
                {
                    // 如果还是失败，保持原样（字符串形式）
                    My_Log{} << "Arguments remains as string (all fixes failed): " << e3.what() << std::endl;
                }
            }
        }
    }

    // Always be string, whatever input is object or string.
    // after dump, the object will be escaping string
    if (!root.contains("arguments") || (!root["arguments"].is_object() && !root["arguments"].is_string()))
    {
        My_Log{My_Log::Level::kError} << "parse tool calls's args as json failed:" << std::endl;
        root["arguments"] = jsonStr;
        goto done;
    }

    done:
    std::string final_result = root.dump();
    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[DEBUG-TOOL-CALL] Final JSON:\n" << final_result << std::endl;
    }

    return wrapJsonInToolCall(final_result);
}

std::string ResponseTools::remove_tool_call_content(const std::string &input)
{
    static std::regex tool_call_block(R"(<tool_call>[\s\S]*?<\/tool_call>\s*)");
    static std::regex name_line(R"((\s*\{ *"name": [^\n]*\n?))");
    std::string result = std::regex_replace(input, tool_call_block, "");
    result = std::regex_replace(result, name_line, "");
    result = remove_empty_lines(result);
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

        // 根据 OpenAI API 规范，arguments 字段必须是 JSON 字符串（不是对象）
        // 如果 arguments 已经是字符串，直接使用（避免双重序列化产生外层引号）
        // 如果 arguments 是对象，序列化为 JSON 字符串
        std::string args_value;
        if (call.contains("arguments")) {
            if (call["arguments"].is_string()) {
                // 直接取字符串值，避免 dump() 产生双重序列化
                // dump() 对字符串会输出 "\"...\""（带外层引号），导致客户端收到双重序列化的参数
                args_value = call["arguments"].get<std::string>();
            } else {
                // 对象类型：序列化为 JSON 字符串（正常路径）
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

std::string ResponseTools::extractJsonFromToolCall(const std::string &input)
{
    std::string output = str_replace(input, "<tool_call>", "");
    output = str_replace(output, "</tool_call>", "");
    output = str_replace(output, "[/tool_call]", "");
    return output;
}

// 在单次扫描中完成所有 JSON 修复，全程跟踪 JSON 字符串边界：
//   修复1：移除尾随逗号（,} 或 ,]）
//   修复2：替换 Python 字面量（None/True/False -> null/true/false）
//   修复3：移除孤立的空 key 片段（,"" 后紧跟 , 或 } 或 ]，无对应 value）
//          例如：{"name":"read","","arguments":{...}}
//             -> {"name":"read","arguments":{...}}
//          这是模型生成畸形 JSON 时的常见错误（多余的空字符串 key）
//
// 字符串值内部的内容完全不受影响，例如：
//   {"key": "value,}"}   -> 不变（,} 在字符串内，不是尾随逗号）
//   {"newText": "None"}  -> 不变（None 在字符串内，不是 Python 字面量）
//   {"value": None,}     -> {"value": null}（None 和尾随逗号均在字符串外）

// 辅助函数：从 pos 开始跳过一个完整的 JSON 字符串（含首尾引号），返回结束后的位置
// 若 pos 不指向 '"'，直接返回 pos
static size_t skipJsonString(const std::string &s, size_t pos)
{
    if (pos >= s.size() || s[pos] != '"') return pos;
    ++pos; // 跳过开头的 "
    while (pos < s.size())
    {
        if (s[pos] == '\\' && pos + 1 < s.size())
        {
            pos += 2; // 跳过转义序列
        }
        else if (s[pos] == '"')
        {
            ++pos; // 跳过结尾的 "
            break;
        }
        else
        {
            ++pos;
        }
    }
    return pos;
}

static std::string repairJsonOutsideStrings(const std::string &input)
{
    std::string result;
    result.reserve(input.size());

    auto isWordChar = [](char c) -> bool {
        return std::isalnum(static_cast<unsigned char>(c)) || c == '_';
    };

    size_t i = 0;
    while (i < input.size())
    {
        if (input[i] == '"')
        {
            // ── 进入 JSON 字符串：原样复制直到遇到未转义的闭合引号 ──
            result += input[i++];
            while (i < input.size())
            {
                if (input[i] == '\\' && i + 1 < input.size())
                {
                    // 转义序列：复制两个字符（含 \" ，避免误判为字符串结束）
                    result += input[i];
                    result += input[i + 1];
                    i += 2;
                }
                else if (input[i] == '"')
                {
                    result += input[i++]; // 字符串结束
                    break;
                }
                else
                {
                    result += input[i++];
                }
            }
        }
        else if (input[i] == ',')
        {
            // ── 修复1：检查是否为尾随逗号 ──
            // 向前跳过空白，若紧跟 } 或 ] 则为尾随逗号，直接丢弃
            size_t j = i + 1;
            while (j < input.size() && (input[j] == ' ' || input[j] == '\t' ||
                                         input[j] == '\n' || input[j] == '\r'))
                ++j;
            if (j < input.size() && (input[j] == '}' || input[j] == ']'))
            {
                i++; // 跳过逗号，不写入 result
            }
            // ── 修复3：检查是否为孤立的空 key 片段（,"" 后无 value）──
            // 模式：,<空白>"" <空白> (,|]|})
            // 例如：{"name":"read","","arguments":{...}}
            //                    ^^^^ 这个 ,"" 是孤立的空 key，需要删除
            else if (j < input.size() && input[j] == '"')
            {
                // 尝试跳过这个字符串，看它是否是一个空字符串 key（即 ""）
                size_t str_end = skipJsonString(input, j);
                // 检查是否是空字符串（"" 即 j+1 == str_end-1，中间只有两个引号）
                bool is_empty_str = (str_end == j + 2); // "" 占 2 个字符
                if (is_empty_str)
                {
                    // 跳过空白，检查后面是否是 , 或 } 或 ]（说明没有 value，是孤立 key）
                    size_t k = str_end;
                    while (k < input.size() && (input[k] == ' ' || input[k] == '\t' ||
                                                 input[k] == '\n' || input[k] == '\r'))
                        ++k;
                    if (k < input.size() && (input[k] == ',' || input[k] == '}' || input[k] == ']'))
                    {
                        // 确认是孤立空 key，跳过整个 ,"" 片段（不写入 result）
                        i = str_end; // 跳过 , 和 ""，后续的 , 或 } 或 ] 留给下一轮处理
                        continue;
                    }
                }
                // 不是孤立空 key，正常写入逗号
                result += input[i++];
            }
            else
            {
                result += input[i++];
            }
        }
        else
        {
            // ── 修复2：检查 Python 字面量（仅在字符串外部，且满足单词边界）──
            bool leftBound = (i == 0) || !isWordChar(input[i - 1]);

            if (leftBound)
            {
                // None -> null
                if (i + 4 <= input.size() && input.compare(i, 4, "None") == 0)
                {
                    bool rightBound = (i + 4 >= input.size()) || !isWordChar(input[i + 4]);
                    if (rightBound) { result += "null";  i += 4; continue; }
                }
                // True -> true
                if (i + 4 <= input.size() && input.compare(i, 4, "True") == 0)
                {
                    bool rightBound = (i + 4 >= input.size()) || !isWordChar(input[i + 4]);
                    if (rightBound) { result += "true";  i += 4; continue; }
                }
                // False -> false
                if (i + 5 <= input.size() && input.compare(i, 5, "False") == 0)
                {
                    bool rightBound = (i + 5 >= input.size()) || !isWordChar(input[i + 5]);
                    if (rightBound) { result += "false"; i += 5; continue; }
                }
            }

            result += input[i++];
        }
    }
    return result;
}

std::string ResponseTools::repairJson(const std::string &input)
{
    // 单次扫描完成全部修复，正确处理 JSON 字符串边界：
    //   1. 移除尾随逗号（,} 或 ,]）—— 原正则不区分字符串内外，存在误修复风险
    //   2. 替换 Python 字面量（None/True/False -> null/true/false）
    return repairJsonOutsideStrings(input);
}

bool ResponseTools::IsSkillName(const std::string& tool_name, const std::unordered_map<std::string, std::string>& skill_mappings) {
    return skill_mappings.find(tool_name) != skill_mappings.end();
}

std::string ResponseTools::RewriteToReadCall(
    const std::string& skill_name, 
    const std::unordered_map<std::string, std::string>& skill_mappings
) {
    auto it = skill_mappings.find(skill_name);
    if (it == skill_mappings.end()) {
        return "";
    }
    
    json rewritten_call = {
        {"name", "read"},
        {"arguments", {
            {"path", it->second}
        }}
    };
    
    return rewritten_call.dump();
}

std::string ResponseTools::AutoCorrectSkillCall(
    const std::string& tool_call_json_str,
    const std::unordered_map<std::string, std::string>& skill_mappings,
    bool enable_correction
) {
    if (!enable_correction || skill_mappings.empty()) {
        return tool_call_json_str;
    }

    // convertToolCallJson 返回的是 <tool_call>..JSON..</tool_call> 格式，
    // 需要先剥离标签，处理纯 JSON，再重新包装。
    // 同时也兼容直接传入纯 JSON 字符串的场景（如单元测试）。
    bool has_tool_call_tag = (tool_call_json_str.find("<tool_call>") != std::string::npos);
    std::string json_str = has_tool_call_tag
        ? extractJsonFromToolCall(tool_call_json_str)
        : tool_call_json_str;

    // 去除首尾空白（extractJsonFromToolCall 可能留有换行符）
    size_t start = json_str.find_first_not_of(" \t\r\n");
    size_t end   = json_str.find_last_not_of(" \t\r\n");
    if (start != std::string::npos) {
        json_str = json_str.substr(start, end - start + 1);
    }

    try {
        // 解析工具调用 JSON
        json tool_call = json::parse(json_str);

        if (!tool_call.contains("name") || !tool_call["name"].is_string()) {
            return tool_call_json_str;
        }

        std::string tool_name = tool_call["name"].get<std::string>();
        bool modified = false;

        // 检查是否是 Skill 名称（精确匹配）
        // 同时支持模糊匹配：模型有时会省略 "skill_" 前缀，直接用 Skill 的短名称调用
        // 例如：skill_mappings 中注册的是 "skill_stooq_market_simple"，
        //       但模型生成的工具名是 "stooq_market_simple"（缺少 "skill_" 前缀）
        {
            std::string matched_skill_id;
            if (IsSkillName(tool_name, skill_mappings)) {
                // 精确匹配：tool_name 本身就是 Skill ID（如 "skill_stooq_market_simple"）
                matched_skill_id = tool_name;
            } else {
                // 模糊匹配：尝试加 "skill_" 前缀后再查找
                std::string prefixed = "skill_" + tool_name;
                if (IsSkillName(prefixed, skill_mappings)) {
                    matched_skill_id = prefixed;
                }
            }

            if (!matched_skill_id.empty()) {
                My_Log{My_Log::Level::kWarning}
                    << "[Auto-Correction] Model incorrectly called skill '" << tool_name
                    << "' as a tool (matched skill_id='" << matched_skill_id
                    << "'). Rewriting to read(SKILL.md)." << std::endl;

                // 改写为 read 调用
                std::string rewritten = RewriteToReadCall(matched_skill_id, skill_mappings);
                if (!rewritten.empty()) {
                    // 重新包装为 <tool_call> 格式（与 convertToolCallJson 输出格式一致）
                    return has_tool_call_tag ? wrapJsonInToolCall(rewritten) : rewritten;
                }
            }
        }

        // ── 兼容旧版参数名 ──────────────────────────────────────────────────────
        // 新版 OpenClaw 工具协议：read/write/edit 的路径参数统一为 "path"
        // 旧版使用 "file_path"；若模型仍输出旧参数名，自动迁移到 "path"。
        //
        // 同时兼容旧版 edit 格式：旧版 edit 使用顶层 oldText/newText，
        // 新版 edit 使用 edits 数组（[{oldText, newText}, ...]）。
        // 若模型输出旧格式，自动转换为新格式。
        //
        // 情况1：arguments 是对象（convertToolCallJson 正常解析路径）
        if (tool_call.contains("arguments") && tool_call["arguments"].is_object()) {
            auto& args = tool_call["arguments"];

            // 1a. file_path → path（适用于 read / write / edit）
            if (args.contains("file_path") && !args.contains("path")) {
                My_Log{My_Log::Level::kWarning}
                    << "[Auto-Correction] Tool '" << tool_name
                    << "' used legacy 'file_path' instead of 'path'. Auto-fixing." << std::endl;
                args["path"] = args["file_path"];
                args.erase("file_path");
                modified = true;
            }

            // 1b. 旧版 edit 格式（顶层 oldText/newText）→ 新版 edits 数组
            if (tool_name == "edit" && !args.contains("edits") &&
                args.contains("oldText") && args.contains("newText")) {
                My_Log{My_Log::Level::kWarning}
                    << "[Auto-Correction] edit tool used legacy flat oldText/newText format. "
                    << "Converting to edits array." << std::endl;
                json edit_item = {
                    {"oldText", args["oldText"]},
                    {"newText", args["newText"]}
                };
                args["edits"] = json::array({edit_item});
                args.erase("oldText");
                args.erase("newText");
                modified = true;
            }
        }
        // 情況2：arguments 是字符串（convertToolCallJson 解析失败时的降级路径）
        // 需要先解析字符串为 JSON，修改后再序列化回字符串
        else if (tool_call.contains("arguments") && tool_call["arguments"].is_string()) {
            std::string args_str = tool_call["arguments"].get<std::string>();
            try {
                json args_obj = json::parse(args_str);
                if (args_obj.is_object()) {
                    bool inner_modified = false;

                    // 2a. file_path → path
                    if (args_obj.contains("file_path") && !args_obj.contains("path")) {
                        My_Log{My_Log::Level::kWarning}
                            << "[Auto-Correction] Tool '" << tool_name
                            << "' used legacy 'file_path' instead of 'path' (string args). Auto-fixing." << std::endl;
                        args_obj["path"] = args_obj["file_path"];
                        args_obj.erase("file_path");
                        inner_modified = true;
                    }

                    // 2b. 旧版 edit 格式 → edits 数组
                    if (tool_name == "edit" && !args_obj.contains("edits") &&
                        args_obj.contains("oldText") && args_obj.contains("newText")) {
                        My_Log{My_Log::Level::kWarning}
                            << "[Auto-Correction] edit tool used legacy flat oldText/newText format "
                            << "(string args). Converting to edits array." << std::endl;
                        json edit_item = {
                            {"oldText", args_obj["oldText"]},
                            {"newText", args_obj["newText"]}
                        };
                        args_obj["edits"] = json::array({edit_item});
                        args_obj.erase("oldText");
                        args_obj.erase("newText");
                        inner_modified = true;
                    }

                    if (inner_modified) {
                        tool_call["arguments"] = args_obj.dump();
                        modified = true;
                    }
                }
            } catch (const json::exception&) {
                // 字符串无法解析为 JSON，跳过修复
            }
        }

        if (modified) {
            std::string fixed_json = tool_call.dump();
            return has_tool_call_tag ? wrapJsonInToolCall(fixed_json) : fixed_json;
        }

        return tool_call_json_str;

    } catch (const json::exception& e) {
        My_Log{My_Log::Level::kError}
            << "[Auto-Correction] JSON parse error: " << e.what() << std::endl;
        return tool_call_json_str;
    }
}

bool ResponseTools::ValidateToolName(
    const std::string& tool_name,
    const std::vector<std::string>& allowed_tools,
    bool enable_whitelist
) {
    if (!enable_whitelist || allowed_tools.empty()) {
        return true;  // 白名单未启用，允许所有工具
    }
    
    bool is_allowed = std::find(allowed_tools.begin(), allowed_tools.end(), tool_name) != allowed_tools.end();
    
    if (!is_allowed) {
        My_Log{My_Log::Level::kWarning}
            << "[Tool Whitelist] Rejected tool call: '" << tool_name 
            << "' is not in the allowed list." << std::endl;
    }
    
    return is_allowed;
}

// 转义 JSON 字符串值内部的字面控制字符（\n \r \t 等）。
// 仅处理 JSON 字符串值内部的内容，不影响 JSON 结构字符（{} [] : , 等）。
//
// 背景：模型生成的 write/edit 工具调用中，content 字段可能包含字面换行符（U+000A），
// 这在 JSON 规范中是非法的，必须转义为 \n（两字符序列）。
// fixBackslashes 只处理反斜杠，不处理控制字符，因此需要此函数作为补充。
//
// 算法：逐字符扫描，跟踪是否在 JSON 字符串值内部：
//   - 在字符串外：原样复制（保留 JSON 结构字符）
//   - 在字符串内：遇到控制字符时转义，遇到 \" 时跳过（转义序列）
std::string ResponseTools::escapeControlCharsInJsonStrings(const std::string &input)
{
    std::string result;
    result.reserve(input.size() * 2);

    bool in_string = false;

    for (size_t i = 0; i < input.size(); ++i)
    {
        unsigned char c = static_cast<unsigned char>(input[i]);

        if (!in_string)
        {
            // 字符串外：检测字符串开始
            if (c == '"')
            {
                in_string = true;
            }
            result += input[i];
        }
        else
        {
            // 字符串内：处理转义序列和控制字符
            if (c == '\\' && i + 1 < input.size())
            {
                // 已有转义序列：原样复制两个字符，不做额外处理
                result += input[i];
                result += input[i + 1];
                ++i;
            }
            else if (c == '"')
            {
                // 字符串结束
                in_string = false;
                result += input[i];
            }
            else if (c == '\n')
            {
                result += "\\n";
            }
            else if (c == '\r')
            {
                result += "\\r";
            }
            else if (c == '\t')
            {
                result += "\\t";
            }
            else if (c < 0x20)
            {
                // 其他控制字符：转义为 \uXXXX
                char buf[8];
                snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned int>(c));
                result += buf;
            }
            else
            {
                result += input[i];
            }
        }
    }
    return result;
}

std::string ResponseTools::fixBackslashes(const std::string &input, bool smart_mode)
{
    std::string result;
    result.reserve(input.size() * 2);

    if (smart_mode)
    {
        // 智能模式：保留已有的转义序列，只转义未转义的反斜杠
        for (size_t i = 0; i < input.size(); ++i) {
            if (input[i] == '\\') {
                if (i + 1 < input.size()) {
                    char next = input[i + 1];
                    // 如果后面是有效的转义字符,保持原样(复制两个字符)
                    if (next == '"' || next == '\\' || next == '/' ||
                        next == 'b' || next == 'f' || next == 'n' ||
                        next == 'r' || next == 't' || next == 'u') {
                        result += input[i];     // 添加反斜杠
                        result += input[i + 1]; // 添加转义字符
                        ++i; // 跳过下一个字符,因为已经处理了
                    } else {
                        // 否则,这是一个未转义的反斜杠,需要转义
                        result += "\\\\";
                    }
                } else {
                    // 字符串末尾的反斜杠,需要转义
                    result += "\\\\";
                }
            } else {
                result += input[i];
            }
        }
    }
    else
    {
        // 简单模式：转义所有反斜杠
        for (size_t i = 0; i < input.size(); ++i) {
            if (input[i] == '\\') {
                result += "\\\\";
            } else {
                result += input[i];
            }
        }
    }

    return result;
}
