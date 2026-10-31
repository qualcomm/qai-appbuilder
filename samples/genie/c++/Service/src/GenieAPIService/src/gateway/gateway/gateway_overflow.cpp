//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include <chrono>
#include <mutex>

bool GenieRoutingGateway::HandleLocalOutputOverflow(const json &request,
                                                    const httplib::Request &http_req,
                                                    httplib::Response &http_res,
                                                    httplib::DataSink *sink)
{
    My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] HandleLocalOutputOverflow triggered, sink="
                                 << (sink ? "provided(stream_fallback)" : "null") << std::endl;

    auto start_time = std::chrono::high_resolution_clock::now();

    InspectionContext ctx;
    ctx.request_id = GenerateRequestId();

    ResolveServerSessionIds(request, ctx);

    std::string agent_type;
    json request_for_inspection = FilterRequestForInspection(request, agent_type, "HandleLocalOutputOverflow");

    InspectionResult inspection = Step1_Inspect(request_for_inspection, ctx);

    bool fallback_to_cloud = false;

    bool public_cloud_available = cloud_client_.IsAvailable();

    bool enterprise_cloud_available = enterprise_cloud_client_.IsAvailable();

    CloudTier fallback_cloud_tier = enterprise_cloud_available ? CloudTier::ENTERPRISE : CloudTier::PUBLIC;
    bool any_cloud_available = enterprise_cloud_available || public_cloud_available;

    DesensitizationResult desensitized;
    desensitized.success = false;
    bool need_desensitize = false;

    if (inspection.sensitivity_level == SensitivityLevel::S2)
    {
        My_Log{My_Log::Level::kWarning} << "[GenieRoutingGateway] Overflow on S2 content. Prohibiting cloud fallback." << std::endl;
        fallback_to_cloud = false;
    }
    else if (!any_cloud_available)
    {
        My_Log{My_Log::Level::kWarning} << "[GenieRoutingGateway] Overflow but all clouds are unavailable. Cannot fallback." << std::endl;
        fallback_to_cloud = false;
    }
    else
    {
        if (inspection.sensitivity_level == SensitivityLevel::S1)
        {

            json request_for_desensitize = FilterSystemMessagesForInspection(request, agent_type);
            desensitized = Step4_Desensitize(request_for_desensitize, inspection, ctx);
            if (desensitized.success)
            {
                fallback_to_cloud = true;
                need_desensitize = true;
            }
            else
            {
                My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed for S1 content. Cannot fallback to cloud." << std::endl;
                fallback_to_cloud = false;
            }
        }
        else
        {
            fallback_to_cloud = true;
            need_desensitize = false;
        }
    }

    bool is_stream = false;
    if (request.contains("stream") && request["stream"].is_boolean())
    {
        is_stream = request["stream"].get<bool>();
    }

    bool intended_for_cloud = fallback_to_cloud;

    if (is_stream && fallback_to_cloud && sink == nullptr)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] HandleLocalOutputOverflow: stream=true, sink=null, "
            << "skipping cloud retry (response already started, no sink for inline fallback). Audit only." << std::endl;
        fallback_to_cloud = false;
    }

    bool handled_by_cloud = false;
    if (fallback_to_cloud)
    {

        json cloud_request = (need_desensitize && desensitized.success)
                             ? desensitized.desensitized_request
                             : request;

        if (routing_config_.s2_turn_cleaning.enabled)
        {
            int overflow_turn_start = -1;
            if (cloud_request.contains("messages") && cloud_request["messages"].is_array()) {
                const auto &overflow_msgs = cloud_request["messages"];
                for (int i = (int)overflow_msgs.size() - 1; i >= 0; --i) {
                    if (overflow_msgs[i].value("role", "") == "user") {
                        overflow_turn_start = i;
                        break;
                    }
                }
            }
            cloud_request = CleanS2TurnsForCloud(
                cloud_request, ctx.global_session_id, overflow_turn_start,
                inspection.sensitivity_level, "LocalOutputOverflow");
        }

        if (routing_config_.fallback.clean_local_history_on_fallback)
        {
            cloud_request = CleanLocalHistoryForCloudFallback(cloud_request);
        }

        int original_status = http_res.status;
        std::string original_body = http_res.body;

        bool success = ExecuteCloudRequest(cloud_request, is_stream, http_res, handled_by_cloud,
                                           ctx.session_id, sink, "cloud_fallback", "Switching to cloud model...",
                                           fallback_cloud_tier);

        if (!success || !handled_by_cloud)
        {
            My_Log{My_Log::Level::kWarning} << "[GenieRoutingGateway] Cloud fallback failed during post-execution. Restoring local truncated response." << std::endl;
            fallback_to_cloud = false;

            http_res.status = original_status;
            http_res.body = original_body;

            if (!is_stream) {
                http_res.set_content(original_body, ResponseDispatcher::MIMETYPE_JSON);
            }
        }
        else
        {

            if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
            {
                int fallback_msg_count = request.contains("messages") && request["messages"].is_array()
                                         ? (int)request["messages"].size() : 0;
                InsertStickySession(ctx.session_id,
                                    (fallback_cloud_tier == CloudTier::ENTERPRISE)
                                        ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD,
                                    inspection.sensitivity_level,
                                    fallback_msg_count);

                My_Log{} << "[GenieRoutingGateway] Sticky route set after overflow fallback for session="
                         << ctx.session_id << ", fallback_msg_count=" << fallback_msg_count << std::endl;
            }
        }
    }

    auto end_time = std::chrono::high_resolution_clock::now();
    int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end_time - start_time).count();

    ComplexityResult fake_complexity;
    fake_complexity.complexity_level = ComplexityLevel::C0;

    RouteDecision decision;

    if (intended_for_cloud) {
        decision.target = (fallback_cloud_tier == CloudTier::ENTERPRISE)
            ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
    } else {
        decision.target = RouteTarget::LOCAL;
    }
    decision.sensitivity = inspection.sensitivity_level;
    decision.complexity = ComplexityLevel::C0;
    decision.enterprise_cloud_available = enterprise_cloud_available;
    decision.public_cloud_available = public_cloud_available;
    decision.local_available = true;
    decision.need_desensitize = need_desensitize;

    if (is_stream && intended_for_cloud && !fallback_to_cloud)
    {
        decision.decision_reason = "post_execution_fallback_stream_skipped";
    }
    else
    {
        decision.decision_reason = "post_execution_fallback";
    }

    AuditLogger::AuditRecord record = NewAuditRecord(ctx, decision.target, decision.complexity, latency_ms,
                                                     fallback_to_cloud ? http_res.status : 200,
                                                     decision.decision_reason);
    FillAuditRecordFromInspection(record, inspection, desensitized, decision.need_desensitize);
    if (handled_by_cloud) {
        FillAuditCloudEndpoint(record, decision.target);
    }
    record.keywords_dict_reloaded = inspection.keywords_dict_reloaded;
    record.keywords_dict_rules_count = inspector_.GetKeywordsRulesCount();

    record.local_output_overflow = true;

    audit_logger_.Log(record);

    return fallback_to_cloud;
}

bool GenieRoutingGateway::HandleLocalInputOverflow(const json &request,
                                                    const httplib::Request &http_req,
                                                    httplib::Response &http_res,
                                                    ResponseDispatcher &dispatcher)
{

    if (!routing_config_.enabled)
    {
        return false;
    }

    My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] HandleLocalInputOverflow triggered "
                                  << "(local input overflow: compressed prompt still exceeds context window)" << std::endl;

    auto start_time = std::chrono::high_resolution_clock::now();

    InspectionContext ctx;
    ctx.request_id = GenerateRequestId();
    ctx.is_internal_inspection = false;

    ResolveServerSessionIds(request, ctx);

    bool input_public_cloud_available = cloud_client_.IsAvailable();
    bool input_enterprise_cloud_available = enterprise_cloud_client_.IsAvailable();
    bool input_any_cloud_available = input_enterprise_cloud_available || input_public_cloud_available;

    CloudTier input_fallback_cloud_tier = input_enterprise_cloud_available
        ? CloudTier::ENTERPRISE : CloudTier::PUBLIC;

    if (!input_any_cloud_available)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] Input overflow but cloud is unavailable. "
            << "Skipping security check and returning error immediately." << std::endl;

        bool within_retry_limit = CheckAndIncrementInputOverflowRetry(ctx.global_session_id);
        int http_status_cloud_unavail = within_retry_limit ? 503 : 422;
        std::string message_cloud_unavail = within_retry_limit
            ? "Service Unavailable: Local input overflow detected and cloud model is not available. "
              "Neither local nor cloud route can handle this request."
            : "Unprocessable Content: Local input overflow and cloud model is persistently unavailable. "
              "Maximum retry attempts reached. Please reduce the request size or try again later.";

        SendPolicyViolationError(http_res,
            "all_routes_unavailable",
            message_cloud_unavail,
            http_status_cloud_unavail);

        auto end_time_early = std::chrono::high_resolution_clock::now();
        int64_t latency_ms_early = std::chrono::duration_cast<std::chrono::milliseconds>(
            end_time_early - start_time).count();

        AuditLogger::AuditRecord record_early = NewAuditRecord(ctx, RouteTarget::POLICY_ERROR, ComplexityLevel::C0,
                                                               latency_ms_early, http_status_cloud_unavail,
                                                               "all_routes_unavailable");
        record_early.keywords_dict_rules_count = inspector_.GetKeywordsRulesCount();
        record_early.local_input_overflow = true;
        audit_logger_.Log(record_early);
        return true;
    }

    std::string agent_type;
    json request_for_inspection = FilterRequestForInspection(request, agent_type, "HandleLocalInputOverflow");

    InspectionResult inspection = Step1_Inspect(request_for_inspection, ctx);

    auto emit_audit = [&](RouteTarget route_target,
                          bool desensitized_flag,
                          const DesensitizationResult &desens,
                          int http_status,
                          const std::string &reason,
                          int64_t latency_ms_val)
    {
        AuditLogger::AuditRecord record = NewAuditRecord(ctx, route_target, ComplexityLevel::C0,
                                                         latency_ms_val, http_status, reason);
        FillAuditRecordFromInspection(record, inspection, desens, desensitized_flag);
        FillAuditCloudEndpoint(record, route_target);
        record.keywords_dict_reloaded = inspection.keywords_dict_reloaded;
        record.keywords_dict_rules_count = inspector_.GetKeywordsRulesCount();

        record.local_input_overflow = true;

        audit_logger_.Log(record);
    };

    if (inspection.sensitivity_level == SensitivityLevel::S2)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] Input overflow on S2 content. "
            << "Attempting desensitization fallback..." << std::endl;

        json request_for_spans = FilterSystemMessagesForInspection(request, agent_type);
        InspectionContext ctx_spans;
        ctx_spans.request_id = ctx.request_id;
        ctx_spans.session_id = ctx.session_id;
        ctx_spans.rule_engine_only = true;
        ctx_spans.collect_spans = true;
        ctx_spans.is_internal_inspection = false;
        InspectionResult inspection_with_spans = inspector_.Inspect(request_for_spans, ctx_spans);

        DesensitizationResult desens_fallback = desensitizer_.Apply(request_for_spans, inspection_with_spans);

        if (desens_fallback.success)
        {

            InspectionContext ctx_verify;
            ctx_verify.request_id = ctx.request_id;
            ctx_verify.session_id = ctx.session_id;
            ctx_verify.rule_engine_only = true;
            ctx_verify.collect_spans = false;
            ctx_verify.is_internal_inspection = false;
            InspectionResult verify = inspector_.Inspect(desens_fallback.desensitized_request, ctx_verify);

            if (verify.sensitivity_level == SensitivityLevel::S2)
            {

                desens_fallback.success = false;
                desens_fallback.failure_reason = "s2_after_desensitize";
                My_Log{My_Log::Level::kError}
                    << "[GenieRoutingGateway] S2 desensitization fallback: content still S2 after desensitize. "
                    << "Cannot route to cloud." << std::endl;
            }
            else
            {
                My_Log{} << "[GenieRoutingGateway] S2 desensitization fallback: content downgraded to "
                         << to_string(verify.sensitivity_level) << " after desensitize." << std::endl;
            }
        }

        if (desens_fallback.success)
        {

            My_Log{} << "[GenieRoutingGateway] S2 desensitization fallback succeeded on input overflow. "
                     << "Downgrading to S1 and routing to cloud." << std::endl;

            json cloud_request_s2 = desens_fallback.desensitized_request;

            if (routing_config_.s2_turn_cleaning.enabled)
            {
                int overflow_turn_start_s2 = -1;
                if (cloud_request_s2.contains("messages") && cloud_request_s2["messages"].is_array()) {
                    const auto &s2_msgs = cloud_request_s2["messages"];
                    for (int i = (int)s2_msgs.size() - 1; i >= 0; --i) {
                        if (s2_msgs[i].value("role", "") == "user") {
                            overflow_turn_start_s2 = i;
                            break;
                        }
                    }
                }
                cloud_request_s2 = CleanS2TurnsForCloud(
                    cloud_request_s2, ctx.global_session_id, overflow_turn_start_s2,
                    SensitivityLevel::S1,
                    "LocalInputOverflow-S2-Desensitized");
            }

            bool is_stream_s2 = request.contains("stream") && request["stream"].is_boolean()
                                && request["stream"].get<bool>();
            bool handled_by_cloud_s2 = false;

            bool success_s2 = ExecuteCloudRequest(cloud_request_s2, is_stream_s2, http_res,
                                                   handled_by_cloud_s2, ctx.session_id, nullptr,
                                                   "cloud_fallback", "Switching to cloud model...",
                                                   input_fallback_cloud_tier);

            auto end_time_s2 = std::chrono::high_resolution_clock::now();
            int64_t latency_ms_s2 = std::chrono::duration_cast<std::chrono::milliseconds>(
                end_time_s2 - start_time).count();

            if (!success_s2 || !handled_by_cloud_s2)
            {
                My_Log{My_Log::Level::kError}
                    << "[GenieRoutingGateway] Cloud routing failed after S2 desensitization fallback. "
                    << "Returning 503/422." << std::endl;
                if (!success_s2)
                {

                    SendPolicyViolationError(http_res, "all_routes_unavailable",
                        "Bad Gateway: Cloud request failed after S2 desensitization. "
                        "The upstream cloud service is unavailable.", 502);
                }
                else
                {

                    bool within_retry_limit = CheckAndIncrementInputOverflowRetry(ctx.global_session_id);
                    int http_status_s2_fail = within_retry_limit ? 503 : 422;
                    std::string message_s2_fail = within_retry_limit
                        ? "Service Unavailable: Cloud routing failed after S2 desensitization. "
                          "Please try again later."
                        : "Unprocessable Content: Cloud routing persistently failed after S2 desensitization. "
                          "Maximum retry attempts reached. Please reduce the request size or try again later.";
                    SendPolicyViolationError(http_res, "all_routes_unavailable",
                        message_s2_fail, http_status_s2_fail);
                }
                emit_audit(RouteTarget::POLICY_ERROR, true, desens_fallback, http_res.status,
                           "cloud_routing_failed_after_s2_desensitization", latency_ms_s2);
                return true;
            }

            if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
            {
                InsertStickySession(ctx.session_id,
                                    (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
                                        ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD,
                                    SensitivityLevel::S1,
                                    request.contains("messages") && request["messages"].is_array()
                                        ? (int)request["messages"].size() : 0);
            }

            RouteTarget s2_route_target = (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
                ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
            emit_audit(s2_route_target, true, desens_fallback, http_res.status,
                       "local_input_overflow_s2_desensitized_routed_to_cloud", latency_ms_s2);
            return true;
        }
        else
        {

            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] S2 desensitization fallback failed on input overflow. "
                << "Cannot route to cloud. Failure reason: " << desens_fallback.failure_reason << std::endl;

            std::string friendly_message =
                "Service Unavailable: The request contains highly sensitive content (S2) and exceeds "
                "the local model's context window. We attempted to desensitize the content to enable "
                "cloud processing, but the desensitization failed. This issue cannot be resolved automatically. "
                "Please try one of the following:\n"
                "1. Reduce the request size by removing unnecessary context or history\n"
                "2. Split your request into smaller parts\n"
                "3. Remove or mask sensitive information manually before submitting";

            SendPolicyViolationError(http_res,
                "local_input_overflow_s2_desensitization_failed",
                friendly_message,
                422);

            auto end_time = std::chrono::high_resolution_clock::now();
            int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                end_time - start_time).count();
            emit_audit(RouteTarget::POLICY_ERROR, true, desens_fallback, 422,
                       "local_input_overflow_s2_desensitization_failed", latency_ms);
            return true;
        }
    }

    DesensitizationResult desensitized;
    bool need_desensitize = false;

    if (inspection.sensitivity_level == SensitivityLevel::S1)
    {

        json request_for_desensitize = FilterSystemMessagesForInspection(request, agent_type);
        desensitized = Step4_Desensitize(request_for_desensitize, inspection, ctx);
        if (!desensitized.success)
        {
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Desensitization failed for S1 content on input overflow. "
                << "Returning 422 (desensitization_failed_on_input_overflow)." << std::endl;

            SendPolicyViolationError(http_res,
                "desensitization_failed_on_input_overflow",
                "Unprocessable Content: Local input overflow detected but desensitization failed "
                "for sensitive content (S1). Cannot route to cloud without desensitization. "
                "Please reduce the request size or remove sensitive information manually.",
                422);

            auto end_time = std::chrono::high_resolution_clock::now();
            int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                end_time - start_time).count();
            emit_audit(RouteTarget::POLICY_ERROR, true, desensitized, 422,
                       "desensitization_failed_on_input_overflow", latency_ms);
            return true;
        }
        need_desensitize = true;
        My_Log{} << "[GenieRoutingGateway] S1 content desensitized successfully for input overflow fallback." << std::endl;
    }

    json cloud_request = (need_desensitize && desensitized.success)
                         ? desensitized.desensitized_request
                         : request;

    if (routing_config_.s2_turn_cleaning.enabled)
    {
        int overflow_turn_start = -1;
        if (cloud_request.contains("messages") && cloud_request["messages"].is_array()) {
            const auto &overflow_msgs = cloud_request["messages"];
            for (int i = (int)overflow_msgs.size() - 1; i >= 0; --i) {
                if (overflow_msgs[i].value("role", "") == "user") {
                    overflow_turn_start = i;
                    break;
                }
            }
        }
        cloud_request = CleanS2TurnsForCloud(
            cloud_request, ctx.global_session_id, overflow_turn_start,
            inspection.sensitivity_level, "LocalInputOverflow");
    }

    if (routing_config_.fallback.clean_local_history_on_fallback)
    {
        cloud_request = CleanLocalHistoryForCloudFallback(cloud_request);
    }

    bool is_stream = false;
    if (request.contains("stream") && request["stream"].is_boolean())
    {
        is_stream = request["stream"].get<bool>();
    }

    My_Log{} << "[GenieRoutingGateway] Routing to cloud due to local input overflow, "
             << "sensitivity=" << to_string(inspection.sensitivity_level)
             << ", stream=" << is_stream << std::endl;

    bool handled_by_cloud = false;

    bool success = ExecuteCloudRequest(cloud_request, is_stream, http_res, handled_by_cloud,
                                       ctx.session_id, nullptr,
                                       "cloud_fallback", "Switching to cloud model...",
                                       input_fallback_cloud_tier);

    auto end_time = std::chrono::high_resolution_clock::now();
    int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        end_time - start_time).count();

    if (!success || !handled_by_cloud)
    {

        if (success && !handled_by_cloud)
        {

            bool within_retry_limit = CheckAndIncrementInputOverflowRetry(ctx.global_session_id);
            int http_status_cloud_fail = within_retry_limit ? 503 : 422;
            std::string message_cloud_fail = within_retry_limit
                ? "Service Unavailable: Cloud routing failed and local processing is unavailable "
                  "(local input overflow). Please try again later."
                : "Unprocessable Content: Cloud routing persistently failed and local processing is "
                  "unavailable (local input overflow). Maximum retry attempts reached. "
                  "Please reduce the request size or try again later.";

            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Cloud fallback failed on input overflow "
                << "(cloud failed, local unavailable). Returning " << http_status_cloud_fail << "." << std::endl;
            SendPolicyViolationError(http_res,
                "all_routes_unavailable",
                message_cloud_fail,
                http_status_cloud_fail);
        }

        emit_audit(RouteTarget::POLICY_ERROR, need_desensitize, desensitized,
                   http_res.status, "cloud_routing_failed_on_input_overflow", latency_ms);
        return true;
    }

    ResetInputOverflowRetry(ctx.global_session_id);
    My_Log{} << "[GenieRoutingGateway] Cloud routing succeeded for input overflow request." << std::endl;

    if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
    {
        int fallback_msg_count = request.contains("messages") && request["messages"].is_array()
                                 ? (int)request["messages"].size() : 0;
        InsertStickySession(ctx.session_id,
                            (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
                                ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD,
                            SensitivityLevel::S0,
                            fallback_msg_count);
        My_Log{} << "[GenieRoutingGateway] Sticky route set after input overflow fallback for session="
                 << ctx.session_id << ", fallback_msg_count=" << fallback_msg_count << std::endl;
    }

    RouteTarget input_route_target = (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
        ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
    emit_audit(input_route_target, need_desensitize, desensitized,
               http_res.status, "local_input_overflow_routed_to_cloud", latency_ms);
    return true;
}
