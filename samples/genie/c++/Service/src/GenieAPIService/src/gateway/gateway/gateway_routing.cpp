//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include "../../chat_request_handler/model_input_builder.h"
#include "../security/security_utils.h"
#include <chrono>
#include <mutex>

ClientSource GenieRoutingGateway::DetectClientSource(const httplib::Request &http_req,
                                                      const json &request)
{

    auto it = http_req.headers.find("X-Genie-Client");
    if (it != http_req.headers.end())
    {
        const std::string &val = it->second;
        if (val == "QAIAgentForge" || val == "qaiagentforge")
        {
            My_Log{My_Log::Level::kDebug}
                << "[ClientSource] Detected QAI_AGENT_FORGE via X-Genie-Client header" << std::endl;
            return ClientSource::QAI_AGENT_FORGE;
        }
        if (val == "OpenClaw" || val == "openclaw")
        {
            My_Log{My_Log::Level::kDebug}
                << "[ClientSource] Detected OPENCLAW via X-Genie-Client header" << std::endl;
            return ClientSource::OPENCLAW;
        }
    }

    if (request.contains("messages") && request["messages"].is_array())
    {
        for (const auto &msg : request["messages"])
        {
            if (!msg.is_object()) continue;
            std::string role = msg.value("role", "");
            std::string content = SecurityUtils::ExtractMessageContentText(msg);

            if (content.empty()) continue;

            if (role == "user" &&
                content.find("Sender (untrusted metadata):") != std::string::npos)
            {
                My_Log{My_Log::Level::kDebug}
                    << "[ClientSource] Detected OPENCLAW via 'Sender (untrusted metadata):' in user message" << std::endl;
                return ClientSource::OPENCLAW;
            }

            if (role == "system" && content.size() > 5 && content[0] == '[')
            {

                if (content.find("GMT") != std::string::npos &&
                    content.find("20") != std::string::npos)
                {
                    My_Log{My_Log::Level::kDebug}
                        << "[ClientSource] Detected OPENCLAW via timestamp prefix in system prompt" << std::endl;
                    return ClientSource::OPENCLAW;
                }
            }
        }
    }

    My_Log{My_Log::Level::kDebug}
        << "[ClientSource] Source UNKNOWN (no header, no OpenClaw features detected)" << std::endl;
    return ClientSource::UNKNOWN;
}

PromptProcessingPolicy GenieRoutingGateway::ResolvePromptPolicy(ClientSource source,
                                                                  RouteTarget target)
{
    switch (source)
    {
        case ClientSource::QAI_AGENT_FORGE:

            return PromptProcessingPolicy::OPTIMIZED;

        case ClientSource::OPENCLAW:
        case ClientSource::UNKNOWN:
        default:

            if (target == RouteTarget::PUBLIC_CLOUD)
                return PromptProcessingPolicy::PASS_THROUGH;
            return PromptProcessingPolicy::OPTIMIZED;
    }
}

bool GenieRoutingGateway::HandleChatCompletion(const json &request,
                                               const httplib::Request &http_req,
                                               httplib::Response &http_res,
                                               ResponseDispatcher &dispatcher,
                                               ModelInputBuilder &input_builder,
                                               bool &handled_by_cloud)
{
    auto t_start = std::chrono::steady_clock::now();
    handled_by_cloud = false;

    InspectionContext ctx;
    ctx.request_id = GenerateRequestId();
    ResolveServerSessionIds(request, ctx);

    My_Log{} << "[GenieRoutingGateway] HandleChatCompletion start, request_id=" << ctx.request_id << std::endl;

    client_source_ = DetectClientSource(http_req, request);
    const char* source_str =
        (client_source_ == ClientSource::QAI_AGENT_FORGE) ? "QAIAgentForge" :
        (client_source_ == ClientSource::OPENCLAW)         ? "OpenClaw" : "Unknown";
    My_Log{} << "[GenieRoutingGateway] Client source: " << source_str << std::endl;

    enum class ForcedRouteIntent { NONE, FORCE_CLOUD, FORCE_ENTERPRISE_CLOUD, FORCE_LOCAL };
    ForcedRouteIntent forced_route = ForcedRouteIntent::NONE;
    json modified_request = request;

    auto DetectAndRemovePrefix = [](const std::string& text, ForcedRouteIntent& intent) -> std::string {
        if (text.empty()) return text;

        size_t content_start = 0;

        std::string metadata_header = "Sender (untrusted metadata):";
        if (text.find(metadata_header) == 0) {
            size_t json_start = text.find("```json\n", metadata_header.length());
            if (json_start == std::string::npos)
                json_start = text.find("```json\r\n", metadata_header.length());
            if (json_start != std::string::npos) {
                size_t closing_ticks = text.find("```", json_start + 7);
                if (closing_ticks != std::string::npos)
                    content_start = closing_ticks + 3;
            }
        }

        while (content_start < text.length() &&
               (text[content_start] == ' ' || text[content_start] == '\t' ||
                text[content_start] == '\n' || text[content_start] == '\r'))
            content_start++;

        if (content_start < text.length() && text[content_start] == '[') {
            size_t closing_bracket = text.find(']', content_start);
            if (closing_bracket != std::string::npos) {
                bool valid_bracket = true;
                for (size_t i = content_start + 1; i < closing_bracket; ++i) {
                    if (text[i] == '\n' || text[i] == '\r') { valid_bracket = false; break; }
                }
                if (valid_bracket)
                    content_start = closing_bracket + 1;
            }
        }

        while (content_start < text.length() &&
               (text[content_start] == ' ' || text[content_start] == '\t' ||
                text[content_start] == '\n' || text[content_start] == '\r'))
            content_start++;

        if (content_start >= text.length()) return text;

        std::string remaining_text = text.substr(content_start);
        std::string remaining_lower = remaining_text;
        for (auto& c : remaining_lower) {
            unsigned char uc = static_cast<unsigned char>(c);
            if (uc < 128) c = static_cast<char>(std::tolower(uc));
        }

        std::vector<std::pair<std::string, ForcedRouteIntent>> prefixes = {
            {"/企业云：",    ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/企业云:",     ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/企业云",      ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/enterprise:", ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/enterprise",  ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/云：",        ForcedRouteIntent::FORCE_CLOUD},
            {"/云:",         ForcedRouteIntent::FORCE_CLOUD},
            {"/云",          ForcedRouteIntent::FORCE_CLOUD},
            {"/cloud:",      ForcedRouteIntent::FORCE_CLOUD},
            {"/cloud",       ForcedRouteIntent::FORCE_CLOUD},
            {"/本地：",      ForcedRouteIntent::FORCE_LOCAL},
            {"/本地:",       ForcedRouteIntent::FORCE_LOCAL},
            {"/本地",        ForcedRouteIntent::FORCE_LOCAL},
            {"/local:",      ForcedRouteIntent::FORCE_LOCAL},
            {"/local",       ForcedRouteIntent::FORCE_LOCAL}
        };

        for (const auto& [prefix, route_intent] : prefixes) {
            if (remaining_lower.find(prefix) == 0) {
                intent = route_intent;
                std::string prefix_removed = remaining_text.substr(prefix.length());
                size_t first_non_space = prefix_removed.find_first_not_of(" \t\n\r");
                prefix_removed = (first_non_space != std::string::npos)
                                 ? prefix_removed.substr(first_non_space) : "";

                return text.substr(0, content_start) + prefix_removed;
            }
        }
        return text;
    };

    if (modified_request.contains("messages") && modified_request["messages"].is_array()) {
        auto& messages = modified_request["messages"];
        for (auto it = messages.rbegin(); it != messages.rend(); ++it) {
            if ((*it).value("role", "") == "user") {
                if ((*it).contains("content")) {
                    auto& content = (*it)["content"];
                    if (content.is_string()) {
                        std::string original_content = content.get<std::string>();
                        ForcedRouteIntent detected_intent = ForcedRouteIntent::NONE;
                        std::string cleaned = DetectAndRemovePrefix(original_content, detected_intent);
                        if (detected_intent != ForcedRouteIntent::NONE) {
                            forced_route = detected_intent;
                            content = cleaned;
                            const char* intent_str =
                                (forced_route == ForcedRouteIntent::FORCE_CLOUD) ? "FORCE_CLOUD" :
                                (forced_route == ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD) ? "FORCE_ENTERPRISE_CLOUD" :
                                "FORCE_LOCAL";
                            My_Log{} << "[GenieRoutingGateway] User prefix detected (string content): "
                                     << intent_str
                                     << ", original_length=" << original_content.length()
                                     << ", cleaned_length=" << cleaned.length() << std::endl;
                        }
                    } else if (content.is_array()) {
                        std::string merged_text;
                        bool has_text_part = false;
                        for (const auto& part : content) {
                            if (part.is_object() && part.value("type", "") == "text" &&
                                part.contains("text") && part["text"].is_string()) {
                                if (!merged_text.empty()) merged_text += "\n";
                                merged_text += part["text"].get<std::string>();
                                has_text_part = true;
                            }
                        }
                        if (has_text_part) {
                            ForcedRouteIntent detected_intent = ForcedRouteIntent::NONE;
                            std::string cleaned = DetectAndRemovePrefix(merged_text, detected_intent);
                            if (detected_intent != ForcedRouteIntent::NONE) {
                                forced_route = detected_intent;
                                content = json::array({{{"type", "text"}, {"text", cleaned}}});
                                const char* intent_str =
                                    (forced_route == ForcedRouteIntent::FORCE_CLOUD) ? "FORCE_CLOUD" :
                                    (forced_route == ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD) ? "FORCE_ENTERPRISE_CLOUD" :
                                    "FORCE_LOCAL";
                                My_Log{} << "[GenieRoutingGateway] User prefix detected (content array): "
                                         << intent_str
                                         << ", original_length=" << merged_text.length()
                                         << ", cleaned_length=" << cleaned.length() << std::endl;
                            }
                        }
                    }
                }
                break;
            }
        }
    }

    std::string agent_type = DetectAgentTypeFromRequest(modified_request);

    auto t_phase = std::chrono::steady_clock::now();
    auto LogPhase = [&](const char* phase_name) {
        auto now = std::chrono::steady_clock::now();
        int64_t ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - t_phase).count();
        int64_t total_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - t_start).count();
        My_Log{} << "[PERF] " << phase_name << ": phase_ms=" << ms
                 << ", total_ms=" << total_ms << std::endl;
        t_phase = now;
    };

    int last_checked_count = 0;
    CheckScope scope = DetermineCheckScope(modified_request, ctx.global_session_id, last_checked_count);

    int total_messages = 0;
    if (modified_request.contains("messages") && modified_request["messages"].is_array())
        total_messages = (int)modified_request["messages"].size();

    bool incremental_check_used = (scope == CheckScope::INCREMENTAL);
    int  incremental_new_messages = 0;
    std::string incremental_fallback_reason;

    if (scope == CheckScope::INCREMENTAL)
    {
        incremental_new_messages = total_messages - last_checked_count;
        My_Log{} << "[GenieRoutingGateway] Incremental check: "
                 << incremental_new_messages << " new messages (total=" << total_messages << ")" << std::endl;
    }
    else
    {
        My_Log{} << "[GenieRoutingGateway] Full check: total_messages=" << total_messages << std::endl;
    }

    LogPhase("Step0_PreInspect");

    json request_for_inspection = FilterSystemMessagesForInspection(modified_request, agent_type);

    if (scope == CheckScope::INCREMENTAL)
        request_for_inspection = ExtractIncrementalContent(request_for_inspection, last_checked_count);

    InspectionResult inspection;
    if (!routing_config_.sensitivity_detection.enabled)
    {

        inspection.sensitivity_level = SensitivityLevel::S0;
        inspection.summary_reason = "sensitivity_detection disabled";
        My_Log{} << "[GenieRoutingGateway] sensitivity_detection.enabled=false, "
                 << "skipping all sensitivity checks (treating as S0)" << std::endl;
    }
    else
    {
        inspection = Step1_Inspect(request_for_inspection, ctx);
    }

    LogPhase("Step1_Inspect");

    bool s2_history_will_be_cleaned = false;

    int s2_turn_start_for_update = -2;

    int current_turn_start = -1;
    if (modified_request.contains("messages") && modified_request["messages"].is_array())
    {
        const auto &msgs_for_turn = modified_request["messages"];
        for (int i = (int)msgs_for_turn.size() - 1; i >= 0; --i)
        {
            if (msgs_for_turn[i].value("role", "") == "user")
            {
                current_turn_start = i;
                break;
            }
        }
    }

    if (scope == CheckScope::INCREMENTAL)
    {
        std::lock_guard<std::mutex> lock(session_security_mutex_);
        auto it = session_security_states_.find(ctx.global_session_id);
        if (it != session_security_states_.end())
        {
            SensitivityLevel history_level = it->second.max_sensitivity_seen;
            incremental_fallback_reason = it->second.fallback_reason;

            if (history_level == SensitivityLevel::S2 &&
                routing_config_.s2_turn_cleaning.enabled &&
                !it->second.s2_turn_ranges.empty() &&
                inspection.sensitivity_level != SensitivityLevel::S2)
            {
                s2_history_will_be_cleaned = true;
                My_Log{} << "[GenieRoutingGateway] S2 history will be cleaned: "
                         << "s2_turns=" << it->second.s2_turn_ranges.size()
                         << ", current_turn_level=" << to_string(inspection.sensitivity_level)
                         << ", used_incremental=1" << std::endl;

            }
            else if (static_cast<int>(history_level) >
                     static_cast<int>(inspection.sensitivity_level))
            {

                inspection.sensitivity_level = history_level;
                inspection.summary_reason += " (Inherited " +
                    to_string(history_level) + " from session history)";
                My_Log{} << "[GenieRoutingGateway] Sensitivity inherited from history: "
                         << to_string(history_level)
                         << " (incremental check returned lower level)" << std::endl;
            }
        }
    }

    if (scope == CheckScope::INCREMENTAL &&
        inspection.sensitivity_level == SensitivityLevel::S2 &&
        routing_config_.s2_turn_cleaning.enabled &&
        modified_request.contains("messages") && modified_request["messages"].is_array())
    {
        const auto &all_msgs = modified_request["messages"];
        int total_msgs = (int)all_msgs.size();
        int scan_end = (current_turn_start >= 0) ? current_turn_start : total_msgs;
        std::vector<int> incr_turn_starts;
        for (int si = last_checked_count; si < scan_end; ++si)
        {
            if (all_msgs[si].value("role", "") == "user")
                incr_turn_starts.push_back(si);
        }

        int accurate_s2_turn_start = current_turn_start;
        for (size_t ti = 0; ti < incr_turn_starts.size(); ++ti)
        {
            int scan_start = (ti == 0) ? last_checked_count : incr_turn_starts[ti - 1];
            int ti_end = (ti + 1 < incr_turn_starts.size())
                         ? incr_turn_starts[ti + 1]
                         : scan_end;
            json turn_req = json::object();
            turn_req["messages"] = json::array();
            for (int si = scan_start; si < ti_end; ++si)
                turn_req["messages"].push_back(all_msgs[si]);
            json turn_req_filtered = FilterSystemMessagesForInspection(turn_req, agent_type);
            InspectionContext ctx_s2t = ctx;
            ctx_s2t.is_internal_inspection = true;
            InspectionResult s2t_insp = Step1_Inspect(turn_req_filtered, ctx_s2t);
            if (s2t_insp.sensitivity_level == SensitivityLevel::S2)
            {
                accurate_s2_turn_start = scan_start;
                My_Log{} << "[GenieRoutingGateway] Incremental check: S2 located in turn "
                         << "scan=[" << scan_start << ", " << ti_end << ")"
                         << " (user_start=" << incr_turn_starts[ti]
                         << ", original turn_start=" << current_turn_start
                         << "), correcting s2 turn range" << std::endl;
                break;
            }
        }
        s2_turn_start_for_update = accurate_s2_turn_start;
    }

    if (!s2_history_will_be_cleaned &&
        scope == CheckScope::FULL &&
        inspection.sensitivity_level == SensitivityLevel::S2 &&
        routing_config_.s2_turn_cleaning.enabled &&
        current_turn_start >= 0)
    {

        json current_turn_content = ExtractIncrementalContent(modified_request, current_turn_start);
        json current_turn_filtered = FilterSystemMessagesForInspection(current_turn_content, agent_type);
        InspectionContext ctx_ct = ctx;
        ctx_ct.is_internal_inspection = true;
        InspectionResult current_turn_insp = Step1_Inspect(current_turn_filtered, ctx_ct);

        if (current_turn_insp.sensitivity_level != SensitivityLevel::S2)
        {

            {
                std::lock_guard<std::mutex> lock(session_security_mutex_);
                auto &state = session_security_states_[ctx.global_session_id];
                state.max_sensitivity_seen = SensitivityLevel::S2;
                state.incremental_mode_enabled = false;
                state.fallback_reason = "s2_detected";
                if (state.s2_turn_ranges.empty())
                {
                    const auto &hist_msgs = modified_request["messages"];
                    std::vector<int> hist_turn_starts;
                    for (int hi = 0; hi < current_turn_start; ++hi)
                    {
                        if (hist_msgs[hi].value("role", "") == "user")
                            hist_turn_starts.push_back(hi);
                    }

                    for (size_t ht = 0; ht < hist_turn_starts.size(); ++ht)
                    {
                        int scan_start = (ht == 0) ? 0 : hist_turn_starts[ht - 1];
                        int ht_end = (ht + 1 < hist_turn_starts.size())
                                     ? hist_turn_starts[ht + 1]
                                     : current_turn_start;
                        json turn_req = json::object();
                        turn_req["messages"] = json::array();
                        for (int hi = scan_start; hi < ht_end; ++hi)
                            turn_req["messages"].push_back(hist_msgs[hi]);
                        json turn_req_filtered = FilterSystemMessagesForInspection(turn_req, agent_type);
                        InspectionContext ctx_ht = ctx;
                        ctx_ht.is_internal_inspection = true;
                        InspectionResult ht_insp = Step1_Inspect(turn_req_filtered, ctx_ht);
                        if (ht_insp.sensitivity_level == SensitivityLevel::S2)
                        {
                            SessionSecurityState::S2TurnRange range;
                            range.start_idx = scan_start;
                            range.end_idx = ht_end;
                            state.s2_turn_ranges.push_back(range);
                            My_Log{} << "[GenieRoutingGateway] Full check: S2 turn located at "
                                     << "scan=[" << scan_start << ", " << ht_end << ")"
                                     << " (user_start=" << hist_turn_starts[ht] << ")" << std::endl;
                        }
                    }

                    if (state.s2_turn_ranges.empty())
                    {
                        SessionSecurityState::S2TurnRange range;
                        range.start_idx = 0;
                        range.end_idx = current_turn_start;
                        state.s2_turn_ranges.push_back(range);
                        My_Log{My_Log::Level::kWarning}
                            << "[GenieRoutingGateway] Full check: S2 turn scan found no S2 turns, "
                            << "falling back to full history range [0, " << current_turn_start << ")"
                            << std::endl;
                    }
                }
                state.checked_message_count = total_messages;
                state.last_updated = std::chrono::steady_clock::now();
            }

            s2_history_will_be_cleaned = true;
            inspection.sensitivity_level = current_turn_insp.sensitivity_level;

            My_Log{} << "[GenieRoutingGateway] Full check: S2 detected in history only "
                     << "(current turn=" << to_string(current_turn_insp.sensitivity_level)
                     << "), pre-populating S2 range [0, " << current_turn_start
                     << ") for cleaning, will route to CLOUD after cleaning" << std::endl;
        }
        else
        {
            My_Log{} << "[GenieRoutingGateway] Full check: S2 detected in current turn "
                     << "(current turn=" << to_string(current_turn_insp.sensitivity_level)
                     << "), maintaining LOCAL route" << std::endl;
        }
    }

    bool sticky_route_hit = false;
    StickyRouteEntry sticky_entry_copy;
    if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
    {
        std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
        auto it = sticky_sessions_.find(ctx.session_id);
        if (it != sticky_sessions_.end() &&
            std::chrono::steady_clock::now() < it->second.expires_at)
        {
            if (inspection.sensitivity_level != SensitivityLevel::S2)
            {
                sticky_route_hit = true;
                it->second.last_used = std::chrono::steady_clock::now();
                sticky_entry_copy = it->second;
                My_Log{} << "[GenieRoutingGateway] Sticky route hit for session=" << ctx.session_id
                         << ", target=" << (sticky_entry_copy.target == RouteTarget::ENTERPRISE_CLOUD
                                            ? "ENTERPRISE_CLOUD" : "PUBLIC_CLOUD")
                         << ", fallback_msg_count=" << sticky_entry_copy.fallback_msg_count << std::endl;
            }
            else
            {

                sticky_sessions_.erase(it);
                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] Sticky route cleared due to S2 detection for session="
                    << ctx.session_id << std::endl;
            }
        }
    }

    ComplexityResult complexity;
    RouteDecision decision;

    if (sticky_route_hit)
    {

        complexity.complexity_level = ComplexityLevel::C0;
        complexity.reason = "sticky_route_hit";
        decision.target          = sticky_entry_copy.target;
        decision.decision_reason = "sticky_route_session_locked";

        bool going_enterprise = (sticky_entry_copy.target == RouteTarget::ENTERPRISE_CLOUD);
        decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1) &&
                                    (!going_enterprise || routing_config_.enterprise_cloud_require_desensitize);
        decision.local_available  = true;
    }
    else
    {

        complexity = Step2_EvaluateComplexity(modified_request, ctx);
        decision = Step3_Route(inspection, complexity, agent_type);
    }

    LogPhase("Step2_3_RouteDecision");

    if (forced_route != ForcedRouteIntent::NONE && !sticky_route_hit)
    {
        RouteTarget original_target = decision.target;

        if (forced_route == ForcedRouteIntent::FORCE_CLOUD)
        {
            if (inspection.sensitivity_level == SensitivityLevel::S2)
            {

                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] User requested FORCE_CLOUD, but S2 content detected. "
                    << "Overriding to LOCAL (security policy)" << std::endl;
                decision.target = RouteTarget::LOCAL;
                decision.decision_reason = "user_force_cloud_overridden_by_s2_policy";
            }
            else
            {

                bool public_avail     = cloud_client_.IsAvailable();
                bool enterprise_avail = enterprise_cloud_client_.IsAvailable();

                if (public_avail)
                {
                    decision.target = RouteTarget::PUBLIC_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1);
                    decision.decision_reason = "user_force_cloud";
                    My_Log{} << "[GenieRoutingGateway] User prefix: forcing PUBLIC_CLOUD route (original="
                             << to_string(original_target) << ")" << std::endl;
                }
                else if (enterprise_avail)
                {
                    decision.target = RouteTarget::ENTERPRISE_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1)
                                                && routing_config_.enterprise_cloud_require_desensitize;
                    decision.decision_reason = "user_force_cloud_via_enterprise";
                    My_Log{} << "[GenieRoutingGateway] User prefix: forcing ENTERPRISE_CLOUD route "
                             << "(public cloud unavailable, original=" << to_string(original_target) << ")" << std::endl;
                }
                else
                {

                    My_Log{My_Log::Level::kWarning}
                        << "[GenieRoutingGateway] User requested FORCE_CLOUD, but all clouds are unavailable. "
                        << "Falling back to LOCAL" << std::endl;
                    decision.target = RouteTarget::LOCAL;
                    decision.decision_reason = "user_force_cloud_but_all_clouds_unavailable";
                }
            }
        }
        else if (forced_route == ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD)
        {
            if (inspection.sensitivity_level == SensitivityLevel::S2)
            {

                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] User requested FORCE_ENTERPRISE_CLOUD, but S2 content detected. "
                    << "Overriding to LOCAL (security policy)" << std::endl;
                decision.target = RouteTarget::LOCAL;
                decision.decision_reason = "user_force_enterprise_cloud_overridden_by_s2_policy";
            }
            else
            {

                bool enterprise_avail = enterprise_cloud_client_.IsAvailable();
                bool public_avail     = cloud_client_.IsAvailable();

                if (enterprise_avail)
                {
                    decision.target = RouteTarget::ENTERPRISE_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1)
                                                && routing_config_.enterprise_cloud_require_desensitize;
                    decision.decision_reason = "user_force_enterprise_cloud";
                    My_Log{} << "[GenieRoutingGateway] User prefix: forcing ENTERPRISE_CLOUD route (original="
                             << to_string(original_target) << ")" << std::endl;
                }
                else if (public_avail)
                {
                    decision.target = RouteTarget::PUBLIC_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1);
                    decision.decision_reason = "user_force_enterprise_cloud_fallback_public";
                    My_Log{} << "[GenieRoutingGateway] User prefix: ENTERPRISE_CLOUD unavailable, "
                             << "falling back to PUBLIC_CLOUD (original=" << to_string(original_target) << ")" << std::endl;
                }
                else
                {

                    My_Log{My_Log::Level::kWarning}
                        << "[GenieRoutingGateway] User requested FORCE_ENTERPRISE_CLOUD, but all clouds are unavailable. "
                        << "Falling back to LOCAL" << std::endl;
                    decision.target = RouteTarget::LOCAL;
                    decision.decision_reason = "user_force_enterprise_cloud_but_all_clouds_unavailable";
                }
            }
        }
        else if (forced_route == ForcedRouteIntent::FORCE_LOCAL)
        {
            if (!local_model_config_.enabled || !model_config_.IsLocalModelAvailable())
            {
                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] User requested FORCE_LOCAL, but local model is unavailable. "
                    << "Returning error" << std::endl;
                decision.target = RouteTarget::POLICY_ERROR;
                decision.error_code = "user_force_local_but_local_unavailable";
                decision.error_http_status = 503;
                decision.decision_reason = "user_force_local_but_local_unavailable";
            }
            else
            {
                decision.target = RouteTarget::LOCAL;
                decision.need_desensitize = false;
                decision.decision_reason = "user_force_local";
                My_Log{} << "[GenieRoutingGateway] User prefix: forcing LOCAL route (original="
                         << to_string(original_target) << ")" << std::endl;
            }
        }
    }

    json request_for_cloud = modified_request;
    bool is_cloud_target = (decision.target == RouteTarget::ENTERPRISE_CLOUD ||
                            decision.target == RouteTarget::PUBLIC_CLOUD);
    bool s2_cleaning_occurred = false;

    if (is_cloud_target && s2_history_will_be_cleaned)
    {

        json s2_cleaned = CleanS2TurnsForCloud(
            request_for_cloud, ctx.global_session_id,
            current_turn_start, inspection.sensitivity_level,
            "HandleChatCompletion");

        s2_cleaning_occurred = s2_cleaned.contains("messages") &&
                               s2_cleaned["messages"] != request_for_cloud["messages"];

        if (s2_cleaning_occurred)
        {

            json cleaned_for_check = FilterSystemMessagesForInspection(s2_cleaned, agent_type);
            InspectionContext ctx_recheck = ctx;
            ctx_recheck.is_internal_inspection = true;
            InspectionResult cleaned_inspection = Step1_Inspect(cleaned_for_check, ctx_recheck);

            if (cleaned_inspection.sensitivity_level == SensitivityLevel::S2)
            {

                My_Log{My_Log::Level::kError}
                    << "[GenieRoutingGateway] S2 turn cleaning: re-inspection found S2 in cleaned history! "
                    << "Forcing LOCAL route to prevent S2 data leakage." << std::endl;
                decision.target = RouteTarget::LOCAL;
                decision.need_desensitize = false;
                decision.sensitivity = SensitivityLevel::S2;
                decision.decision_reason += " (Fallback: S2 found in cleaned history, forced LOCAL)";
                is_cloud_target = false;
                s2_cleaning_occurred = false;

            }
            else
            {

                request_for_cloud = s2_cleaned;

                if (static_cast<int>(cleaned_inspection.sensitivity_level) >
                    static_cast<int>(inspection.sensitivity_level))
                {
                    inspection.sensitivity_level = cleaned_inspection.sensitivity_level;
                    inspection.summary_reason += " (Re-inspected after S2 turn cleaning: " +
                        to_string(cleaned_inspection.sensitivity_level) + ")";
                    {
                        std::lock_guard<std::mutex> lock(session_security_mutex_);
                        auto it = session_security_states_.find(ctx.global_session_id);
                        if (it != session_security_states_.end())
                            it->second.max_sensitivity_seen = inspection.sensitivity_level;
                    }
                }
                decision.sensitivity = inspection.sensitivity_level;

                if (decision.target == RouteTarget::LOCAL &&
                    routing_config_.s2_turn_cleaning.allow_cloud_reroute_after_clean &&
                    forced_route == ForcedRouteIntent::NONE)
                {
                    My_Log{} << "[GenieRoutingGateway] S2 turn cleaning: re-evaluating route after history cleaned "
                             << "(original decision=LOCAL, checking if CLOUD is now appropriate)..." << std::endl;
                    RouteDecision new_decision = Step3_Route(inspection, complexity, agent_type);

                    if (new_decision.target == RouteTarget::ENTERPRISE_CLOUD ||
                        new_decision.target == RouteTarget::PUBLIC_CLOUD)
                    {
                        My_Log{} << "[GenieRoutingGateway] S2 turn cleaning: re-routing LOCAL→"
                                 << to_string(new_decision.target) << " confirmed, "
                                 << "new_reason=" << new_decision.decision_reason << std::endl;
                        decision = new_decision;
                        is_cloud_target = true;
                    }
                    else
                    {
                        My_Log{} << "[GenieRoutingGateway] S2 turn cleaning: re-routing evaluated, staying LOCAL "
                                 << "(reason=" << new_decision.decision_reason << ")" << std::endl;
                    }
                }

                if (inspection.sensitivity_level == SensitivityLevel::S1)
                {
                    bool going_enterprise = (decision.target == RouteTarget::ENTERPRISE_CLOUD);
                    decision.need_desensitize = is_cloud_target &&
                        (!going_enterprise || routing_config_.enterprise_cloud_require_desensitize);
                }
                else if (inspection.sensitivity_level == SensitivityLevel::S0)
                {
                    decision.need_desensitize = false;
                }
            }
        }
        else
        {

        }
    }
    else if (is_cloud_target)
    {

        bool has_s2_turns = false;
        if (routing_config_.s2_turn_cleaning.enabled && !ctx.global_session_id.empty())
        {
            std::lock_guard<std::mutex> lock(session_security_mutex_);
            auto it = session_security_states_.find(ctx.global_session_id);
            has_s2_turns = (it != session_security_states_.end() &&
                            !it->second.s2_turn_ranges.empty());
        }
        if (has_s2_turns)
        {
            request_for_cloud = CleanS2TurnsForCloud(
                request_for_cloud, ctx.global_session_id,
                current_turn_start, inspection.sensitivity_level,
                "HandleChatCompletion");
        }
    }

    {
        int turn_start_for_update = current_turn_start;

        if (s2_turn_start_for_update != -2)
            turn_start_for_update = s2_turn_start_for_update;

        UpdateSessionSecurityState(ctx.global_session_id, inspection,
                                   total_messages, turn_start_for_update);
    }

    if (is_cloud_target && sticky_route_hit)
    {
        request_for_cloud = CleanLocalHistoryForCloudFallback(
            request_for_cloud, sticky_entry_copy.fallback_msg_count);
    }

    DesensitizationResult desensitized;
    desensitized.success = true;

    if (is_cloud_target && decision.need_desensitize)
    {
        LogPhase("Step4_PreDesensitize");

        desensitized = DesensitizeForCloudOnly(request_for_cloud, inspection, ctx,
                                               agent_type, "HandleChatCompletion");
        LogPhase("Step4_Desensitize");
        if (!desensitized.success)
        {
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Desensitization failed (" << desensitized.failure_reason
                << "), falling back to LOCAL or ERROR" << std::endl;

            bool local_avail = local_model_config_.enabled && model_config_.IsLocalModelAvailable();
            if (local_avail)
            {
                decision.target           = RouteTarget::LOCAL;
                decision.need_desensitize = false;
                decision.decision_reason  = "desensitization_failed_fallback_local";
                is_cloud_target           = false;
            }
            else
            {
                decision.target           = RouteTarget::POLICY_ERROR;
                decision.error_code       = "desensitization_failed";
                decision.error_http_status = 403;
                decision.local_available  = false;
            }
        }
    }

    LogPhase("Step5_PreExecute");
    bool ok = Step5_Execute(decision,
                            request_for_cloud,
                            desensitized,
                            http_req, http_res,
                            dispatcher, input_builder,
                            handled_by_cloud,
                            ctx.session_id);

    if (handled_by_cloud && !sticky_route_hit &&
        routing_config_.sticky_routing.enabled && !ctx.session_id.empty())
    {
        InsertStickySession(ctx.session_id, decision.target, inspection.sensitivity_level, total_messages);

        My_Log{} << "[GenieRoutingGateway] Sticky route set for session=" << ctx.session_id
                 << ", target=" << (decision.target == RouteTarget::ENTERPRISE_CLOUD
                                    ? "ENTERPRISE_CLOUD" : "PUBLIC_CLOUD")
                 << ", fallback_msg_count=" << total_messages << std::endl;
    }

    auto t_end = std::chrono::steady_clock::now();
    int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(t_end - t_start).count();

    int audit_http_status = handled_by_cloud ? 200 : -1;

    Step6_Audit(ctx, inspection, complexity, decision, desensitized,
                latency_ms,
                audit_http_status,
                sticky_route_hit,
                0, 0, 0,
                incremental_check_used,
                incremental_new_messages,
                total_messages,
                incremental_fallback_reason);

    return ok;
}
