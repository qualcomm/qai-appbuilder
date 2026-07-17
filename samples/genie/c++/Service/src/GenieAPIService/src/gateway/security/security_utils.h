//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// security_utils.h
//
// 安全检查与复杂度评估共用工具函数：
//   - FNV-1a 哈希
//   - OpenClaw 元数据块过滤（strip_untrusted_metadata_blocks）
//   - OpenClaw 时间戳前缀过滤（strip_openclaw_timestamp_prefix）
//   - 本地模型推理框架（LocalModelQuery：std::thread + std::promise 超时控制）
//   - 前缀匹配早停（CheckPrefixEarlyStop）
//
// 使用方：
//   - content_security_inspector.cpp
//   - task_complexity_evaluator.cpp
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

// ============================================================
// FNV-1a 哈希（64位）
// 用于缓存 key 计算，避免引入额外依赖
// ============================================================
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

// ============================================================
// 过滤 OpenClaw 注入的 "Sender (untrusted metadata):" JSON 块
//
// 这类元数据块（如 {"label":"openclaw-control-ui","id":"openclaw-control-ui"}）
// 不含任何真正的敏感信息，但会导致小参数量模型（如 7B QNN）产生误判。
// 过滤后仅保留用户实际输入的文本内容。
//
// 格式示例：
//   Sender (untrusted metadata):
//   ```json
//   {
//     "label": "openclaw-control-ui",
//     "id": "openclaw-control-ui"
//   }
//   ```
//
// 返回：过滤并 trim 后的文本
// ============================================================
inline std::string StripUntrustedMetadataBlocks(const std::string& text)
{
    static const std::string kHeader = "Sender (untrusted metadata):";
    if (text.find(kHeader) == std::string::npos) return text;  // 快速路径：无元数据块

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

    // trim 首尾空白
    size_t trim_start = result.find_first_not_of(" \t\r\n");
    if (trim_start == std::string::npos) return "";
    size_t trim_end = result.find_last_not_of(" \t\r\n");
    return result.substr(trim_start, trim_end - trim_start + 1);
}

// ============================================================
// 过滤 OpenClaw 注入的时间戳前缀
//
// 格式：[Mon|Tue|Wed|Thu|Fri|Sat|Sun YYYY-MM-DD HH:MM GMT±N]
// 示例：[Wed 2026-03-25 20:13 GMT+8] hello → hello
//
// QNN 小模型（7B）看到方括号 "[" 开头时，会将其当作代码/数组语法续写，
// 导致 JSON 解析失败并触发保守升级。
// ============================================================
inline std::string StripOpenClawTimestampPrefix(const std::string& text)
{
    if (text.empty() || text[0] != '[') return text;

    size_t close_bracket = text.find(']');
    if (close_bracket == std::string::npos) return text;

    // 验证方括号内是否是时间戳格式（长度合理，含4位年份）
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

// ============================================================
// 对字符串进行首尾 trim（去除空白字符）
// ============================================================
inline std::string TrimWhitespace(const std::string& text)
{
    size_t ts = text.find_first_not_of(" \t\r\n");
    if (ts == std::string::npos) return "";
    size_t te = text.find_last_not_of(" \t\r\n");
    return text.substr(ts, te - ts + 1);
}

// ============================================================
// 对消息文本进行完整清洗：
//   步骤1：过滤 "Sender (untrusted metadata):" JSON 块（含 trim）
//   步骤2：过滤 OpenClaw 时间戳前缀
//   步骤3：再次 trim
// ============================================================
inline std::string CleanMessageText(const std::string& raw_text)
{
    std::string text = StripUntrustedMetadataBlocks(raw_text);
    text = StripOpenClawTimestampPrefix(text);
    return TrimWhitespace(text);
}

// ============================================================
// 前缀匹配早停检查
//
// 当 output_buf 中检测到 json_key_prefix（如 `{"sensitivity":"` 或 `{"complexity":"`）
// 后紧跟 valid_values 中的某个值，且该值后面跟着非字母字符时，
// 说明模型已输出了有效的字段值，可以提前停止生成。
//
// 参数：
//   output_buf    - 当前已生成的输出缓冲区
//   json_key_prefix - JSON 键前缀，如 `{"sensitivity":"` 或 `{"complexity":"`
//   valid_values  - 有效值列表，如 {"S0","S1","S2"} 或 {"C0","C1","C2"}
//   completed_json - [out] 若匹配成功，填充补全后的 JSON 字符串
//   matched_value  - [out] 若匹配成功，填充匹配到的值
//
// 返回：true 表示匹配成功，应立即停止生成
// ============================================================
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

        // 确认值后面有非字母字符（说明值已完整）
        size_t after_val = val_start + val.size();
        if (after_val >= output_buf.size()) continue;  // 还没有后续字符，继续等待

        char next_char = output_buf[after_val];
        if (!std::isalpha(static_cast<unsigned char>(next_char))) {
            // 补全 JSON：{"key":"val"}
            // 从 json_key_prefix 中提取 key 名（去掉 `{"` 前缀和 `":"` 后缀）
            // json_key_prefix 格式：{"key":"
            std::string key_part = json_key_prefix;
            // 去掉开头的 `{"`
            if (key_part.size() >= 2 && key_part[0] == '{' && key_part[1] == '"')
                key_part = key_part.substr(2);
            // 去掉结尾的 `":"`
            if (key_part.size() >= 3 && key_part.substr(key_part.size() - 3) == "\":\"")
                key_part = key_part.substr(0, key_part.size() - 3);

            completed_json = "{\"" + key_part + "\":\"" + val + "\"}";
            matched_value = val;
            return true;
        }
    }
    return false;
}

// ============================================================
// 本地模型推理框架（带超时控制）
//
// 封装 std::thread + std::promise 模式，实现真正的非阻塞超时控制。
// 调用方提供：
//   - handle：模型句柄
//   - prompt：输入 prompt
//   - json_prefill：output_buf 初始前缀（如 `{"sensitivity":"` 或 `{"complexity":"`）
//   - timeout_ms：超时时间（毫秒）
//   - max_gen_tokens：最大生成 token 数（硬上限）
//   - on_token：每个 token 的回调，返回 false 时停止生成
//     签名：bool on_token(std::string& output_buf, const std::string& token, bool& json_complete)
//   - logger_prefix：日志前缀（如 "[ContentSecurityInspector]" 或 "[TaskComplexityEvaluator]"）
//
// 返回：{success, model_output}
//   success=true 且 model_output 非空时表示推理成功
// ============================================================
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

            // 调用方提供的 token 回调（负责早停检测）
            bool should_continue = on_token(output_buf, token, json_complete);
            if (!should_continue) return false;

            // 硬上限：超过 max_gen_tokens 时强制停止
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

} // namespace SecurityUtils
