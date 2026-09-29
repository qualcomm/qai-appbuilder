//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_tools_layer1_selftest.h"
#include "response_tools.h"

#include <nlohmann/json.hpp>
#include <iomanip>
#include <sstream>
#include <string>
#include <vector>

using json = nlohmann::ordered_json;

namespace
{

struct ExpectedArg
{
    std::string key;
    json value;
};

struct Layer1SelfTestCase
{
    std::string id;
    std::string description;
    std::string raw_input;
    bool expect_success;
    std::string expected_name;                  // 仅在 expect_success=true 时有意义
    std::vector<ExpectedArg> expected_args;      // 仅在 expect_success=true 时有意义（只做“至少包含”校验）
    ToolCallFailureReason expected_reason;       // 仅在 expect_success=false 时有意义
};

std::vector<Layer1SelfTestCase> BuildTestCases()
{
    std::vector<Layer1SelfTestCase> c;

    // ── 类别1：截断/未闭合 ──────────────────────────────────────────────────
    c.push_back({"truncated_recoverable_required_complete",
        "截断发生在必需参数已全部给出之后（多余的 trailing 字段被截断）——应成功恢复",
        R"({"name": "read", "arguments": {"path": "notes.txt", "offset": 10, "limit": 50, "trailing": "some extra tex)",
        true, "read", {{"path", json("notes.txt")}}, ToolCallFailureReason::kNone});

    c.push_back({"truncated_no_name_field",
        "截断发生在普通字段里，且整段找不到任何 name/tool 字段——应分类为 kTruncated",
        R"({"status": "in_progress", "note": "continuing to write file conten)",
        false, "", {}, ToolCallFailureReason::kTruncated});

    c.push_back({"truncated_missing_required_arg",
        "截断导致必需参数 content 从未出现——补齐括号后语法合法但语义不完整，应分类为 kMissingRequiredArgs",
        R"({"name": "write", "arguments": {"path": "out.txt")",
        false, "", {}, ToolCallFailureReason::kMissingRequiredArgs});

    // ── 类别2：一次输出混杂多个候选对象 ────────────────────────────────────
    c.push_back({"ambiguous_two_valid_candidates",
        "一次输出混杂 2 个结构合理（各自都能解析出 name 字段）的候选对象——不能盲目取第一个",
        R"({"name":"read","arguments":{"path":"a.txt"}} then also {"name":"write","arguments":{"path":"b.txt","content":"hi"}})",
        false, "", {}, ToolCallFailureReason::kAmbiguousMultipleCalls});

    c.push_back({"not_ambiguous_garbage_then_valid",
        "第一个候选是花括号平衡但完全无法解析的乱码，第二个才是真正合法的候选——不应误判为歧义",
        R"(Sure, {oops this is not json at all} here you go: {"name": "read", "arguments": {"path": "final.txt"}})",
        true, "read", {{"path", json("final.txt")}}, ToolCallFailureReason::kNone});

    // ── 类别3：幻觉出不存在的工具名 ────────────────────────────────────────
    c.push_back({"unknown_tool_hallucinated",
        "模型幻觉出不存在的工具名（与已知工具表编辑距离过大）——应分类为 kUnknownToolName",
        R"({"name": "delete_universe", "arguments": {"path": "x"})",
        false, "", {}, ToolCallFailureReason::kUnknownToolName});

    c.push_back({"fuzzy_match_too_far_unknown",
        "工具名与已知工具编辑距离超过阈值（\"rd\" 与 \"read\"）——不应被宽松匹配，应分类为 kUnknownToolName",
        R"({"name": "rd", "arguments": {"path": "file.txt"})",
        false, "", {}, ToolCallFailureReason::kUnknownToolName});

    // ── 类别4：参数 key 拼写偏差 / 别名归一化 ──────────────────────────────
    c.push_back({"arg_alias_file_path",
        "参数 key 拼写偏差：file_path 应归一化为 path",
        R"({"name": "read", "arguments": {"file_path": "readme.md", "offset": 0})",
        true, "read", {{"path", json("readme.md")}}, ToolCallFailureReason::kNone});

    c.push_back({"arg_alias_cmd",
        "参数 key 拼写偏差：cmd 应归一化为 command",
        R"({"name": "exec", "arguments": {"cmd": "ls -la", "timeout": 30})",
        true, "exec", {{"command", json("ls -la")}}, ToolCallFailureReason::kNone});

    c.push_back({"arg_alias_search_query",
        "参数 key 拼写偏差：search_query 应归一化为 query",
        R"({"name": "web_search", "arguments": {"search_query": "qualcomm npu", "count": 5})",
        true, "web_search", {{"query", json("qualcomm npu")}}, ToolCallFailureReason::kNone});

    c.push_back({"arg_alias_target_url",
        "参数 key 拼写偏差：target_url 应归一化为 url",
        R"({"name": "web_fetch", "arguments": {"target_url": "https://example.com", "maxChars": 500})",
        true, "web_fetch", {{"url", json("https://example.com")}}, ToolCallFailureReason::kNone});

    c.push_back({"arg_alias_file_content",
        "参数 key 拼写偏差：file_content 应归一化为 content",
        R"({"name": "write", "arguments": {"path": "out.md", "file_content": "# Title\n"})",
        true, "write", {{"path", json("out.md")}, {"content", json("# Title\n")}}, ToolCallFailureReason::kNone});

    // ── 类别5：必需参数整体缺失 ────────────────────────────────────────────
    c.push_back({"missing_required_args_edit",
        "必需参数整体缺失：edit 缺少 edits 数组",
        R"({"name": "edit", "arguments": {"path": "main.cpp"})",
        false, "", {}, ToolCallFailureReason::kMissingRequiredArgs});

    c.push_back({"missing_required_args_cron",
        "必需参数整体缺失：cron 缺少 action",
        R"({"name": "cron", "arguments": {"schedule": "* * * * *"})",
        false, "", {}, ToolCallFailureReason::kMissingRequiredArgs});

    c.push_back({"missing_required_args_alias_empty_value",
        "别名命中但值为空字符串，仍应视为缺失：exec 的 cmd 归一化为 command 后值为空",
        R"({"name": "exec", "arguments": {"cmd": "", "timeout": 5})",
        false, "", {}, ToolCallFailureReason::kMissingRequiredArgs});

    // ── 类别6：参数值字符串内部包含花括号（代码片段） ──────────────────────
    c.push_back({"arg_value_contains_braces_code_snippet",
        "参数值字符串内部包含花括号（代码片段）——不能被误判为候选对象边界",
        R"({"name": "write", "arguments": {"path": "main.py", "content": "line1 { inner } line2"})",
        true, "write", {{"path", json("main.py")}, {"content", json("line1 { inner } line2")}}, ToolCallFailureReason::kNone});

    c.push_back({"nested_edits_array_with_braces",
        "edit 工具的 edits 数组内含嵌套对象（花括号），应正确恢复整个结构",
        R"({"name": "edit", "arguments": {"path": "a.cpp", "edits": [{"oldText": "foo", "newText": "bar"}]})",
        true, "edit", {{"path", json("a.cpp")}}, ToolCallFailureReason::kNone});

    // ── 类别7：完全不可解析的乱码 ──────────────────────────────────────────
    c.push_back({"unparseable_no_braces",
        "原始文本完全没有花括号——应分类为 kUnparseable",
        R"(I think you should call the read tool now with path notes.txt)",
        false, "", {}, ToolCallFailureReason::kUnparseable});

    c.push_back({"unparseable_garbage_with_braces",
        "花括号平衡但内容完全不是 JSON 结构——应分类为 kUnparseable（不是截断特征）",
        R"({this is just plain garbage text without any real json structure})",
        false, "", {}, ToolCallFailureReason::kUnparseable});

    c.push_back({"unparseable_random_symbols",
        "完全随机的乱码符号，无花括号——应分类为 kUnparseable",
        R"(@#$%^&*() random noise !!! ???)",
        false, "", {}, ToolCallFailureReason::kUnparseable});

    // ── 类别8：模糊匹配阈值内的拼写偏差（应成功纠正） ──────────────────────
    // 注意：已知工具名长度 > 4 才允许模糊容忍（阈值2）；<=4 字符的短名字（read/edit/exec/
    // cron）只接受精确匹配，样本必须选用长度>4 的工具才能验证“阈值内成功纠正”这一行为，
    // 否则会与类别13 的“短名字零容忍”规则冲突。
    c.push_back({"fuzzy_match_small_typo_success",
        "工具名拼写偏差在编辑距离阈值内且已知工具名长度>4（\"web_serch\" 与 \"web_search\"，距离1）——应模糊匹配成功",
        R"({"name": "web_serch", "arguments": {"query": "npu benchmark", "count": 3})",
        true, "web_search", {{"query", json("npu benchmark")}}, ToolCallFailureReason::kNone});

    // ── 类别9：大小写不敏感 ────────────────────────────────────────────────
    c.push_back({"case_insensitive_tool_name",
        "工具名大小写不敏感：WRITE 应归一化匹配到 write",
        R"({"name": "WRITE", "arguments": {"path": "out.txt", "content": "hello"})",
        true, "write", {{"path", json("out.txt")}, {"content", json("hello")}}, ToolCallFailureReason::kNone});

    // ── 类别10：备选 name 字段 / OpenAI function 风格 ──────────────────────
    c.push_back({"alt_name_key_tool_field",
        "使用 \"tool\" 而非 \"name\" 作为字段名——应仍能提取",
        R"({"tool": "read", "arguments": {"path": "a.txt"})",
        true, "read", {{"path", json("a.txt")}}, ToolCallFailureReason::kNone});

    c.push_back({"openai_function_style",
        "OpenAI function 风格（function.name / function.arguments）——应仍能提取",
        R"({"function": {"name": "read", "arguments": {"path": "spec.md"}})",
        true, "read", {{"path", json("spec.md")}}, ToolCallFailureReason::kNone});

    // ── 类别11：其它工具的必需参数校验 ─────────────────────────────────────
    c.push_back({"browser_action_required_present",
        "browser 工具只有 action 是必需参数（签名里的 \"...\" 不计入必需）——给出后应成功",
        R"({"name": "browser", "arguments": {"action": "click", "selector": "#btn"})",
        true, "browser", {{"action", json("click")}}, ToolCallFailureReason::kNone});

    c.push_back({"extra_unknown_fields_ignored",
        "存在必需参数之外的未知字段——不应影响恢复判定",
        R"({"name": "read", "arguments": {"path": "x.txt", "unexpected_field": 123, "another": true})",
        true, "read", {{"path", json("x.txt")}}, ToolCallFailureReason::kNone});

    // ── 类别12：arguments 整体是 JSON 编码字符串（双重转义），而不是对象 ──────
    c.push_back({"arg_value_arguments_as_json_string_success",
        "arguments 字段本身是双重转义的 JSON 字符串而非对象，且能被正确识别提取为完整调用",
        R"(Sure! I'll do that now. {"name": "write", "arguments": "{\"path\": \"out.txt\", \"content\": \"hello world\"}"} Let me know if you need anything else.)",
        true, "write", {{"path", json("out.txt")}, {"content", json("hello world")}}, ToolCallFailureReason::kNone});

    c.push_back({"arg_value_arguments_as_json_string_missing_required",
        "arguments 字段是 JSON 编码字符串，解析出的对象缺少必需参数 content——应分类为 kMissingRequiredArgs",
        R"(Here it is -> {"name": "write", "arguments": "{\"path\": \"out.txt\"}"} <- done)",
        false, "", {}, ToolCallFailureReason::kMissingRequiredArgs});

    // ── 类别13：短名字（长度<=4）零容忍模糊匹配，避免语义不相关的碰撞 ────────
    c.push_back({"short_name_no_fuzzy_tolerance_exit_vs_edit",
        "\"exit\" 与已知工具 \"edit\" 编辑距离仅为1，但长度<=4 已禁用模糊匹配，且语义完全不相关"
        "（会话终止 vs 文件编辑）——应分类为 kUnknownToolName，不能被误配执行为 edit",
        R"({"name": "exit", "arguments": {"path": "a.cpp", "edits": [{"oldText": "x", "newText": "y"}]})",
        false, "", {}, ToolCallFailureReason::kUnknownToolName});

    // ── 类别14：外层语法合法但 name 为空字符串（Harmony 缺口2 占位符产出场景） ─
    // 真实红队复盘案例：Harmony 处理器对"非标准 to=functions（无函数名）"分支解析失败时，
    // 用空字符串 name 占位拼出 {"name":"","arguments":{...}}——这个整体在语法上是合法 JSON，
    // 会在第一次 json::parse 就直接成功，完全绕开 Layer0 repair 链与 Layer1
    // TryLayer1Recovery（两者都只在 catch 链里被调用）。必须在 name 有效性检查处单独拦截
    // 空字符串（"".is_string()==true 无法被原有 !is_string() 检查捕获）。
    c.push_back({"empty_name_syntactically_valid_outer_json",
        "外层 JSON 语法合法但 name 是空字符串（模拟 Harmony 缺口2 的占位符产出）"
        "——必须被统一防线拦截为 kUnknownToolName，不能原样带着空函数名转发给客户端",
        R"({"name": "", "arguments": {"path": "notes.txt", "content": "hello world"}})",
        false, "", {}, ToolCallFailureReason::kUnknownToolName});

    c.push_back({"empty_name_via_repair_chain_trailing_comma",
        "外层 JSON 需要先经 Layer0 repairJson（尾随逗号）才能解析成功，解析成功后 name 仍是"
        "空字符串——验证统一防线同样覆盖 repair 链成功的路径，不仅是第一次 parse 直接成功的路径",
        R"({"name": "", "arguments": {"path": "notes.txt",},})",
        false, "", {}, ToolCallFailureReason::kUnknownToolName});

    return c;
}

// 从 convertToolCallJson() 的 "<tool_call>\n{...}\n</tool_call>" 包裹结果里取出纯 JSON 对象
json ExtractResultJson(const std::string &wrapped)
{
    std::string stripped = ResponseTools::extractJsonFromToolCall(wrapped);
    size_t start = stripped.find_first_not_of(" \t\r\n");
    size_t end = stripped.find_last_not_of(" \t\r\n");
    if (start == std::string::npos)
    {
        return json();
    }
    stripped = stripped.substr(start, end - start + 1);
    return json::parse(stripped, nullptr, false);
}

} // namespace

bool RunLayer1RecoverySelfTest(std::ostream &out)
{
    std::vector<Layer1SelfTestCase> cases = BuildTestCases();

    int total = static_cast<int>(cases.size());
    int expect_success_count = 0;
    int expect_success_correct = 0;
    int expect_failure_count = 0;
    int expect_failure_correct = 0;

    out << "==================== Layer1 Recovery Self-Test ====================\n";

    for (const auto &tc : cases)
    {
        ToolCallFailureReason actual_reason = ToolCallFailureReason::kNone;
        std::string wrapped = ResponseTools::convertToolCallJson(tc.raw_input, &actual_reason);
        json parsed = ExtractResultJson(wrapped);

        bool actual_success = (actual_reason == ToolCallFailureReason::kNone) &&
                               parsed.is_object() &&
                               parsed.contains("name") &&
                               parsed["name"].is_string() &&
                               parsed["name"].get<std::string>() != "unknow";

        bool case_correct = false;
        std::string verdict_detail;

        if (tc.expect_success)
        {
            ++expect_success_count;
            if (!actual_success)
            {
                verdict_detail = "expected success but got failure (reason=" +
                                  ResponseTools::ToolCallFailureReasonToString(actual_reason) + ")";
            }
            else
            {
                std::string actual_name = parsed.value("name", "");
                bool name_ok = (actual_name == tc.expected_name);
                bool args_ok = true;
                const json &args = parsed.contains("arguments") ? parsed["arguments"] : json();
                for (const auto &expected_arg : tc.expected_args)
                {
                    if (!args.is_object() || !args.contains(expected_arg.key) ||
                        args[expected_arg.key] != expected_arg.value)
                    {
                        args_ok = false;
                        break;
                    }
                }
                case_correct = name_ok && args_ok;
                if (!case_correct)
                {
                    verdict_detail = "name/arguments mismatch (actual name=" + actual_name +
                                      ", arguments=" + (args.is_null() ? "null" : args.dump()) + ")";
                }
            }
            if (case_correct)
            {
                ++expect_success_correct;
            }
        }
        else
        {
            ++expect_failure_count;
            if (actual_success)
            {
                verdict_detail = "expected failure (" +
                                  ResponseTools::ToolCallFailureReasonToString(tc.expected_reason) +
                                  ") but recovery succeeded";
            }
            else
            {
                case_correct = (actual_reason == tc.expected_reason);
                if (!case_correct)
                {
                    verdict_detail = "reason mismatch: expected=" +
                                      ResponseTools::ToolCallFailureReasonToString(tc.expected_reason) +
                                      " actual=" + ResponseTools::ToolCallFailureReasonToString(actual_reason);
                }
            }
            if (case_correct)
            {
                ++expect_failure_correct;
            }
        }

        out << "[" << (case_correct ? "PASS" : "FAIL") << "] " << tc.id << " -- " << tc.description << "\n";
        if (!case_correct)
        {
            out << "       " << verdict_detail << "\n";
        }
    }

    double success_rate = expect_success_count > 0
        ? (100.0 * expect_success_correct / expect_success_count) : 100.0;
    double reason_accuracy = expect_failure_count > 0
        ? (100.0 * expect_failure_correct / expect_failure_count) : 100.0;
    int total_correct = expect_success_correct + expect_failure_correct;

    out << "=====================================================================\n";
    out << std::fixed << std::setprecision(1);
    out << "Total cases: " << total << "\n";
    out << "Expected-success cases: " << expect_success_count
        << ", extracted successfully with correct semantics: " << expect_success_correct
        << " (" << success_rate << "%)\n";
    out << "Expected-failure cases: " << expect_failure_count
        << ", classified with correct failure reason: " << expect_failure_correct
        << " (" << reason_accuracy << "%)\n";
    out << "Overall correct: " << total_correct << "/" << total
        << " (" << (100.0 * total_correct / total) << "%)\n";
    out << "=====================================================================\n";

    return total_correct == total;
}
