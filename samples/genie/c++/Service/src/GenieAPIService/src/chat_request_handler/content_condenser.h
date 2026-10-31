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

enum class ContentKind {
    kGenericText,
    kToolResponse,
    kJsonPayload
};

struct CondenseBudget {
    size_t max_tokens   = 0;
    size_t max_chars    = 0;
    double tail_ratio   = 0.30;
    bool   extract_high_signal = true;
    size_t max_token_probe     = 8;
    size_t json_head_items     = 3;
    size_t json_tail_items     = 1;

    double cjk_bytes_per_token   = 3.0;
    double ascii_bytes_per_token = 4.0;
};

struct CondenseResult {
    std::string text;
    bool   truncated = false;
    size_t omitted_chars = 0;
    size_t high_signal_lines = 0;
};

class ContentCondenser {
public:
    static CondenseResult Condense(const std::string& content,
                                    ContentKind kind,
                                    const CondenseBudget& budget,
                                    const std::function<size_t(const std::string&)>* token_len);

private:
    static std::vector<std::string> ExtractHighSignalLines(const std::string& text, size_t max_lines);

    static std::string HeadTailKeep(const std::string& text, size_t head_chars, size_t tail_chars);

    static std::string CondenseJsonArray(const nlohmann::json& arr, size_t head_n, size_t tail_n, size_t max_chars);
};

#endif
