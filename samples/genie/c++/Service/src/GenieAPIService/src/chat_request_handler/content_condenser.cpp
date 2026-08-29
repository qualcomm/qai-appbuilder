//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "content_condenser.h"

#include <utils.h>
#include <regex>
#include <sstream>
#include <unordered_set>

namespace {

// JSON 数组保留的首/尾项数由 CondenseBudget::json_head_items/json_tail_items 配置化
// （Step 4），默认值 3/1 已在 CondenseBudget 结构体中声明。

// 高信号行最大保留条数
constexpr size_t kMaxHighSignalLines = 10;

// 预算过小的下限阈值（字符数）：低于此值时，省略标记与高信号行本身的固定开销会
// 挤掉正文，退化为纯头部保留（不做高信号提取、不做尾部保留）。
constexpr size_t kMinViableChars = 200;

}  // namespace

std::vector<std::string> ContentCondenser::ExtractHighSignalLines(const std::string& text, size_t max_lines)
{
    // 覆盖：英文关键词（大小写不敏感）、中文「错误/失败/异常」、`path:line` 形式的路径行、
    // 独立数字行（纯数字，常见于退出码/计数汇总）
    static const std::regex kHighSignalPattern(
        R"(error|exception|failed|fatal|traceback|exit code|warning|错误|失败|异常|[\w./\\-]+:\d+|^\s*-?\d+\s*$)",
        std::regex::icase);

    std::vector<std::string> result;
    std::unordered_set<std::string> seen;
    std::istringstream iss(text);
    std::string line;
    while (result.size() < max_lines && std::getline(iss, line)) {
        if (line.empty()) continue;
        if (!std::regex_search(line, kHighSignalPattern)) continue;
        if (seen.insert(line).second) {
            result.push_back(line);
        }
    }
    return result;
}

std::string ContentCondenser::HeadTailKeep(const std::string& text, size_t head_chars, size_t tail_chars)
{
    size_t len = text.length();
    if (head_chars + tail_chars >= len) {
        return text;
    }

    // ── 头部：优先在段落（\n\n）边界切，其次行（\n）边界，最后 UTF-8 安全兜底 ──
    std::string head_part;
    if (head_chars > 0) {
        size_t boundary = text.rfind("\n\n", head_chars);
        if (boundary == std::string::npos || boundary < head_chars / 2) {
            boundary = text.rfind('\n', head_chars);
        }
        if (boundary != std::string::npos && boundary >= head_chars / 2) {
            head_part = text.substr(0, boundary);
        } else {
            head_part = safe_utf8_truncate(text, head_chars, "");
        }
    }

    // ── 尾部：先对齐到合法 UTF-8 起始字节，再尽量对齐到行首，避免从行中间开始 ──
    std::string tail_part;
    if (tail_chars > 0) {
        size_t tail_start = (len > tail_chars) ? (len - tail_chars) : 0;
        tail_start = utf8_align_start(text, tail_start);
        size_t nl = text.find('\n', tail_start);
        if (nl != std::string::npos && nl < tail_start + tail_chars / 2) {
            tail_start = nl + 1;
        }
        tail_part = text.substr(tail_start);
    }

    size_t omitted = (len > head_part.length() + tail_part.length())
                          ? (len - head_part.length() - tail_part.length())
                          : 0;
    return head_part + "\n...[" + std::to_string(omitted) + " chars omitted]...\n" + tail_part;
}

std::string ContentCondenser::CondenseJsonArray(const nlohmann::json& arr, size_t head_n, size_t tail_n, size_t max_chars)
{
    size_t n = arr.size();
    if (n == 0) return "[]";

    // 数组本身不够长，无需省略；调用方在外部已经按 (head_n + tail_n) 判断过 size，
    // 这里是兜底（例如被直接调用而未经过该判断）。
    if (n <= head_n + tail_n) {
        std::string dumped = arr.dump();
        return (dumped.length() <= max_chars) ? dumped : safe_utf8_truncate(dumped, max_chars, "...[JSON truncated]");
    }

    size_t omitted = n - head_n - tail_n;
    std::ostringstream oss;
    oss << "[";
    bool wrote_any = false;
    for (size_t i = 0; i < head_n; ++i) {
        if (wrote_any) oss << ",";
        oss << arr[i].dump();
        wrote_any = true;
    }
    if (wrote_any) oss << ",";
    oss << "\"[" << omitted << " items omitted]\"";
    for (size_t i = n - tail_n; i < n; ++i) {
        oss << "," << arr[i].dump();
    }
    oss << "]";

    std::string result = oss.str();
    return (result.length() <= max_chars) ? result : safe_utf8_truncate(result, max_chars, "...[JSON truncated]");
}

CondenseResult ContentCondenser::Condense(const std::string& content,
                                           ContentKind kind,
                                           const CondenseBudget& budget,
                                           const std::function<size_t(const std::string&)>* token_len)
{
    CondenseResult result;
    result.text = content;

    // 字符预算：max_chars 优先；仅当未设置字符预算但给了 token 预算时才粗略换算
    // （1 token ≈ 4 字符，保守估算，避免砍太狠）。
    size_t char_budget = budget.max_chars;
    if (char_budget == 0 && budget.max_tokens > 0) {
        char_budget = budget.max_tokens * 4;
    }

    // 步骤 1：长度快筛，命中即原样返回，不调 tokenizer
    if (char_budget == 0 || content.length() <= char_budget) {
        return result;
    }

    result.truncated = true;

    // 步骤 7：预算过小 → 退化为纯头部保留，避免省略标记/高信号行块本身挤掉正文
    if (char_budget < kMinViableChars) {
        result.text = safe_utf8_truncate(content, char_budget, "...[truncated]");
        result.omitted_chars = content.length() - result.text.length();
        return result;
    }

    // 步骤 2：JSON 数组感知截断（仅 kToolResponse/kJsonPayload 尝试；kGenericText 从不
    // 尝试 JSON 解析，与旧 truncate_content 行为一致，避免对普通聊天文本做无意义解析）
    bool json_handled = false;
    if (kind != ContentKind::kGenericText) {
        try {
            nlohmann::json parsed = nlohmann::json::parse(content);
            if (parsed.is_array() && parsed.size() > (budget.json_head_items + budget.json_tail_items)) {
                result.text = CondenseJsonArray(parsed, budget.json_head_items, budget.json_tail_items, char_budget);
                json_handled = true;
            }
        } catch (...) {
            // 非法/截断的 JSON 或非 JSON 内容：走通用文本路径，不抛异常
        }
    }

    if (!json_handled) {
        // 步骤 3：提取高信号行，置于保留内容最前面
        std::string high_signal_block;
        if (budget.extract_high_signal) {
            auto lines = ExtractHighSignalLines(content, kMaxHighSignalLines);
            if (!lines.empty()) {
                std::ostringstream oss;
                oss << "[Key lines]\n";
                for (const auto& l : lines) oss << l << "\n";
                high_signal_block = oss.str();
                result.high_signal_lines = lines.size();
            }
        }

        size_t hs_len = high_signal_block.length();
        size_t remaining = (char_budget > hs_len) ? (char_budget - hs_len) : 0;
        // 高信号行占用过多导致正文空间不足：放弃高信号行，全部额度让给正文
        if (remaining < kMinViableChars / 2) {
            high_signal_block.clear();
            hs_len = 0;
            remaining = char_budget;
        }

        // 步骤 4：剩余额度按 tail_ratio 拆成头/尾两段
        size_t tail_chars = static_cast<size_t>(static_cast<double>(remaining) * budget.tail_ratio);
        size_t head_chars = (remaining > tail_chars) ? (remaining - tail_chars) : 0;

        // 步骤 5：UTF-8 安全兜底由 HeadTailKeep 内部通过 safe_utf8_truncate/utf8_align_start 保证
        result.text = high_signal_block + HeadTailKeep(content, head_chars, tail_chars);
    }

    // 步骤 6：token 收敛（仅当提供 token_len 时生效；本步骤接入点恒定传 nullptr，跳过）
    if (token_len != nullptr && budget.max_tokens > 0) {
        size_t probes = 0;
        size_t cur_tokens = (*token_len)(result.text);
        while (cur_tokens > budget.max_tokens && probes < budget.max_token_probe) {
            probes++;
            double scale = static_cast<double>(budget.max_tokens) / static_cast<double>(cur_tokens) * 0.95;
            size_t new_len = static_cast<size_t>(static_cast<double>(result.text.length()) * scale);
            if (new_len < kMinViableChars) new_len = kMinViableChars;
            if (new_len >= result.text.length()) break;  // 无法继续收敛
            result.text = safe_utf8_truncate(result.text, new_len, "...[truncated]");
            cur_tokens = (*token_len)(result.text);
        }
        if (cur_tokens > budget.max_tokens) {
            // 探测次数用尽仍超预算：按更激进的字符估算（3 chars/token）硬截断，保证不越界
            size_t hard_chars = budget.max_tokens * 3;
            if (hard_chars < result.text.length()) {
                result.text = safe_utf8_truncate(result.text, hard_chars, "...[truncated]");
            }
        }
    }

    result.omitted_chars = (content.length() > result.text.length())
                                ? (content.length() - result.text.length())
                                : 0;

    return result;
}
