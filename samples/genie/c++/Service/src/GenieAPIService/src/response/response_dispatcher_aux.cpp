//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_dispatcher.h"

#include "log.h"
#include "log_table.h"

#include <cstdint>
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
        double prefill_tokens{0.0};
        double prefill_seconds{0.0};
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
    const std::string title = "Token Summary [slot: " + slot_label + "] Round " + std::to_string(round);
    LogTable table{title};

    auto handle = GetEffectiveHandle();
    if (!handle)
    {
        My_Log{My_Log::Level::kWarning}
            << "[PrintProfile] Model handle is null, skip profile summary." << std::endl;
        table.SetTitle(title + " - profile unavailable");
        My_Log{}.original() << table.Str() << std::flush;
        return;
    }
    auto json_str = handle->HandleProfile();
    if (json_str.empty())
    {
        table.SetTitle(title + " - no profile data");
        My_Log{}.original() << table.Str() << std::flush;
        return;
    }

    try
    {
        const double pp_rate = std::stod(json_str.at("prompt_processing_rate").get<std::string>());
        const double tg_rate = std::stod(json_str.at("token_generation_rate").get<std::string>());
        uint64_t samples;
        double pp_avg;
        double tg_avg;
        const bool weighted = json_str.contains("num_prefill_tokens") && json_str.contains("prefill_time");
        const double prefill_tokens = weighted ? json_str.at("num_prefill_tokens").get<double>() : 0.0;
        const double prefill_seconds = weighted ? std::stod(json_str.at("prefill_time").get<std::string>()) : 0.0;
        {
            auto &totals = GetProfileTotals();
            std::lock_guard<std::mutex> lk(totals.mtx);
            auto &slot_totals = totals.slots[slot_name];
            samples = ++slot_totals.samples;
            slot_totals.pp_rate_sum += pp_rate;
            slot_totals.tg_rate_sum += tg_rate;
            if (weighted)
            {
                slot_totals.prefill_tokens += prefill_tokens;
                slot_totals.prefill_seconds += prefill_seconds;
            }
            pp_avg = slot_totals.prefill_seconds > 0.0
                ? slot_totals.prefill_tokens / slot_totals.prefill_seconds
                : slot_totals.pp_rate_sum / static_cast<double>(samples);
            tg_avg = slot_totals.tg_rate_sum / static_cast<double>(samples);
        }

        const std::string prompt_tokens_text = weighted
            ? LogTable::JsonText(json_str.at("num_prompt_tokens")) + " (new " + std::to_string(static_cast<long long>(prefill_tokens)) + ")"
            : LogTable::JsonText(json_str.at("num_prompt_tokens"));
        const std::vector<std::string> prompt_row{
            "Prompt (TTFT)",
            prompt_tokens_text,
            std::to_string(model_input_.text_.length()),
            json_str.at("time_to_first_token").get<std::string>(),
            json_str.at("prompt_processing_rate").get<std::string>(),
            LogTable::Fixed(pp_avg)};
        const std::vector<std::string> generation_row{
            "Generation",
            LogTable::JsonText(json_str.at("num_generated_tokens")),
            std::to_string(response_buffer.length()),
            json_str.at("token_generation_time").get<std::string>(),
            json_str.at("token_generation_rate").get<std::string>(),
            LogTable::Fixed(tg_avg)};

        table.SetTitle(title + ", slot avg of " + std::to_string(samples) + " rounds");
        table.SetHeader({"Phase", "Tokens", "Text Length", "Time (s)", "Rate (toks/s)", "Slot Avg (toks/s)"},
                        {LogTable::Align::kLeft, LogTable::Align::kRight, LogTable::Align::kRight,
                         LogTable::Align::kRight, LogTable::Align::kRight, LogTable::Align::kRight});
        table.AddRow(prompt_row).AddRow(generation_row);
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "profile print failed:" << e.what() << std::endl;
        table.SetTitle(title + " - profile unavailable");
    }

    My_Log{}.original() << table.Str() << std::flush;
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
