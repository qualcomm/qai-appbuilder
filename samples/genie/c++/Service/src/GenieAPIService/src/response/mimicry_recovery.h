//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MIMICRY_RECOVERY_H
#define MIMICRY_RECOVERY_H

#include <cstddef>
#include <string>

namespace MimicryRecovery
{
constexpr size_t kSoftTokenLimit = 300;
constexpr size_t kHardTokenLimit = 900;

enum class Action
{
    kContinue,
    kSoftStop,
    kHardStop
};

inline Action Decide(size_t mimicry_tokens, bool soft_retry_used, bool has_tool_call)
{
    if (has_tool_call)
        return Action::kContinue;
    if (mimicry_tokens > kHardTokenLimit)
        return Action::kHardStop;
    if (!soft_retry_used && mimicry_tokens > kSoftTokenLimit)
        return Action::kSoftStop;
    return Action::kContinue;
}

inline bool ShouldDegradeInsteadOfAbort(bool query_ok, bool hard_stop_triggered, bool connection_broken, size_t buffer_size)
{
    return !query_ok && hard_stop_triggered && !connection_broken && buffer_size > 0;
}

inline std::string BuildCorrectionMessage()
{
    return "Your previous reply only repeated the system's reasoning-note text and did not make progress. "
           "Do not repeat that note. Either call a tool with a new action, or answer the user directly "
           "in your own words.";
}
}

#endif
