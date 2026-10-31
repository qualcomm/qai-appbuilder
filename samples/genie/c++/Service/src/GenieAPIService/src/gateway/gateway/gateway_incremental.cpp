//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include <algorithm>
#include <mutex>

CheckScope GenieRoutingGateway::DetermineCheckScope(
    const json &request,
    const std::string &global_session_id,
    int &out_last_checked_count)
{
    out_last_checked_count = 0;

    if (!routing_config_.incremental_check.enabled) {
        return CheckScope::FULL;
    }

    if (global_session_id.empty()) {
        return CheckScope::FULL;
    }

    std::lock_guard<std::mutex> lock(session_security_mutex_);

    auto it = session_security_states_.find(global_session_id);

    if (it == session_security_states_.end()) {
        return CheckScope::FULL;
    }

    auto &state = it->second;

    auto now = std::chrono::steady_clock::now();
    auto age = std::chrono::duration_cast<std::chrono::seconds>(
        now - state.last_updated).count();
    if (age > routing_config_.incremental_check.session_ttl_seconds) {
        session_security_states_.erase(it);
        return CheckScope::FULL;
    }

    if (!state.incremental_mode_enabled) {

        if (routing_config_.s2_turn_cleaning.enabled &&
            !state.s2_turn_ranges.empty() &&
            state.max_sensitivity_seen == SensitivityLevel::S2 &&
            state.fallback_reason == "s2_detected") {

        } else {
            return CheckScope::FULL;
        }
    }

    int current_msg_count = request.contains("messages") && request["messages"].is_array()
                            ? (int)request["messages"].size() : 0;
    if (routing_config_.incremental_check.detect_history_tampering &&
        current_msg_count < state.checked_message_count) {
        state.incremental_mode_enabled = false;
        state.fallback_reason = "message_count_decreased";
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] History tampering detected: "
            << "current=" << current_msg_count
            << " < checked=" << state.checked_message_count
            << ". Falling back to full check." << std::endl;
        return CheckScope::FULL;
    }

    if (routing_config_.incremental_check.s2_always_full_check &&
        state.max_sensitivity_seen == SensitivityLevel::S2) {

        if (routing_config_.s2_turn_cleaning.enabled &&
            !state.s2_turn_ranges.empty()) {
            out_last_checked_count = state.checked_message_count;
            My_Log{} << "[GenieRoutingGateway] S2 history with turn ranges: "
                     << "allowing incremental check for current turn "
                     << "(s2_turns=" << state.s2_turn_ranges.size() << ")" << std::endl;
            return CheckScope::INCREMENTAL;
        }

        return CheckScope::FULL;
    }

    if (current_msg_count <= state.checked_message_count) {
        return CheckScope::FULL;
    }

    out_last_checked_count = state.checked_message_count;
    return CheckScope::INCREMENTAL;
}

json GenieRoutingGateway::ExtractIncrementalContent(
    const json &request,
    int last_checked_count)
{
    json incremental = json::object();
    incremental["messages"] = json::array();

    if (!request.contains("messages") || !request["messages"].is_array()) {
        return incremental;
    }

    const auto &messages = request["messages"];

    for (int i = last_checked_count; i < (int)messages.size(); ++i) {
        incremental["messages"].push_back(messages[i]);
    }

    return incremental;
}

bool GenieRoutingGateway::DetectSensitiveReference(const json &incremental_messages)
{

    static const std::vector<std::string> reference_patterns = {
        "刚才的", "之前的", "上面的", "前面提到的", "上次的",
        "the previous", "the above", "mentioned earlier", "as above"
    };

    static const std::vector<std::string> sensitive_keywords = {
        "手机", "电话", "邮箱", "身份证", "银行卡", "密码", "卡号",
        "phone", "email", "id card", "bank card", "password", "card number"
    };

    if (!incremental_messages.is_array()) return false;

    for (const auto &msg : incremental_messages) {
        std::string text;
        if (msg.contains("content") && msg["content"].is_string()) {
            text = msg["content"].get<std::string>();
        } else {
            continue;
        }

        std::string lower_text = text;
        std::transform(lower_text.begin(), lower_text.end(), lower_text.begin(),
                       [](unsigned char c) { return std::tolower(c); });

        for (const auto &ref : reference_patterns) {
            bool ref_found = (text.find(ref) != std::string::npos) ||
                             (lower_text.find(ref) != std::string::npos);
            if (ref_found) {
                for (const auto &kw : sensitive_keywords) {
                    if (text.find(kw) != std::string::npos ||
                        lower_text.find(kw) != std::string::npos) {
                        return true;
                    }
                }
            }
        }
    }
    return false;
}

void GenieRoutingGateway::UpdateSessionSecurityState(
    const std::string &global_session_id,
    const InspectionResult &inspection,
    int current_message_count,
    int turn_start_idx)
{
    if (global_session_id.empty()) return;

    std::lock_guard<std::mutex> lock(session_security_mutex_);

    if ((int)session_security_states_.size() >= routing_config_.incremental_check.max_sessions) {

        auto oldest_it = session_security_states_.begin();
        for (auto it = session_security_states_.begin(); it != session_security_states_.end(); ++it) {
            if (it->second.last_updated < oldest_it->second.last_updated) {
                oldest_it = it;
            }
        }
        My_Log{} << "[GenieRoutingGateway] session_security_states at max capacity, removed LRU entry: "
                 << oldest_it->first << std::endl;
        session_security_states_.erase(oldest_it);
    }

    auto &state = session_security_states_[global_session_id];

    if (static_cast<int>(inspection.sensitivity_level) >
        static_cast<int>(state.max_sensitivity_seen)) {
        state.max_sensitivity_seen = inspection.sensitivity_level;

        if (inspection.sensitivity_level == SensitivityLevel::S2) {
            state.incremental_mode_enabled = false;
            state.fallback_reason = "s2_detected";
            My_Log{} << "[GenieRoutingGateway] S2 detected, incremental mode permanently disabled for global_session="
                     << global_session_id << std::endl;
        }
    }

    if (routing_config_.s2_turn_cleaning.enabled &&
        inspection.sensitivity_level == SensitivityLevel::S2)
    {

        int effective_start = (turn_start_idx >= 0) ? turn_start_idx : 0;

        bool already_recorded = false;
        for (const auto &r : state.s2_turn_ranges) {
            if (r.start_idx == effective_start) {
                already_recorded = true;
                break;
            }
        }
        if (!already_recorded) {
            SessionSecurityState::S2TurnRange range;
            range.start_idx = effective_start;
            range.end_idx = current_message_count;
            state.s2_turn_ranges.push_back(range);
            if (routing_config_.s2_turn_cleaning.log_details) {
                My_Log{} << "[GenieRoutingGateway] S2 turn range recorded: "
                         << "global_session=" << global_session_id
                         << ", turn=[" << range.start_idx << ", " << range.end_idx << ")"
                         << (turn_start_idx < 0 ? " (fallback start=0, no user msg found)" : "")
                         << ", total_s2_turns=" << state.s2_turn_ranges.size() << std::endl;
            }
        }
    }

    state.checked_message_count = current_message_count;
    state.last_updated = std::chrono::steady_clock::now();
}
