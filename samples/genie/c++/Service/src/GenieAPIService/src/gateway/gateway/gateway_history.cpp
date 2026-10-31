//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include <set>
#include <mutex>

json GenieRoutingGateway::CleanLocalHistoryForCloudFallback(const json &request, int fallback_boundary)
{
    json cleaned = request;
    if (!cleaned.contains("messages") || !cleaned["messages"].is_array()) return cleaned;

    const auto &messages = cleaned["messages"];

    int last_user_idx = -1;
    for (int i = (int)messages.size() - 1; i >= 0; --i)
    {
        if (messages[i].value("role", "") == "user")
        {
            last_user_idx = i;
            break;
        }
    }
    if (last_user_idx < 0) return cleaned;

    json new_messages = json::array();
    for (int i = 0; i <= last_user_idx; ++i)
    {
        new_messages.push_back(messages[i]);
    }

    for (int i = last_user_idx + 1; i < (int)messages.size(); ++i)
    {
        if (i < (int)messages.size())
        {
            const auto &msg = messages[i];

            if (fallback_boundary > 0 && i >= fallback_boundary)
            {
                new_messages.push_back(msg);
                continue;
            }

            std::string role = msg.value("role", "");
            if (role == "tool") continue;
            if (role == "assistant" && msg.contains("tool_calls")) continue;
            new_messages.push_back(msg);
        }
    }

    cleaned["messages"] = new_messages;

    My_Log{} << "[GenieRoutingGateway] CleanLocalHistoryForCloudFallback: "
             << "original_messages=" << messages.size()
             << ", cleaned_messages=" << new_messages.size()
             << ", fallback_boundary=" << fallback_boundary << std::endl;

    return cleaned;
}

json GenieRoutingGateway::PurgeS2TurnsFromHistory(
    const json &request,
    const std::vector<SessionSecurityState::S2TurnRange> &s2_turn_ranges,
    bool log_details)
{
    if (s2_turn_ranges.empty()) return request;
    if (!request.contains("messages") || !request["messages"].is_array()) return request;

    const auto &messages = request["messages"];
    int total = (int)messages.size();

    std::set<int> indices_to_remove;
    for (const auto &range : s2_turn_ranges) {
        int start = std::max(0, range.start_idx);
        int end = std::min(total, range.end_idx);
        for (int i = start; i < end; ++i) {
            const std::string role = messages[i].value("role", "");
            if (role != "system" && role != "developer") {
                indices_to_remove.insert(i);
            }
        }
    }

    if (indices_to_remove.empty()) return request;

    json new_messages = json::array();
    bool in_removed_segment = false;

    for (int i = 0; i < total; ++i) {
        bool is_removed = (indices_to_remove.count(i) > 0);

        if (is_removed) {

            if (!in_removed_segment) {

                json user_placeholder = json::object();
                user_placeholder["role"] = "user";
                user_placeholder["content"] = "[Previous user message was removed due to security policy]";
                new_messages.push_back(user_placeholder);

                json assistant_placeholder = json::object();
                assistant_placeholder["role"] = "assistant";
                assistant_placeholder["content"] = "[Previous assistant response was removed due to security policy]";
                new_messages.push_back(assistant_placeholder);

                in_removed_segment = true;
            }

        } else {

            in_removed_segment = false;
            new_messages.push_back(messages[i]);
        }
    }

    json result = request;
    result["messages"] = new_messages;

    if (log_details) {
        My_Log{} << "[GenieRoutingGateway] PurgeS2TurnsFromHistory: "
                 << "original_messages=" << total
                 << ", removed=" << indices_to_remove.size()
                 << ", cleaned_messages=" << new_messages.size()
                 << ", s2_turn_ranges=" << s2_turn_ranges.size() << std::endl;
        for (size_t i = 0; i < s2_turn_ranges.size(); ++i) {
            My_Log{} << "[GenieRoutingGateway]   S2 turn[" << i << "]: "
                     << "[" << s2_turn_ranges[i].start_idx << ", " << s2_turn_ranges[i].end_idx << ")"
                     << std::endl;
        }
    } else {
        My_Log{} << "[GenieRoutingGateway] PurgeS2TurnsFromHistory: "
                 << "removed " << indices_to_remove.size() << " messages from "
                 << s2_turn_ranges.size() << " S2 turn(s), "
                 << "cleaned_messages=" << new_messages.size() << std::endl;
    }

    return result;
}

json GenieRoutingGateway::CleanS2TurnsForCloud(
    const json &request,
    const std::string &global_session_id,
    int current_turn_start,
    SensitivityLevel new_max_sensitivity,
    const std::string &log_tag)
{
    if (!routing_config_.s2_turn_cleaning.enabled || global_session_id.empty()) {
        return request;
    }

    std::vector<SessionSecurityState::S2TurnRange> s2_ranges;
    {
        std::lock_guard<std::mutex> lock(session_security_mutex_);
        auto it = session_security_states_.find(global_session_id);
        if (it == session_security_states_.end() || it->second.s2_turn_ranges.empty()) {
            return request;
        }
        s2_ranges = it->second.s2_turn_ranges;
    }

    for (size_t i = 0; i < s2_ranges.size(); ++i) {
        if (i + 1 < s2_ranges.size()) {

            s2_ranges[i].end_idx = s2_ranges[i + 1].start_idx;
        } else {

            if (current_turn_start >= 0) {
                s2_ranges[i].end_idx = current_turn_start;
            }
        }
    }

    json cleaned = PurgeS2TurnsFromHistory(request, s2_ranges,
                                           routing_config_.s2_turn_cleaning.log_details);

    {
        std::lock_guard<std::mutex> lock(session_security_mutex_);
        auto it = session_security_states_.find(global_session_id);
        if (it != session_security_states_.end()) {
            it->second.s2_turn_ranges.clear();
            it->second.max_sensitivity_seen = new_max_sensitivity;
            it->second.incremental_mode_enabled = true;
            it->second.fallback_reason = "";

            if (request.contains("messages") && request["messages"].is_array()) {
                it->second.checked_message_count = (int)request["messages"].size();
            }
        }
    }

    My_Log{} << "[GenieRoutingGateway] " << log_tag << ": cleaned "
             << s2_ranges.size() << " S2 turn(s) from history"
             << ", new_max_sensitivity=" << to_string(new_max_sensitivity)
             << std::endl;

    return cleaned;
}
