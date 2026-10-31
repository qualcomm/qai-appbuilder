//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SECURITY_PROMPT_BUILDER_H
#define SECURITY_PROMPT_BUILDER_H

#include "../../model/model_config.h"
#include "../../model/model_instance_config.h"
#include "../../processor/harmony.h"
#include <chrono>
#include <iomanip>
#include <ctime>
#include <sstream>
#include <string>

inline std::string BuildLocalModelPrompt(
    const IModelConfig* model_config,
    const std::string& system_prompt,
    const std::string& user_message,
    const std::string& json_prefill = "")
{
    if (!model_config)
    {

        return system_prompt + "\n" + user_message;
    }

    const ModelInstanceConfig* instance_config = model_config->GetDefaultInstanceConfig();

    auto is_thinking  = [&]() -> bool {
        return instance_config ? instance_config->is_thinking_model() : model_config->is_thinking_model();
    };
    auto enable_think = [&]() -> bool {
        return instance_config ? instance_config->getenableThinking() : model_config->getenableThinking();
    };
    auto prompt_type  = [&]() -> PromptType {
        return instance_config ? instance_config->get_prompt_type() : model_config->get_prompt_type();
    };
    auto prompt_tmpl  = [&]() -> const json& {
        return instance_config ? instance_config->get_prompt_template() : model_config->get_prompt_template();
    };

    auto append_think_control = [&](std::string& system_text) {
        if (is_thinking()) {
            if (enable_think()) {
                system_text += "/think";
            } else {
                system_text += "/no_think";
            }
        }
    };

    bool is_harmony = (prompt_type() == PromptType::Harmony);

    if (is_harmony)
    {

        const auto &j = prompt_tmpl();
        std::string knowledge_cutoff = j.value("knowledge_cutoff", "2024-06");

        std::string reasoning_level = "low";

        auto now_tp    = std::chrono::system_clock::now();
        auto time_t_val = std::chrono::system_clock::to_time_t(now_tp);
        std::tm tm_val  = *std::localtime(&time_t_val);
        std::ostringstream date_oss;
        date_oss << std::put_time(&tm_val, "%Y-%m-%d");
        std::string current_date = date_oss.str();

        std::string system_msg = HarmonyProcessor::BuildSystemMessage(
            knowledge_cutoff, current_date, reasoning_level, false);

        std::string effective_system_prompt = system_prompt;
        append_think_control(effective_system_prompt);

        std::string developer_msg  = "<|start|>developer<|message|># Instructions\n\n";
        developer_msg += effective_system_prompt;
        developer_msg += "<|end|>";

        std::string user_msg = HarmonyProcessor::BuildUserMessage(user_message);

        return system_msg + developer_msg + user_msg
            + "<|start|>assistant<|channel|>final<|message|>" + json_prefill;
    }
    else
    {

        const auto &j = prompt_tmpl();

        auto replace_placeholder = [](const std::string& tmpl, const std::string& content) -> std::string
        {
            std::string result = tmpl;
            size_t pos = result.find("string");
            if (pos != std::string::npos)
                result.replace(pos, 6, content);
            return result;
        };

        std::string effective_system_prompt = system_prompt;
        std::string start_prompt = j["start"].get<std::string>();
        append_think_control(effective_system_prompt);

        std::string formatted_system = replace_placeholder(j["system"].get<std::string>(), effective_system_prompt);
        std::string formatted_user   = replace_placeholder(j["user"].get<std::string>(),   user_message);

        return formatted_system + formatted_user + start_prompt + json_prefill;
    }
}

#endif
