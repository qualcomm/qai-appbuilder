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

struct PromptLedger {
    int    context_size = 0;
    size_t tokens_in = 0, tokens_out = 0;
    size_t messages_in = 0, messages_kept = 0;
    size_t messages_dropped = 0, messages_truncated = 0;
    int    tools_tier = 0;
    size_t tools_total = 0, tools_kept = 0;
    size_t skills_total = 0, skills_kept = 0;

    size_t skills_budget_tokens = 0;

    size_t skills_l2 = 0, skills_l1 = 0, skills_l0 = 0;
    bool   emergency_truncated = false;
    bool   summarized = false;

    bool   memo_active = false;
    double memo_confidence = 0.0;
    size_t memo_refresh_count = 0;
    size_t memo_pages_total = 0;

    size_t messages_dropped_prefix_len = 0;
    std::string prefix_reset_reason = "none";

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
        j["skills_budget_tokens"] = skills_budget_tokens;
        j["skills_l2"] = skills_l2;
        j["skills_l1"] = skills_l1;
        j["skills_l0"] = skills_l0;
        j["emergency_truncated"] = emergency_truncated;
        j["summarized"] = summarized;
        j["memo_active"] = memo_active;
        j["memo_confidence"] = memo_confidence;
        j["memo_refresh_count"] = memo_refresh_count;
        j["memo_pages_total"] = memo_pages_total;
        j["messages_dropped_prefix_len"] = messages_dropped_prefix_len;
        j["prefix_reset_reason"] = prefix_reset_reason;
        return j;
    }

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
        res.set_header("X-Genie-Prompt-Skills-Budget-Tokens", std::to_string(skills_budget_tokens));
        res.set_header("X-Genie-Prompt-Skills-L2", std::to_string(skills_l2));
        res.set_header("X-Genie-Prompt-Skills-L1", std::to_string(skills_l1));
        res.set_header("X-Genie-Prompt-Skills-L0", std::to_string(skills_l0));
        res.set_header("X-Genie-Prompt-Emergency-Truncated", emergency_truncated ? "1" : "0");
        res.set_header("X-Genie-Prompt-Summarized", summarized ? "1" : "0");
        res.set_header("X-Genie-Prompt-Memo-Active", memo_active ? "1" : "0");
        res.set_header("X-Genie-Prompt-Memo-Confidence", std::to_string(memo_confidence));
        res.set_header("X-Genie-Prompt-Memo-Refresh-Count", std::to_string(memo_refresh_count));
        res.set_header("X-Genie-Prompt-Memo-Pages", std::to_string(memo_pages_total));
        res.set_header("X-Genie-Prompt-Messages-Dropped-Prefix-Len", std::to_string(messages_dropped_prefix_len));
        res.set_header("X-Genie-Prompt-Prefix-Reset-Reason", prefix_reset_reason);
    }
};

#endif
