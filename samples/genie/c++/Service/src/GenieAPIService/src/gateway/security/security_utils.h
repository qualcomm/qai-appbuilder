//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include "log.h"
#include "../../context/context_base.h"
#include <string>
#include <vector>
#include <memory>
#include <thread>
#include <future>
#include <atomic>
#include <chrono>
#include <cctype>
#include <algorithm>

namespace SecurityUtils {

inline std::string ExtractMessageContentText(const json& msg)
{
    if (!msg.contains("content")) return "";
    const auto& content = msg["content"];
    if (content.is_string()) return content.get<std::string>();
    if (!content.is_array()) return "";

    std::string text;
    for (const auto& part : content) {
        if (part.is_object() && part.value("type", "") == "text" &&
            part.contains("text") && part["text"].is_string()) {
            text += part["text"].get<std::string>();
        }
    }
    return text;
}

inline uint64_t FNV1aHash(const std::string &s)
{
    uint64_t hash = 14695981039346656037ULL;
    for (unsigned char c : s)
    {
        hash ^= static_cast<uint64_t>(c);
        hash *= 1099511628211ULL;
    }
    return hash;
}

inline std::string StripUntrustedMetadataBlocks(const std::string& text)
{
    static const std::string kHeader = "Sender (untrusted metadata):";
    if (text.find(kHeader) == std::string::npos) return text;

    std::string result;
    result.reserve(text.size());
    size_t pos = 0;
    const size_t len = text.size();

    while (pos < len) {
        size_t header_pos = text.find(kHeader, pos);
        if (header_pos == std::string::npos) {
            result.append(text, pos, len - pos);
            break;
        }
        if (header_pos > pos) {
            result.append(text, pos, header_pos - pos);
        }
        size_t line_end = text.find('\n', header_pos);
        if (line_end == std::string::npos) break;
        pos = line_end + 1;

        while (pos < len && (text[pos] == ' ' || text[pos] == '\t' ||
                              text[pos] == '\r' || text[pos] == '\n')) {
            ++pos;
        }
        if (pos < len && text.compare(pos, 3, "```") == 0) {
            size_t start_line_end = text.find('\n', pos);
            if (start_line_end == std::string::npos) break;
            pos = start_line_end + 1;
            size_t close_ticks = text.find("```", pos);
            if (close_ticks == std::string::npos) break;
            size_t close_line_end = text.find('\n', close_ticks);
            pos = (close_line_end != std::string::npos) ? close_line_end + 1 : len;
        }
    }

    size_t trim_start = result.find_first_not_of(" \t\r\n");
    if (trim_start == std::string::npos) return "";
    size_t trim_end = result.find_last_not_of(" \t\r\n");
    return result.substr(trim_start, trim_end - trim_start + 1);
}

inline std::string StripOpenClawTimestampPrefix(const std::string& text)
{
    if (text.empty() || text[0] != '[') return text;

    size_t close_bracket = text.find(']');
    if (close_bracket == std::string::npos) return text;

    if (close_bracket >= 10 && close_bracket <= 40) {
        std::string bracket_content = text.substr(1, close_bracket - 1);
        bool has_year = false;
        for (size_t i = 0; i + 3 < bracket_content.size(); ++i) {
            if (std::isdigit(static_cast<unsigned char>(bracket_content[i])) &&
                std::isdigit(static_cast<unsigned char>(bracket_content[i+1])) &&
                std::isdigit(static_cast<unsigned char>(bracket_content[i+2])) &&
                std::isdigit(static_cast<unsigned char>(bracket_content[i+3]))) {
                has_year = true;
                break;
            }
        }
        if (has_year) {
            size_t content_start = close_bracket + 1;
            while (content_start < text.size() && text[content_start] == ' ')
                ++content_start;
            return text.substr(content_start);
        }
    }
    return text;
}

inline std::string TrimWhitespace(const std::string& text)
{
    size_t ts = text.find_first_not_of(" \t\r\n");
    if (ts == std::string::npos) return "";
    size_t te = text.find_last_not_of(" \t\r\n");
    return text.substr(ts, te - ts + 1);
}

inline std::string CleanMessageText(const std::string& raw_text)
{
    std::string text = StripUntrustedMetadataBlocks(raw_text);
    text = StripOpenClawTimestampPrefix(text);
    return TrimWhitespace(text);
}

inline bool CheckPrefixEarlyStop(
        const std::string& output_buf,
        const std::string& json_key_prefix,
        const std::vector<std::string>& valid_values,
        std::string& completed_json,
        std::string& matched_value)
{
    if (output_buf.size() <= json_key_prefix.size()) return false;

    size_t prefix_pos = output_buf.find(json_key_prefix);
    if (prefix_pos == std::string::npos) return false;

    size_t val_start = prefix_pos + json_key_prefix.size();

    for (const auto& val : valid_values) {
        if (val_start + val.size() > output_buf.size()) continue;
        if (output_buf.substr(val_start, val.size()) != val) continue;

        size_t after_val = val_start + val.size();
        if (after_val >= output_buf.size()) continue;

        char next_char = output_buf[after_val];
        if (!std::isalpha(static_cast<unsigned char>(next_char))) {

            std::string key_part = json_key_prefix;

            if (key_part.size() >= 2 && key_part[0] == '{' && key_part[1] == '"')
                key_part = key_part.substr(2);

            if (key_part.size() >= 3 && key_part.substr(key_part.size() - 3) == "\":\"")
                key_part = key_part.substr(0, key_part.size() - 3);

            completed_json = "{\"" + key_part + "\":\"" + val + "\"}";
            matched_value = val;
            return true;
        }
    }
    return false;
}

struct LocalModelQueryResult {
    bool success = false;
    std::string output;
};

template<typename TokenCallback>
inline LocalModelQueryResult LocalModelQuery(
        std::shared_ptr<ContextBase> handle,
        const std::string& prompt,
        const std::string& json_prefill,
        int timeout_ms,
        int max_gen_tokens,
        TokenCallback on_token,
        const std::string& logger_prefix)
{
    auto model_output_ptr = std::make_shared<std::string>();
    auto promise_ptr = std::make_shared<std::promise<bool>>();
    auto promise_fulfilled = std::make_shared<std::atomic<bool>>(false);
    auto cancelled = std::make_shared<std::atomic<bool>>(false);
    auto future = promise_ptr->get_future();

    std::thread worker([handle, prompt, json_prefill, model_output_ptr,
                        promise_ptr, promise_fulfilled, cancelled,
                        max_gen_tokens, on_token, logger_prefix]() mutable {
        ModelInput input;
        input.text_ = prompt;

        std::string output_buf = json_prefill;
        int token_count = 0;
        bool json_complete = false;

        bool ok = handle->Query(input, [&](std::string& token) -> bool {
            if (cancelled->load(std::memory_order_relaxed)) return false;

            output_buf += token;
            token_count++;

            bool should_continue = on_token(output_buf, token, json_complete);
            if (!should_continue) return false;

            if (max_gen_tokens > 0 && token_count >= max_gen_tokens) {
                My_Log{My_Log::Level::kWarning}
                    << logger_prefix << " [STREAM] Hard limit reached ("
                    << max_gen_tokens << " tokens). Force stop. buf=\""
                    << output_buf.substr(0, 200) << "\"\n";
                return false;
            }
            return true;
        });

        if (!cancelled->load(std::memory_order_relaxed) && !output_buf.empty())
        {
            *model_output_ptr = output_buf;
        }

        My_Log{} << logger_prefix << " [STREAM] Worker done: ok=" << ok
                 << ", json_complete=" << json_complete
                 << ", output_len=" << output_buf.size()
                 << ", full_output=\"" << output_buf << "\"\n";

        if (!promise_fulfilled->exchange(true))
        {
            try { promise_ptr->set_value(ok); }
            catch (...) {}
        }
    });

    auto status = future.wait_for(std::chrono::milliseconds(timeout_ms));
    if (status != std::future_status::ready)
    {
        cancelled->store(true, std::memory_order_relaxed);
        promise_fulfilled->store(true);
        worker.detach();
        My_Log{My_Log::Level::kError}
            << logger_prefix << " LocalModel timeout ("
            << timeout_ms << "ms), using conservative fallback" << std::endl;
        return {false, ""};
    }

    worker.join();
    bool query_success = future.get();
    if (!query_success || model_output_ptr->empty())
    {
        return {false, ""};
    }

    return {true, *model_output_ptr};
}

}
