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

constexpr size_t kMaxHighSignalLines = 10;

constexpr size_t kMinViableChars = 200;

}

std::vector<std::string> ContentCondenser::ExtractHighSignalLines(const std::string& text, size_t max_lines)
{
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

    double effective_bytes_per_token = EstimateCjkAwareBytesPerToken(
        content, budget.cjk_bytes_per_token, budget.ascii_bytes_per_token);
    size_t char_budget = budget.max_chars;
    if (char_budget == 0 && budget.max_tokens > 0) {
        char_budget = static_cast<size_t>(static_cast<double>(budget.max_tokens) * effective_bytes_per_token);
    }

    if (char_budget == 0 || content.length() <= char_budget) {
        return result;
    }

    result.truncated = true;

    if (char_budget < kMinViableChars) {
        result.text = safe_utf8_truncate(content, char_budget, "...[truncated]");
        result.omitted_chars = content.length() - result.text.length();
        return result;
    }

    bool json_handled = false;
    if (kind != ContentKind::kGenericText) {
        try {
            nlohmann::json parsed = nlohmann::json::parse(content);
            if (parsed.is_array() && parsed.size() > (budget.json_head_items + budget.json_tail_items)) {
                result.text = CondenseJsonArray(parsed, budget.json_head_items, budget.json_tail_items, char_budget);
                json_handled = true;
            }
        } catch (...) {
        }
    }

    if (!json_handled) {
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
        if (remaining < kMinViableChars / 2) {
            high_signal_block.clear();
            hs_len = 0;
            remaining = char_budget;
        }

        size_t tail_chars = static_cast<size_t>(static_cast<double>(remaining) * budget.tail_ratio);
        size_t head_chars = (remaining > tail_chars) ? (remaining - tail_chars) : 0;

        result.text = high_signal_block + HeadTailKeep(content, head_chars, tail_chars);
    }

    if (token_len != nullptr && budget.max_tokens > 0) {
        size_t probes = 0;
        size_t cur_tokens = (*token_len)(result.text);
        while (cur_tokens > budget.max_tokens && probes < budget.max_token_probe) {
            probes++;
            double scale = static_cast<double>(budget.max_tokens) / static_cast<double>(cur_tokens) * 0.95;
            size_t new_len = static_cast<size_t>(static_cast<double>(result.text.length()) * scale);
            if (new_len < kMinViableChars) new_len = kMinViableChars;
            if (new_len >= result.text.length()) break;
            result.text = safe_utf8_truncate(result.text, new_len, "...[truncated]");
            cur_tokens = (*token_len)(result.text);
        }
        if (cur_tokens > budget.max_tokens) {
            size_t hard_chars = static_cast<size_t>(
                static_cast<double>(budget.max_tokens) * effective_bytes_per_token * 0.75);
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
