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

// ── Layer -1（DetectBareToolCall）自测用例 ─────────────────────────────────────

// 复刻 ResponseDispatcher::extractFinalAnswer() 在 General 格式下的剥离规则（简化版，
// 只覆盖"取 </think> 之后的子串"这一条）：DetectBareToolCall 的文档化契约是调用方先
// 剥掉 <think>...</think> 再传入，本自测借此模拟真实调用方的输入，而不是在 raw_input
// 里直接测试 think 剥离逻辑本身（那是 ResponseDispatcher::extractFinalAnswer 的职责）。
std::string StripThinkPrefixForTest(const std::string &raw)
{
    const std::string tag = "</think>";
    size_t pos = raw.find(tag);
    if (pos != std::string::npos)
    {
        return raw.substr(pos + tag.length());
    }
    return raw;
}

struct BareToolCallSelfTestCase
{
    std::string id;
    std::string description;
    std::string raw_input;      // 模拟真实 response_buffer 内容（可能含 <think> 前缀）
    bool expect_detect;
    std::string expected_tool_name;  // 仅在 expect_detect=true 时有意义
};

std::vector<BareToolCallSelfTestCase> BuildBareToolCallTestCases()
{
    std::vector<BareToolCallSelfTestCase> c;

    c.push_back({"think_prefix_then_bare_json",
        "<think>...</think> 思考前缀之后紧跟裸 JSON（qwen3 类模型典型输出形态）——剥掉 "
        "think 前缀后应能检测出 exec 工具调用",
        R"(<think>Let me plan this out in detail before acting, considering all the file paths and options carefully.</think>{"name": "exec", "arguments": {"command": "ls -la"}})",
        true, "exec"});

    c.push_back({"fenced_json_block_with_lang",
        "```json 代码围栏包裹的裸 JSON——应能检测出 read 工具调用",
        "```json\n{\"name\": \"read\", \"arguments\": {\"path\": \"a.txt\"}}\n```",
        true, "read"});

    c.push_back({"fenced_json_block_no_lang_tag",
        "``` 代码围栏（无 json 语言标记）包裹的裸 JSON——应能检测出 write 工具调用",
        "```\n{\"name\": \"write\", \"arguments\": {\"path\": \"x.txt\", \"content\": \"hi\"}}\n```",
        true, "write"});

    c.push_back({"short_preamble_then_json",
        "简短前置说明文字（\"我将执行:\"）之后紧跟裸 JSON——前置文字很短，应仍能检测出",
        R"(I'll run this now:
{"name": "exec", "arguments": {"command": "ls"}})",
        true, "exec"});

    c.push_back({"broken_escaped_quote_real_sample",
        "真机复现样本：命令参数里三个连续转义引号导致字符串未正常闭合（截断类特征），"
        "TryParseLayer1Candidate 的补齐右括号/右引号启发式应能恢复出 exec 工具调用",
        R"({"name": "exec", "arguments": {"command": "python -c \"import onnx; m = onnx.load('x.onnx'); print(m.graph.input); \"\"\"}}})",
        true, "exec"});

    c.push_back({"ordinary_json_answer_not_known_tool",
        "普通数据 JSON（name 是人名，不是任何已知工具，也没有 arguments 字段）——不应被"
        "误判为工具调用意图",
        R"({"name": "Alice", "age": 3})",
        false, ""});

    c.push_back({"known_tool_name_but_no_arguments_field",
        "name 恰好与已知工具 \"read\" 同名，但完全没有 arguments/params/parameters/args"
        "（或 function.arguments）字段——单独出现已知工具名不足以判定为工具调用意图",
        R"({"name": "read"})",
        false, ""});

    c.push_back({"unknown_tool_name_with_args_field",
        "有 arguments 字段但工具名是幻觉出的不存在工具——不应被判定为工具调用",
        R"({"name": "delete_universe", "arguments": {"target": "x"}})",
        false, ""});

    c.push_back({"json_embedded_in_long_explanation",
        "候选 JSON 嵌在一大段说明文字中间（前后说明文字远超候选本身长度）——应判定为"
        "普通回答里的 JSON 示例，不予转换",
        R"(Let me explain how this tool call format generally works in this kind of system, just as background information before we continue our conversation about the weather and other unrelated topics today. For example purposes only, something like )"
        R"({"name": "read", "arguments": {"path": "a.txt"}})"
        R"( might appear in some documentation, but that is not what I am asking you to execute right now, this is purely illustrative text meant to explain the general shape of such calls in various software systems and frameworks that people build.)",
        false, ""});

    c.push_back({"no_json_at_all",
        "完全没有花括号的纯文本回答——应快速判定为不命中",
        R"(Sure, I can help you with that. Let me think about the best approach and get back to you shortly.)",
        false, ""});

    return c;
}

// ── Layer -1 流式 hold-back（ApplyBareJsonHoldBack）分块自测用例 ──────────────────
// 与上面 BareToolCallSelfTestCase 的区别：上面测的是"生成结束后对完整文本做一次性判定"
// （DetectBareToolCall），这里测的是"生成过程中逐 chunk 到达时的状态机行为"
// （ApplyBareJsonHoldBack），是两条独立代码路径——真机复现的边界 bug（"</think>" 独占
// 一个 chunk）只会在这里被捕获，DetectBareToolCall 的自测覆盖不到它。
struct HoldBackStreamingSelfTestCase
{
    std::string id;
    std::string description;
    std::vector<std::string> chunks;      // 模拟 genie_callback 依次收到的 message 片段
    bool expect_hold;                     // 全部 chunk 处理完毕后，是否应处于 bare_json_hold 状态
    std::string expected_visible_sent;    // 期望"实际发给客户端"的可见内容（各 chunk 处理后拼接）
    std::string expected_held_visible;    // 仅在 expect_hold=true 时有意义
};

std::vector<HoldBackStreamingSelfTestCase> BuildHoldBackStreamingTestCases()
{
    std::vector<HoldBackStreamingSelfTestCase> c;

    // 真机复现的确切 bug 场景："</think>" 独占一个 chunk（token-by-token 流式推理下
    // 的常见边界情形），裸 JSON 分散在后续多个 chunk 里到达。修复前 post_think_judged
    // 状态位不存在，这类场景会在 "</think>" 独占 chunk 时被误判为"已判定不像裸 JSON"，
    // 导致后续所有裸 JSON 片段都被当作正常内容直接发给客户端。
    c.push_back({"chunk_boundary_think_alone_then_bare_json",
        "</think> 独占一个 chunk，裸 JSON 分散在后续多个 chunk——真机复现的确切 bug 场景",
        {"<think>", "Okay let me think about which tool to call here.", "</think>",
         "{\"", "name", "\":\"exec\",\"arguments\":{\"command\":\"date\"}}"},
        true,
        "<think>Okay let me think about which tool to call here.</think>",
        "{\"name\":\"exec\",\"arguments\":{\"command\":\"date\"}}"});

    // 同一边界场景，但 </think> 之后是普通文本而非裸 JSON——验证"推迟到下一个 chunk
    // 才判定"这一修复本身不会把普通回答误判为 hold。
    c.push_back({"chunk_boundary_think_alone_then_plain_text",
        "</think> 独占一个 chunk，但后续内容是普通文本——不应进入 hold",
        {"<think>", "reasoning content", "</think>", "The answer is 42."},
        false,
        "<think>reasoning content</think>The answer is 42.",
        ""});

    // 回归防护：</think> 与 post-think 裸 JSON 同在一个 chunk 内（本次修复之前唯一
    // 能正常工作的场景），确认状态机重构后该场景没有被破坏。
    c.push_back({"same_chunk_think_and_bare_json",
        "</think> 与裸 JSON 同在一个 chunk 内——回归防护，确认修复前本就能 work 的场景未被破坏",
        {"<think>reasoning</think>{\"name\":\"exec\",\"arguments\":{\"command\":\"date\"}}"},
        true,
        "<think>reasoning</think>",
        "{\"name\":\"exec\",\"arguments\":{\"command\":\"date\"}}"});

    // 回归防护：</think> 与普通文本同在一个 chunk 内，不应 hold。
    c.push_back({"same_chunk_think_and_plain_text",
        "</think> 与普通文本同在一个 chunk 内——回归防护，不应 hold",
        {"<think>reasoning</think>The answer is 42."},
        false,
        "<think>reasoning</think>The answer is 42.",
        ""});

    return c;
}

} // namespace

bool RunBareToolCallDetectionSelfTest(std::ostream &out)
{
    std::vector<BareToolCallSelfTestCase> cases = BuildBareToolCallTestCases();
    int total = static_cast<int>(cases.size());
    int correct = 0;

    out << "================ Bare Tool Call Detection (Layer -1) Self-Test ================\n";

    for (const auto &tc : cases)
    {
        std::string visible_text = StripThinkPrefixForTest(tc.raw_input);
        std::string wrapped;
        bool actual_detect = ResponseTools::DetectBareToolCall(visible_text, wrapped);

        bool case_correct = false;
        std::string verdict_detail;

        if (tc.expect_detect)
        {
            if (!actual_detect)
            {
                verdict_detail = "expected detect=true but got false";
            }
            else
            {
                // DetectBareToolCall 命中时返回的是"包装后的原始候选文本"（未经修复，
                // 设计上交由下游 convertToolCallJson()->Layer1/2/3 链路统一修复），不保证
                // 自身即是合法 JSON（如 broken_escaped_quote_real_sample 用例）；必须像
                // 生产环境实际调用链那样先过一遍 convertToolCallJson() 再解析，才能安全
                // 提取 "name" 字段——这与 RunLayer1RecoverySelfTest（上方 410-411 行）
                // 的既有用法一致。
                //
                // 工具名可能来自两个不同的通道，取决于 TryLayer1Recovery 内部走的具体
                // 分支（见 response_tools.cpp:1536-1567 kTruncated/kUnparseable 分支的
                // 正则救援注释）：结构化恢复成功时，名字在直接返回结果的 root["name"]
                // 里；仅剩正则救援成功（结构解析仍失败，如本样本的嵌套转义错误）时，
                // 直接返回结果会退化为 root["name"]="unknow"，救援出的名字只会出现在
                // out_partial_recovery["name"]——这正是 Layer3 情形A（"识别出工具名，
                // 即使参数不全"）实际消费的通道，不是 bug，测试需要同时检查这两个通道，
                // 否则会误判为"检测失败"。
                ToolCallFailureReason reason = ToolCallFailureReason::kNone;
                json partial_recovery;
                std::string recovered = ResponseTools::convertToolCallJson(wrapped, &reason, &partial_recovery);
                json parsed = ExtractResultJson(recovered);
                std::string actual_name = parsed.value("name", "");
                if ((actual_name.empty() || actual_name == "unknow") && partial_recovery.is_object())
                {
                    actual_name = partial_recovery.value("name", "");
                }
                case_correct = (actual_name == tc.expected_tool_name);
                if (!case_correct)
                {
                    verdict_detail = "detected but wrong tool name (actual=" + actual_name + ")";
                }
            }
        }
        else
        {
            case_correct = !actual_detect;
            if (!case_correct)
            {
                verdict_detail = "expected detect=false but got true (wrapped=" + wrapped + ")";
            }
        }

        if (case_correct)
        {
            ++correct;
        }
        out << "[" << (case_correct ? "PASS" : "FAIL") << "] " << tc.id << " -- " << tc.description << "\n";
        if (!case_correct)
        {
            out << "       " << verdict_detail << "\n";
        }
    }

    out << "=================================================================================\n";
    out << "Total cases: " << total << ", correct: " << correct << "/" << total << "\n";
    out << "=================================================================================\n";

    return correct == total;
}

bool RunBareJsonHoldBackStreamingSelfTest(std::ostream &out)
{
    std::vector<HoldBackStreamingSelfTestCase> cases = BuildHoldBackStreamingTestCases();
    int total = static_cast<int>(cases.size());
    int correct = 0;

    out << "============== Bare JSON Hold-Back Streaming Self-Test (Layer -1) ==============\n";

    for (const auto &tc : cases)
    {
        ResponseTools::BareJsonHoldState state;
        std::string response_buffer;
        std::string visible_sent;

        for (const auto &msg : tc.chunks)
        {
            response_buffer += msg;
            std::string chunk = msg;  // 模拟 preprocessStream 未做任何过滤（outputChunk == message）
            ResponseTools::ApplyBareJsonHoldBack(state, response_buffer, msg, chunk);
            visible_sent += chunk;
        }

        bool case_correct = (state.bare_json_hold == tc.expect_hold) &&
                             (visible_sent == tc.expected_visible_sent);
        if (case_correct && tc.expect_hold)
        {
            case_correct = (state.held_visible == tc.expected_held_visible);
        }

        out << "[" << (case_correct ? "PASS" : "FAIL") << "] " << tc.id << " -- " << tc.description << "\n";
        if (!case_correct)
        {
            out << "       expected: hold=" << tc.expect_hold << " visible_sent=\"" << tc.expected_visible_sent
                << "\" held_visible=\"" << tc.expected_held_visible << "\"\n";
            out << "       actual:   hold=" << state.bare_json_hold << " visible_sent=\"" << visible_sent
                << "\" held_visible=\"" << state.held_visible << "\"\n";
        }

        if (case_correct)
        {
            ++correct;
        }
    }

    out << "=================================================================================\n";
    out << "Total cases: " << total << ", correct: " << correct << "/" << total << "\n";
    out << "=================================================================================\n";

    return correct == total;
}

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

    double success_rate = 100.0;
    if (expect_success_count > 0)
    {
        const int success_denom = expect_success_count;
        success_rate = 100.0 * expect_success_correct / success_denom;
    }
    double reason_accuracy = 100.0;
    if (expect_failure_count > 0)
    {
        const int failure_denom = expect_failure_count;
        reason_accuracy = 100.0 * expect_failure_correct / failure_denom;
    }
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
