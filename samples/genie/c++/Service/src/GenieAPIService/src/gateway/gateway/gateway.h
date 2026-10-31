//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GATEWAY_H
#define GATEWAY_H

#include "../security/content_security_types.h"
#include "../security/content_security_inspector.h"
#include "../security/desensitizer.h"
#include "../security/task_complexity_evaluator.h"
#include "../routing/model_router.h"
#include "../cloud/cloud_model_client.h"
#include "../audit/audit_logger.h"
#include "../../model/model_config.h"
#include "../../chat_request_handler/prompt_preparation_service.h"
#include <httplib.h>
#include <string>
#include <memory>
#include <atomic>
#include <functional>
#include <chrono>
#include <unordered_map>
#include <mutex>

class ResponseDispatcher;
class ModelInputBuilder;

struct StickyRouteEntry {
    RouteTarget target = RouteTarget::LOCAL;
    SensitivityLevel sensitivity = SensitivityLevel::S0;
    std::chrono::steady_clock::time_point expires_at;
    std::chrono::steady_clock::time_point last_used;

    int fallback_msg_count = 0;
};

struct ServerSessionEntry {
    std::string user_session_id;
    std::string global_session_id;
    std::chrono::steady_clock::time_point last_updated;

    std::unordered_map<std::string, std::string> desensitization_mapping;
};

struct CloudRateLimitCounter {
    int inference_count = 0;
    int64_t prompt_tokens = 0;
    int64_t completion_tokens = 0;
    int64_t total_tokens = 0;
    std::chrono::steady_clock::time_point last_updated;
};

enum class CheckScope {
    FULL,
    INCREMENTAL
};

struct SessionSecurityState {

    SensitivityLevel max_sensitivity_seen = SensitivityLevel::S0;

    int checked_message_count = 0;

    std::chrono::steady_clock::time_point last_updated;

    bool incremental_mode_enabled = true;

    std::string fallback_reason;

    struct S2TurnRange {
        int start_idx = 0;
        int end_idx = 0;
    };
    std::vector<S2TurnRange> s2_turn_ranges;
};

enum class CloudTier {
    ENTERPRISE,
    PUBLIC
};

enum class ClientSource {
    UNKNOWN,
    QAI_AGENT_FORGE,
    OPENCLAW
};

enum class PromptProcessingPolicy {
    OPTIMIZED,
    PASS_THROUGH
};

class GenieRoutingGateway
{
public:
    GenieRoutingGateway(IModelConfig &model_config,
               const RoutingConfig &routing_config,
               const CloudModelConfig &cloud_config,
               const EnterpriseCloudModelConfig &enterprise_cloud_config);

    ~GenieRoutingGateway();

    bool HandleChatCompletion(const json &request,
                               const httplib::Request &http_req,
                               httplib::Response &http_res,
                               ResponseDispatcher &dispatcher,
                               ModelInputBuilder &input_builder,
                               bool &handled_by_cloud);

    bool HandleLocalOutputOverflow(const json &request,
                                   const httplib::Request &http_req,
                                   httplib::Response &http_res,
                                   httplib::DataSink *sink = nullptr);

    bool HandleLocalInputOverflow(const json &request,
                                  const httplib::Request &http_req,
                                  httplib::Response &http_res,
                                  ResponseDispatcher &dispatcher);

    void ResolveServerSessionIds(const json &request, InspectionContext &ctx);

private:

    InspectionResult Step1_Inspect(const json &request,
                                    const InspectionContext &ctx);

    ComplexityResult Step2_EvaluateComplexity(const json &request,
                                               const InspectionContext &ctx);

    RouteDecision Step3_Route(const InspectionResult &inspection,
                               const ComplexityResult &complexity,
                               const std::string &agent_type = "main");

    DesensitizationResult Step4_Desensitize(const json &request,
                                             const InspectionResult &inspection,
                                             const InspectionContext &ctx);

    bool Step5_Execute(const RouteDecision &decision,
                        const json &original_request,
                        const DesensitizationResult &desensitized,
                        const httplib::Request &http_req,
                        httplib::Response &http_res,
                        ResponseDispatcher &dispatcher,
                        ModelInputBuilder &input_builder,
                        bool &handled_by_cloud,
                        const std::string &user_session_id);

    bool ExecuteCloudRequest(const json &cloud_request,
                             bool is_stream,
                             httplib::Response &http_res,
                             bool &handled_by_cloud,
                             const std::string &user_session_id,
                             httplib::DataSink *sink = nullptr,
                             const std::string &initial_status = "inference",
                             const std::string &initial_message = "正在云端推理...",
                             CloudTier cloud_tier = CloudTier::PUBLIC);

    bool ExecuteCloudTierCase(CloudTier tier,
                               bool need_desensitize,
                               const json &original_request,
                               const DesensitizationResult &desensitized,
                               httplib::Response &http_res,
                               bool &handled_by_cloud,
                               const std::string &user_session_id);

    void Step6_Audit(const InspectionContext &ctx,
                      const InspectionResult &inspection,
                      const ComplexityResult &complexity,
                      const RouteDecision &decision,
                      const DesensitizationResult &desensitized,
                      int64_t latency_ms,
                      int http_status = 0,
                      bool sticky_route_hit = false,
                      int64_t cloud_prompt_tokens = 0,
                      int64_t cloud_completion_tokens = 0,
                      int64_t cloud_total_tokens = 0,
                      bool incremental_check_used = false,
                      int incremental_new_messages = 0,
                      int total_messages_count = 0,
                      const std::string &incremental_fallback_reason = "");

    void SendPolicyViolationError(httplib::Response &res,
                                   const std::string &code,
                                   const std::string &message,
                                   int http_status = 403,
                                   bool is_stream = false);

    bool HandleCloudResponse(const json &cloud_response,
                              httplib::Response &http_res);

    static std::string GenerateRequestId();

    static std::string FormatAuditTimestampUtc();

    static AuditLogger::AuditRecord NewAuditRecord(const InspectionContext &ctx,
                                                   RouteTarget route_target,
                                                   ComplexityLevel complexity_level,
                                                   int64_t latency_ms,
                                                   int http_status,
                                                   const std::string &route_reason);

    static void FillAuditRecordFromInspection(AuditLogger::AuditRecord &record,
                                              const InspectionResult &inspection,
                                              const DesensitizationResult &desensitized,
                                              bool desensitized_flag);

    void FillAuditCloudEndpoint(AuditLogger::AuditRecord &record, RouteTarget route_target);

    void InsertStickySession(const std::string &session_id,
                             RouteTarget target,
                             SensitivityLevel sensitivity,
                             int fallback_msg_count);

    json FilterRequestForInspection(const json &request,
                                    std::string &agent_type,
                                    const char *log_tag);

    static json CleanLocalHistoryForCloudFallback(const json &request, int fallback_boundary = 0);

    static json PurgeS2TurnsFromHistory(
        const json &request,
        const std::vector<SessionSecurityState::S2TurnRange> &s2_turn_ranges,
        bool log_details = false);

    json CleanS2TurnsForCloud(
        const json &request,
        const std::string &global_session_id,
        int current_turn_start,
        SensitivityLevel new_max_sensitivity,
        const std::string &log_tag);

    DesensitizationResult DesensitizeForCloudOnly(
        const json &request,
        const InspectionResult &inspection,
        const InspectionContext &ctx,
        const std::string &agent_type,
        const std::string &log_tag);

    static json FilterSystemMessagesForInspection(const json &request, const std::string &agent_type);

    void MergeDesensitizationMapping(const std::string &session_id,
                                      const std::unordered_map<std::string, std::string> &mapping);

    std::unordered_map<std::string, std::string> GetDesensitizationMapping(const std::string &session_id) const;

    void RestoreCloudResponse(json &response,
                              const std::unordered_map<std::string, std::string> &mapping) const;

    std::string RestoreStringFully(const std::string &text,
                                   const std::unordered_map<std::string, std::string> &mapping) const;

    IModelConfig &model_config_;
    ContentSecurityInspector inspector_;
    Desensitizer desensitizer_;
    TaskComplexityEvaluator complexity_evaluator_;
    ModelRouter router_;
    CloudModelClient cloud_client_;
    CloudModelClient enterprise_cloud_client_;
    AuditLogger audit_logger_;
    RoutingConfig routing_config_;
    CloudModelConfig cloud_config_;
    EnterpriseCloudModelConfig enterprise_cloud_config_;
    LocalModelConfig local_model_config_;

    PromptPreparationService prompt_prep_service_;

    std::unordered_map<std::string, StickyRouteEntry> sticky_sessions_;
    std::mutex sticky_sessions_mutex_;

    std::unordered_map<std::string, CloudRateLimitCounter> rate_limit_counters_;
    std::mutex rate_limit_mutex_;

    bool CheckAndIncrementInferenceCount(const std::string &session_id,
                                          httplib::Response &http_res,
                                          CloudTier cloud_tier = CloudTier::PUBLIC);

    bool UpdateAndCheckTokenCount(const std::string &session_id,
                                   int64_t tokens_used,
                                   httplib::Response &http_res,
                                   int64_t prompt_tokens = 0,
                                   int64_t completion_tokens = 0,
                                   CloudTier cloud_tier = CloudTier::PUBLIC);

    std::shared_ptr<std::atomic<bool>> alive_;

    std::unordered_map<std::string, SessionSecurityState> session_security_states_;
    mutable std::mutex session_security_mutex_;

    struct InputOverflowRetryEntry {
        int retry_count = 0;
        std::chrono::steady_clock::time_point last_retry;
    };
    std::unordered_map<std::string, InputOverflowRetryEntry> input_overflow_retries_;
    std::mutex input_overflow_retries_mutex_;

    bool CheckAndIncrementInputOverflowRetry(const std::string &session_id);

    void ResetInputOverflowRetry(const std::string &session_id);

    CheckScope DetermineCheckScope(const json &request,
                                   const std::string &global_session_id,
                                   int &out_last_checked_count);

    static json ExtractIncrementalContent(const json &request, int last_checked_count);

    static bool DetectSensitiveReference(const json &incremental_messages);

    void UpdateSessionSecurityState(const std::string &global_session_id,
                                    const InspectionResult &inspection,
                                    int current_message_count,
                                    int turn_start_idx = -1);

    std::unordered_map<std::string, ServerSessionEntry> server_sessions_;
    mutable std::mutex server_sessions_mutex_;

    static std::string ComputeHistoryFingerprint(const json &messages);

    static std::string GenerateSessionId();

    std::string DetectAgentTypeFromRequest(const json& request);

    static ClientSource DetectClientSource(const httplib::Request &http_req,
                                           const json &request);

    static PromptProcessingPolicy ResolvePromptPolicy(ClientSource source,
                                                       RouteTarget target);

    ClientSource client_source_ = ClientSource::UNKNOWN;
};

#endif
