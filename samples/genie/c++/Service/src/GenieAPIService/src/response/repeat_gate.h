//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef REPEAT_GATE_H
#define REPEAT_GATE_H

#include "../chat_request_handler/tool_call_repetition_store.h"

#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace RepeatGateLogic
{
    using Json = nlohmann::ordered_json;
    using Outcome = ToolCallRepetitionStore::SignatureRecord::Outcome;

    struct ExecutedOutcome
    {
        std::string signature;
        std::string content;
    };

    std::string MakeSignature(const Json& function_obj);

    std::string ExtractContentText(const Json& content);

    std::vector<ExecutedOutcome> CollectExecutedOutcomes(const Json& messages, size_t* orphan_tool_results = nullptr);

    Outcome ClassifyOutcome(const std::string& content);

    std::string MakeOutcomePreview(const std::string& content);

    std::string BuildHardBlockBody(const std::string& tool_name, int executed_count,
                                   const std::string& outcome_preview);

    std::string BuildHardBlockFinalBody(const std::string& tool_name, int executed_count,
                                        int blocked_count, const std::string& outcome_preview);

    std::string BuildSoftRetryNote(const std::string& outcome_preview, int executed_count);

    bool TryConsumeExtraRetry(bool& extra_retry_used);
}

#endif
