//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CONTENT_CONDENSER_H
#define CONTENT_CONDENSER_H

#include <nlohmann/json.hpp>
#include <cstddef>
#include <functional>
#include <string>
#include <vector>

// 内容分类：影响 Condense() 是否尝试 JSON 数组感知截断。
// kGenericText 从不尝试 JSON 解析（与旧 truncate_content 行为一致，避免对普通聊天文本
// 做无意义的 JSON 解析尝试）；kToolResponse/kJsonPayload 会先尝试按 JSON 数组处理。
enum class ContentKind {
    kGenericText,   // 普通文本（user/assistant 消息内容）
    kToolResponse,  // 工具响应（可能是 JSON，也可能是纯文本日志/报错）
    kJsonPayload    // 已知/期望是 JSON payload
};

// 截断预算。
// max_tokens=0（配合 token_len=nullptr）时退化为纯字符模式，不调用任何 tokenizer——
// 这是 budget_unit=="chars" 时的回退路径，也是消息侧 fidelity 特性关闭时的等价行为。
struct CondenseBudget {
    size_t max_tokens   = 0;      // 硬预算（0 = 退化为字符模式）
    size_t max_chars    = 0;      // 字符上限（快筛 / 回退）
    double tail_ratio   = 0.30;   // 尾部占保留额度的比例；0.0 等价于仅保留头部
    bool   extract_high_signal = true;
    size_t max_token_probe     = 8;   // 单条消息 tokenizer 调用次数上限
    size_t json_head_items     = 3;   // JSON 数组截断保留的首部项数
    size_t json_tail_items     = 1;   // JSON 数组截断保留的尾部项数
};

// 截断结果。
struct CondenseResult {
    std::string text;
    bool   truncated = false;
    size_t omitted_chars = 0;
    size_t high_signal_lines = 0;
};

// 保真截断算法：高信号行提取 + 头尾双保留，取代原先三处各自实现的"只保留前缀/仅保头弃尾"
// 策略。算法顺序、坑点见同名文档 content_condenser.md。
class ContentCondenser {
public:
    // token_len 为 nullptr 时纯字符模式，不会调用任何 tokenizer。
    static CondenseResult Condense(const std::string& content,
                                    ContentKind kind,
                                    const CondenseBudget& budget,
                                    const std::function<size_t(const std::string&)>* token_len);

private:
    // 提取高信号行（错误/异常/失败/退出码/traceback/路径行/独立数字行），去重，限最大条数
    static std::vector<std::string> ExtractHighSignalLines(const std::string& text, size_t max_lines);

    // 头尾双保留：优先在 \n\n -> \n 边界切，中部替换为省略标记
    static std::string HeadTailKeep(const std::string& text, size_t head_chars, size_t tail_chars);

    // JSON 数组保真截断：保留首 head_n + 尾 tail_n 项，中间插入 "[K items omitted]" 标记
    static std::string CondenseJsonArray(const nlohmann::json& arr, size_t head_n, size_t tail_n, size_t max_chars);
};

#endif // CONTENT_CONDENSER_H
