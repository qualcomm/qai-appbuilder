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
#include <sstream>
#include <iomanip>
#include <mutex>

json GenieRoutingGateway::FilterSystemMessagesForInspection(const json &request, const std::string &agent_type)
{

    if (agent_type != "main")
    {
        return request;
    }

    if (!request.contains("messages") || !request["messages"].is_array())
    {
        return request;
    }

    json result = request;
    json filtered = json::array();
    for (const auto& msg : request["messages"])
    {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
        {
            filtered.push_back(msg);
        }
    }
    result["messages"] = filtered;
    return result;
}

InspectionResult GenieRoutingGateway::Step1_Inspect(const json &request,
                                             const InspectionContext &ctx)
{
    return inspector_.Inspect(request, ctx);
}

ComplexityResult GenieRoutingGateway::Step2_EvaluateComplexity(const json &request,
                                                        const InspectionContext &ctx)
{
    json messages = request.value("messages", json::array());
    json tools = request.value("tools", json::array());
    return complexity_evaluator_.Evaluate(messages, tools, ctx);
}

RouteDecision GenieRoutingGateway::Step3_Route(const InspectionResult &inspection,
                                       const ComplexityResult &complexity,
                                       const std::string &agent_type)
{

    bool public_cloud_available = cloud_client_.IsAvailable();

    bool enterprise_cloud_available = enterprise_cloud_client_.IsAvailable();

    bool local_available = local_model_config_.enabled && model_config_.IsLocalModelAvailable();

    return router_.Route(inspection, complexity,
                         enterprise_cloud_available,
                         public_cloud_available,
                         local_available,
                         agent_type);
}

DesensitizationResult GenieRoutingGateway::Step4_Desensitize(const json &request,
                                                       const InspectionResult &inspection,
                                                       const InspectionContext &ctx)
{

    json current = request;
    int rounds = 0;

    std::unordered_map<std::string, std::string> accumulated_mapping;

    while (true) {

        InspectionContext ctx2;
        ctx2.request_id = ctx.request_id;
        ctx2.session_id = ctx.session_id;
        ctx2.rule_engine_only = true;
        ctx2.collect_spans = true;
        ctx2.is_internal_inspection = false;

        InspectionResult insp = inspector_.Inspect(current, ctx2);

        if (insp.hit_rule_ids.empty()) {
            DesensitizationResult ok;
            ok.success = true;
            ok.desensitized_request = current;
            if (rounds > 0) {
                ok.applied_strategies.push_back("iterative_" + std::to_string(rounds) + "_rounds");
            }
            return ok;
        }

        if (insp.sensitivity_level == SensitivityLevel::S2) {
            DesensitizationResult fail;
            fail.success = false;
            fail.failure_reason = "s2_found_in_iteration";
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed: S2 content found in iteration round=" << rounds << std::endl;
            return fail;
        }

        const std::unordered_map<std::string, std::string> *prev_mapping =
            accumulated_mapping.empty() ? nullptr : &accumulated_mapping;
        DesensitizationResult out = desensitizer_.Apply(current, insp, prev_mapping);
        if (!out.success) {
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed in round=" << rounds
                                          << ": " << out.failure_reason << std::endl;
            return out;
        }

        InspectionContext ctx3 = ctx2;
        ctx3.collect_spans = false;
        InspectionResult verify = inspector_.Inspect(out.desensitized_request, ctx3);

        if (out.use_format_preserving && !out.mapping_table.empty()) {
            MergeDesensitizationMapping(ctx.session_id, out.mapping_table);

            for (const auto &[mock, real] : out.mapping_table) {
                accumulated_mapping.emplace(mock, real);
            }
        }

        if (verify.hit_rule_ids.empty()) {

            return out;
        }

        if (verify.sensitivity_level == SensitivityLevel::S2) {
            out.success = false;
            out.failure_reason = "s2_after_desensitize";
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed: S2 after desensitize in round=" << rounds << std::endl;
            return out;
        }

        if (!routing_config_.desensitization.iterative) {
            out.success = false;
            out.failure_reason = "still_hit_rules_no_iterative";
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed: rules still hit after desensitization (iterative disabled)" << std::endl;
            return out;
        }

        if (++rounds >= routing_config_.desensitization.max_rounds) {
            out.success = false;
            out.failure_reason = "max_rounds_reached";
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed: max_rounds=" << routing_config_.desensitization.max_rounds << " reached" << std::endl;
            return out;
        }

        current = out.desensitized_request;
        My_Log{} << "[GenieRoutingGateway] Desensitization round=" << rounds << " completed, still has hits, retrying..." << std::endl;
    }
}

DesensitizationResult GenieRoutingGateway::DesensitizeForCloudOnly(
    const json &request,
    const InspectionResult &inspection,
    const InspectionContext &ctx,
    const std::string &agent_type,
    const std::string &log_tag)
{

    json request_for_desensitize = FilterSystemMessagesForInspection(request, agent_type);
    json system_messages = json::array();

    if (agent_type == "main" && request.contains("messages") && request["messages"].is_array())
    {
        for (const auto &msg : request["messages"])
        {
            const std::string role = msg.value("role", "");
            if (role == "system" || role == "developer")
            {
                system_messages.push_back(msg);
            }
        }

        if (!system_messages.empty())
        {
            My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] Cloud-only mode (" << log_tag << "): skipped "
                << system_messages.size() << " system/developer message(s) from desensitization"
                << std::endl;
        }
    }

    DesensitizationResult result = Step4_Desensitize(request_for_desensitize, inspection, ctx);

    if (result.success && agent_type == "main" && !system_messages.empty() &&
        result.desensitized_request.contains("messages") &&
        result.desensitized_request["messages"].is_array())
    {
        json merged = json::array();
        for (const auto &sys_msg : system_messages)
        {
            merged.push_back(sys_msg);
        }
        for (const auto &msg : result.desensitized_request["messages"])
        {
            merged.push_back(msg);
        }
        result.desensitized_request["messages"] = merged;
        My_Log{} << "[GenieRoutingGateway] Cloud-only mode (" << log_tag << "): re-inserted "
                 << system_messages.size()
                 << " system/developer message(s) into desensitized request" << std::endl;
    }

    return result;
}

bool GenieRoutingGateway::Step5_Execute(const RouteDecision &decision,
                                 const json &original_request,
                                 const DesensitizationResult &desensitized,
                                 const httplib::Request &http_req,
                                 httplib::Response &http_res,
                                 ResponseDispatcher &dispatcher,
                                 ModelInputBuilder &input_builder,
                                 bool &handled_by_cloud,
                                 const std::string &user_session_id)
{
    handled_by_cloud = false;

    switch (decision.target)
    {
        case RouteTarget::POLICY_ERROR:
        {

            std::string code = decision.error_code.empty()
                ? "sensitive_content_local_unavailable" : decision.error_code;
            int http_status = (decision.error_http_status > 0) ? decision.error_http_status : 403;

            std::string message;
            if (code == "sensitive_content_local_unavailable") {
                message = "Security Policy Violation: High sensitivity content (S2). "
                          "Cloud processing is prohibited. Local model is unavailable.";
            } else if (code == "desensitization_failed") {
                message = "Security Policy Violation: Desensitization failed for sensitive content (S1). "
                          "Raw content cannot be sent to cloud, and local model is unavailable.";
            } else if (code == "all_routes_unavailable") {
                message = "Service Unavailable: Neither local nor cloud route is currently available.";
            } else if (code == "cloud_unavailable_no_fallback") {
                message = "Service Unavailable: This task is too complex (C2) for local processing. "
                          "Cloud model is required but not configured or currently unavailable. "
                          "Please configure a cloud model endpoint in service_config.json.";
            } else {
                message = "Security policy violation: " + decision.decision_reason;
            }

            My_Log{} << "[GenieRoutingGateway] Route=ERROR, code=" << code
                     << ", http_status=" << http_status << std::endl;
            SendPolicyViolationError(http_res, code, message, http_status);
            return false;
        }

        case RouteTarget::ENTERPRISE_CLOUD:
        {

            return ExecuteCloudTierCase(CloudTier::ENTERPRISE, decision.need_desensitize, original_request,
                                         desensitized, http_res, handled_by_cloud, user_session_id);
        }

        case RouteTarget::PUBLIC_CLOUD:
        {

            return ExecuteCloudTierCase(CloudTier::PUBLIC, decision.need_desensitize, original_request,
                                         desensitized, http_res, handled_by_cloud, user_session_id);
        }

        case RouteTarget::LOCAL:
        default:
        {

            if (!decision.local_available)
            {

                std::string code = decision.error_code.empty()
                    ? "sensitive_content_local_unavailable" : decision.error_code;
                int http_status = (decision.error_http_status > 0) ? decision.error_http_status : 403;

                std::string message;
                if (code == "desensitization_failed") {
                    message = "Security Policy Violation: Desensitization failed for sensitive content (S1). "
                              "Raw content cannot be sent to cloud, and local model is unavailable.";
                } else {
                    message = "Security Policy Violation: High sensitivity content (S2). "
                              "Cloud processing is prohibited. Local model is unavailable.";
                }

                My_Log{My_Log::Level::kError}
                    << "[GenieRoutingGateway] Route=LOCAL but local model is not available. "
                    << "Returning error (code=" << code << ", http_status=" << http_status << ")." << std::endl;
                SendPolicyViolationError(http_res, code, message, http_status);
                return false;
            }

            My_Log{} << "[GenieRoutingGateway] Route=LOCAL, continuing with local processing" << std::endl;
            handled_by_cloud = false;
            return true;
        }
    }
}

std::string GenieRoutingGateway::FormatAuditTimestampUtc()
{
    auto now = std::chrono::system_clock::now();
    auto time_t_now = std::chrono::system_clock::to_time_t(now);
    std::ostringstream ts_oss;
#ifdef _MSC_VER
    struct tm tm_buf;
    gmtime_s(&tm_buf, &time_t_now);
    ts_oss << std::put_time(&tm_buf, "%Y-%m-%dT%H:%M:%SZ");
#else
    ts_oss << std::put_time(std::gmtime(&time_t_now), "%Y-%m-%dT%H:%M:%SZ");
#endif
    return ts_oss.str();
}

AuditLogger::AuditRecord GenieRoutingGateway::NewAuditRecord(const InspectionContext &ctx,
                                                             RouteTarget route_target,
                                                             ComplexityLevel complexity_level,
                                                             int64_t latency_ms,
                                                             int http_status,
                                                             const std::string &route_reason)
{
    AuditLogger::AuditRecord record;
    record.request_id = ctx.request_id;
    record.session_id = ctx.session_id;
    record.timestamp = FormatAuditTimestampUtc();
    record.route_decision = route_target;
    record.complexity_level = complexity_level;
    record.latency_ms = latency_ms;
    record.http_status = http_status;
    record.route_reason = route_reason;
    return record;
}

void GenieRoutingGateway::FillAuditRecordFromInspection(AuditLogger::AuditRecord &record,
                                                        const InspectionResult &inspection,
                                                        const DesensitizationResult &desensitized,
                                                        bool desensitized_flag)
{
    record.sensitivity_level = inspection.sensitivity_level;
    record.desensitized = desensitized_flag;
    record.desensitize_success = desensitized_flag && desensitized.success;
    record.desensitize_strategies = desensitized.applied_strategies;
    record.desensitize_failure_reason = desensitized.failure_reason;
    record.hit_rule_ids = inspection.hit_rule_ids;
    record.hit_categories = inspection.hit_categories;
    record.rule_engine_failed = inspection.rule_engine_failed;
    record.tool_output_escalation = inspection.tool_output_escalation;
}

void GenieRoutingGateway::FillAuditCloudEndpoint(AuditLogger::AuditRecord &record, RouteTarget route_target)
{
    if (route_target == RouteTarget::PUBLIC_CLOUD)
    {
        std::string ep = cloud_client_.GetLastUsedEndpoint();
        record.cloud_endpoint = ep.empty() ? "public_cloud_model" : ep;
        record.cloud_tier = "public_cloud";
    }
    else if (route_target == RouteTarget::ENTERPRISE_CLOUD)
    {
        std::string ep = enterprise_cloud_client_.GetLastUsedEndpoint();
        record.cloud_endpoint = ep.empty() ? "enterprise_cloud_model" : ep;
        record.cloud_tier = "enterprise_cloud";
    }
    else if (route_target == RouteTarget::POLICY_ERROR)
    {
        record.cloud_tier = "error";
    }
}

void GenieRoutingGateway::InsertStickySession(const std::string &session_id,
                                              RouteTarget target,
                                              SensitivityLevel sensitivity,
                                              int fallback_msg_count)
{
    std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
    if (!sticky_sessions_.empty() &&
        sticky_sessions_.find(session_id) == sticky_sessions_.end() &&
        (int)sticky_sessions_.size() >= routing_config_.sticky_routing.max_sessions)
    {
        auto oldest = sticky_sessions_.begin();
        for (auto it = sticky_sessions_.begin(); it != sticky_sessions_.end(); ++it)
        {
            if (it->second.last_used < oldest->second.last_used)
            {
                oldest = it;
            }
        }
        sticky_sessions_.erase(oldest);
    }

    const auto now = std::chrono::steady_clock::now();
    StickyRouteEntry entry;
    entry.target = target;
    entry.sensitivity = sensitivity;
    entry.expires_at = now + std::chrono::seconds(routing_config_.sticky_routing.ttl_seconds);
    entry.last_used = now;
    entry.fallback_msg_count = fallback_msg_count;
    sticky_sessions_[session_id] = entry;
}

json GenieRoutingGateway::FilterRequestForInspection(const json &request,
                                                     std::string &agent_type,
                                                     const char *log_tag)
{
    agent_type = DetectAgentTypeFromRequest(request);
    json filtered_request = FilterSystemMessagesForInspection(request, agent_type);
    if (agent_type == "main" && request.contains("messages") && filtered_request.contains("messages"))
    {
        size_t orig = request["messages"].size();
        size_t filtered = filtered_request["messages"].size();
        if (orig != filtered)
        {
            My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] " << log_tag << ": Main agent skipped "
                << (orig - filtered) << " system/developer message(s) from security inspection" << std::endl;
        }
    }
    return filtered_request;
}

void GenieRoutingGateway::Step6_Audit(const InspectionContext &ctx,
                               const InspectionResult &inspection,
                               const ComplexityResult &complexity,
                               const RouteDecision &decision,
                               const DesensitizationResult &desensitized,
                               int64_t latency_ms,
                               int http_status,
                               bool sticky_route_hit,
                               int64_t cloud_prompt_tokens,
                               int64_t cloud_completion_tokens,
                               int64_t cloud_total_tokens,
                               bool incremental_check_used,
                               int incremental_new_messages,
                               int total_messages_count,
                               const std::string &incremental_fallback_reason)
{
    AuditLogger::AuditRecord record = NewAuditRecord(ctx, decision.target, complexity.complexity_level,
                                                     latency_ms, http_status, decision.decision_reason);
    FillAuditRecordFromInspection(record, inspection, desensitized, decision.need_desensitize);
    record.oversized_field_warning = inspection.oversized_field_warning;
    record.oversized_fields_count = inspection.oversized_fields_count;

    {
        bool use_aux = routing_config_.sensitivity_detection.use_local_model_fallback ||
                       routing_config_.complexity.use_local_model_fallback;
        int latency_threshold_ms = use_aux ? 300000 : 300;
        record.latency_warning = (latency_ms > latency_threshold_ms);
        if (record.latency_warning)
        {
            My_Log{My_Log::Level::kWarning}
                << "[GenieRoutingGateway] Latency warning: latency_ms=" << latency_ms
                << " exceeds P95 target=" << latency_threshold_ms << "ms" << std::endl;
        }
    }

    FillAuditCloudEndpoint(record, decision.target);

    record.sticky_route_hit = sticky_route_hit;

    record.cloud_prompt_tokens     = cloud_prompt_tokens;
    record.cloud_completion_tokens = cloud_completion_tokens;
    record.cloud_total_tokens      = cloud_total_tokens;

    record.incremental_check_used    = incremental_check_used;
    record.incremental_new_messages  = incremental_new_messages;
    record.total_messages_count      = total_messages_count;
    record.incremental_fallback_reason = incremental_fallback_reason;

    audit_logger_.Log(record);
}

std::string GenieRoutingGateway::DetectAgentTypeFromRequest(const json& request)
{

    std::string system_prompt;
    if (request.contains("messages") && request["messages"].is_array()) {
        for (const auto& msg : request["messages"]) {
            if (msg.value("role", "") == "system") {
                system_prompt = SecurityUtils::ExtractMessageContentText(msg);
                break;
            }
        }
    }

    bool is_main_agent = (system_prompt.find("agent=main") != std::string::npos);

    std::string agent_type = is_main_agent ? "main" : "sub";

    My_Log{My_Log::Level::kDebug} << "[DetectAgentType] Detected: " << agent_type
                                   << " (agent=main " << (is_main_agent ? "found" : "not found")
                                   << " in system prompt)" << std::endl;

    return agent_type;
}
