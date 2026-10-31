//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PROMPT_PREPARATION_SERVICE_H
#define PROMPT_PREPARATION_SERVICE_H

#include "../model/model_config.h"
#include "prompt_optimizer.h"
#include <nlohmann/json.hpp>
#include <string>

using json = nlohmann::ordered_json;

struct PreparedPromptResult {
    bool success = false;
    json prepared_request;
    std::string optimized_system_prompt;
    int used_context_size = 0;
    bool prompt_optimized = false;
    bool messages_trimmed = false;
    bool tools_filtered = false;
    std::string failure_reason;

    std::string agent_type = "sub";
};

class PromptPreparationService
{
public:

    explicit PromptPreparationService(IModelConfig &model_config);

    PreparedPromptResult PrepareForCloud(const json &request, int context_size);

private:
    IModelConfig &model_config_;

    static std::string ExtractSystemPrompt(const json &messages);

    static json ExtractNonSystemMessages(const json &messages);

    static json RebuildMessagesForCloud(const std::string &optimized_system_prompt,
                                         const json &non_system_messages);

    static std::string AdaptSystemPromptForCloud(const std::string &system_prompt);
};

#endif
