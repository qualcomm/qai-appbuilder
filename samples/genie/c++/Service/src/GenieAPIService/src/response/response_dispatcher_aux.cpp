//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_dispatcher.h"

#include "log.h"

#include <cstdint>
#include <iomanip>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace
{
    struct SlotProfileTotals
    {
        uint64_t rounds{0};
        uint64_t samples{0};
        double pp_rate_sum{0.0};
        double tg_rate_sum{0.0};
    };

    struct ProfileTotals
    {
        std::mutex mtx;
        std::unordered_map<std::string, SlotProfileTotals> slots;
    };

    ProfileTotals &GetProfileTotals()
    {
        static ProfileTotals totals;
        return totals;
    }
}

void ResponseDispatcher::PrintProfile()
{
    const std::string slot_name = instance_config_ ? instance_config_->get_model_name() : std::string{"(default)"};
    const std::string slot_label = instance_config_
        ? slot_name + " | " + instance_config_->get_backend() + "/" + instance_config_->get_device()
        : slot_name;
    uint64_t round;
    {
        auto &totals = GetProfileTotals();
        std::lock_guard<std::mutex> lk(totals.mtx);
        round = ++totals.slots[slot_name].rounds;
    }
    My_Log{} << "--- Token Summary Start [slot: " << slot_label << "] Round " << round << " ---" << std::endl;

    auto handle = GetEffectiveHandle();
    if (!handle)
    {
        My_Log{My_Log::Level::kWarning}
            << "[PrintProfile] Model handle is null, skip profile summary." << std::endl;
        My_Log{} << "--- Token Summary End [slot: " << slot_label << "] ---\n";
        return;
    }
    auto json_str = handle->HandleProfile();
    if (json_str.empty())
    {
        goto done;
    }

    try
    {
        const double pp_rate = std::stod(json_str.at("prompt_processing_rate").get<std::string>());
        const double tg_rate = std::stod(json_str.at("token_generation_rate").get<std::string>());
        uint64_t samples;
        double pp_avg;
        double tg_avg;
        {
            auto &totals = GetProfileTotals();
            std::lock_guard<std::mutex> lk(totals.mtx);
            auto &slot_totals = totals.slots[slot_name];
            samples = ++slot_totals.samples;
            slot_totals.pp_rate_sum += pp_rate;
            slot_totals.tg_rate_sum += tg_rate;
            pp_avg = slot_totals.pp_rate_sum / static_cast<double>(samples);
            tg_avg = slot_totals.tg_rate_sum / static_cast<double>(samples);
        }

        My_Log{}.original() << "Time to First Token: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("time_to_first_token").get<std::string>()
                 << " s" << std::endl;

        My_Log{}.original() << "Token Generation Time: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("token_generation_time").get<std::string>()
                 << " s" << std::endl;

        My_Log{}.original() << "Num Prompt Tokens: "
                 << json_str.at("num_prompt_tokens")
                 << ", Text Length: " << model_input_.text_.length()
                 << std::endl;

        My_Log{}.original() << "Prompt Processing Rate: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("prompt_processing_rate").get<std::string>()
                 << " toks/sec (slot avg of " << samples << " rounds: "
                 << pp_avg << " toks/sec)" << std::endl;

        My_Log{}.original() << "Num Generated Tokens: "
                 << json_str.at("num_generated_tokens")
                 << ", Text Length: " << response_buffer.length()
                 << std::endl;

        My_Log{}.original() << "Token Generation Rate: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("token_generation_rate").get<std::string>()
                 << " toks/sec (slot avg of " << samples << " rounds: "
                 << tg_avg << " toks/sec)" << std::endl;
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "profile print failed:" << e.what() << std::endl;
    }

    done:
    My_Log{} << "--- Token Summary End [slot: " << slot_label << "] ---\n";
}

std::pair<std::string, std::string> ResponseDispatcher::GetToolCallStatusByName(const std::string &tool_name)
{
    struct StatusRule
    {
        std::vector<const char *> keywords;
        const char *status;
        const char *message;
    };
    static const StatusRule kRules[] = {
        {{"execute_script", "run_script", "exec_script"}, "executing_script", "Executing script..."},
        {{"execute_command", "run_command", "exec_command", "shell", "bash"}, "executing_command", "Executing command..."},
        {{"read_file", "write_file", "create_file", "delete_file"}, "file_operation", "Operating on file..."},
        {{"search", "browse", "fetch"}, "searching", "Searching..."},
        {{"write_code", "generate_code"}, "writing_code", "Writing code..."},
    };

    for (const auto &rule : kRules)
    {
        for (const char *keyword : rule.keywords)
        {
            if (tool_name.find(keyword) != std::string::npos)
            {
                return {rule.status, rule.message};
            }
        }
    }

    return {"", ""};
}
