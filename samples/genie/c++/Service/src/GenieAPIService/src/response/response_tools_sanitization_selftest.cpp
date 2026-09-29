//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_tools_sanitization_selftest.h"
#include "response_tools.h"

#include <cctype>
#include <string>
#include <vector>

namespace
{

struct SanitizationTestCase
{
    std::string id;
    std::string description;
    std::string raw_input;
    std::string expected_output;
};

std::vector<SanitizationTestCase> BuildTestCases()
{
    std::vector<SanitizationTestCase> c;

    // ── Reviewer 4个具体反例场景：未闭合/跨多行截断 ──────────────────────────
    c.push_back({"truncated_missing_closing_brace_no_closing_tag",
        "缺右括号，无闭合标签——两条既有正则均不匹配，最终防线必须兜底清空",
        "<tool_call>\n{\"name\": \"write\", \"arguments\": {\"path\": \"notes.txt\"",
        ""});

    c.push_back({"truncated_extremely_early",
        "极早截断（连 \"name\": 字段本身都未完整输出）——两条既有正则均不匹配，最终防线必须兜底清空",
        "<tool_call>\n{\"nam",
        ""});

    c.push_back({"truncated_pretty_printed_multiline",
        "pretty-print 多行 JSON（{ 与 \"name\": 分行）+ 后续截断——name_line 正则要求同一行，"
        "不匹配，最终防线必须兜底清空",
        "<tool_call>\n{\n  \"name\": \"write\",\n  \"arguments\": {\n    \"path\": \"notes.txt\"",
        ""});

    c.push_back({"truncated_single_line_name_complete",
        "单行截断但 name 行本身完整——name_line 正则能清除 JSON 碎片，但 <tool_call> 标签本身"
        "会残留，最终防线必须兜底把残留标签也清空",
        "<tool_call>\n{\"name\": \"write\", \"arguments\": {\"path\": \"notes.txt\", \"content\": \"some very long conte",
        ""});

    // ── 正向/负向对照：确保最终防线不会误伤已经清洗干净的正常场景 ────────────
    c.push_back({"clean_single_line_closed_tag_with_trailing_text",
        "完整闭合的单行 <tool_call> 标签（Case A 原始使用场景）——现有正则本应完全清除，"
        "最终防线不应误触发，紧随其后的自然语言文本应完整保留",
        "<tool_call>\n{\"name\": \"write\", \"arguments\": {\"path\": \"notes.txt\"}}\n</tool_call>\nDone.",
        "Done."});

    c.push_back({"plain_text_no_tool_call_tag_at_all",
        "完全不含 <tool_call> 标签的普通自然语言——应原样保留，不受本函数影响",
        "Sure, I can help with that. Here is the summary you asked for.",
        "Sure, I can help with that. Here is the summary you asked for."});

    return c;
}

} // namespace

bool RunToolCallSanitizationSelfTest(std::ostream &out)
{
    std::vector<SanitizationTestCase> cases = BuildTestCases();

    int total = static_cast<int>(cases.size());
    int correct = 0;

    out << "==================== Tool Call Sanitization Self-Test ====================\n";

    for (const auto &tc : cases)
    {
        std::string actual = ResponseTools::remove_tool_call_content(tc.raw_input);
        bool case_correct = (actual == tc.expected_output);

        out << "[" << (case_correct ? "PASS" : "FAIL") << "] " << tc.id << " -- " << tc.description << "\n";
        if (!case_correct)
        {
            out << "       expected=\"" << tc.expected_output << "\" actual=\"" << actual << "\"\n";
        }
        // 额外的、独立于精确字符串匹配之外的硬性断言：无论期望值是什么，清洗结果都绝对不能
        // 含有任何 "<tool_call" 残留（大小写不敏感）——这是本自测存在的核心目的。
        std::string lower_actual = actual;
        for (auto &ch : lower_actual) { ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch))); }
        bool leaked_tag = lower_actual.find("<tool_call") != std::string::npos;
        if (leaked_tag)
        {
            case_correct = false;
            out << "       [LEAK] sanitized output still contains a <tool_call fragment: \"" << actual << "\"\n";
        }

        if (case_correct)
        {
            ++correct;
        }
    }

    out << "=============================================================================\n";
    out << "Total cases: " << total << ", passed: " << correct
        << " (" << (total > 0 ? (100.0 * correct / total) : 100.0) << "%)\n";
    out << "=============================================================================\n";

    return correct == total;
}
