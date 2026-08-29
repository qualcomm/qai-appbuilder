//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PROMPT_LEDGER_H
#define PROMPT_LEDGER_H

#include <nlohmann/json.hpp>
#include <httplib.h>
#include <string>

using json = nlohmann::ordered_json;

// ============================================================
// PromptLedger：提示词压缩账本（可观测性核心）
//
// 目的：把「本轮压缩到底做了什么」回报给调用方（HTTP 响应头 / 流式 status 帧），
// 而不是只留在服务端日志里——调用方无需读日志即可知道本轮被丢了几条消息、
// 工具定义降到了第几档、技能目录是否被相关性过滤清空。
//
// 数据源：直接取自现有 MessagePreFilter::GetStats()、FitMessagesToContext() 的
// OptimizedMessages 返回值、PromptOptimizer::GetLastStats() 的降级档位/相关性
// 过滤结果——不新增第二套统计，本结构只是既有统计的一次机器可读汇总。
// ============================================================
struct PromptLedger {
    int    context_size = 0;
    size_t tokens_in = 0, tokens_out = 0;
    size_t messages_in = 0, messages_kept = 0;
    size_t messages_dropped = 0, messages_truncated = 0;
    int    tools_tier = 0;
    size_t tools_total = 0, tools_kept = 0;
    size_t skills_total = 0, skills_kept = 0;
    bool   emergency_truncated = false;
    bool   summarized = false;

    // 真实 JSON 类型（bool 用真实 bool，数字用真实数字），供流式 status 帧
    // （status="prompt_optimized"）payload 复用，与响应头字段口径完全一致。
    json ToJson() const
    {
        json j;
        j["context_size"] = context_size;
        j["tokens_in"] = tokens_in;
        j["tokens_out"] = tokens_out;
        j["messages_in"] = messages_in;
        j["messages_kept"] = messages_kept;
        j["messages_dropped"] = messages_dropped;
        j["messages_truncated"] = messages_truncated;
        j["tools_tier"] = tools_tier;
        j["tools_total"] = tools_total;
        j["tools_kept"] = tools_kept;
        j["skills_total"] = skills_total;
        j["skills_kept"] = skills_kept;
        j["emergency_truncated"] = emergency_truncated;
        j["summarized"] = summarized;
        return j;
    }

    // 非流式响应头：全部字段转字符串。bool 字段写 "0"/"1"（不是 "true"/"false"），
    // 与 test/test_service.py 的 _pf_check_ledger_headers()（int(str(raw).strip())）对齐。
    void WriteHeaders(httplib::Response &res) const
    {
        res.set_header("X-Genie-Prompt-Context-Size", std::to_string(context_size));
        res.set_header("X-Genie-Prompt-Tokens-In", std::to_string(tokens_in));
        res.set_header("X-Genie-Prompt-Tokens-Out", std::to_string(tokens_out));
        res.set_header("X-Genie-Prompt-Messages-In", std::to_string(messages_in));
        res.set_header("X-Genie-Prompt-Messages-Kept", std::to_string(messages_kept));
        res.set_header("X-Genie-Prompt-Messages-Dropped", std::to_string(messages_dropped));
        res.set_header("X-Genie-Prompt-Messages-Truncated", std::to_string(messages_truncated));
        res.set_header("X-Genie-Prompt-Tools-Total", std::to_string(tools_total));
        res.set_header("X-Genie-Prompt-Tools-Kept", std::to_string(tools_kept));
        res.set_header("X-Genie-Prompt-Tools-Tier", std::to_string(tools_tier));
        res.set_header("X-Genie-Prompt-Skills-Total", std::to_string(skills_total));
        res.set_header("X-Genie-Prompt-Skills-Kept", std::to_string(skills_kept));
        res.set_header("X-Genie-Prompt-Emergency-Truncated", emergency_truncated ? "1" : "0");
        res.set_header("X-Genie-Prompt-Summarized", summarized ? "1" : "0");
    }
};

#endif //PROMPT_LEDGER_H
