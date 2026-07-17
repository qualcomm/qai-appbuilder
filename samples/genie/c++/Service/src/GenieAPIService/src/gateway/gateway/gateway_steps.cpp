//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// genie_routing_gateway_steps.cpp
//
// 职责：
//   - Step1_Inspect：敏感检测
//   - Step2_EvaluateComplexity：复杂性评估
//   - Step3_Route：路由决策
//   - Step4_Desensitize：迭代脱敏闭环
//   - Step5_Execute：执行（LOCAL / CLOUD / ERROR）
//   - Step6_Audit：审计日志
//   - DesensitizeForCloudOnly：Cloud-only 模式脱敏辅助
//   - IsSessionStickyToCloud：查询 sticky 路由状态
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include "../../chat_request_handler/model_input_builder.h"
#include <chrono>
#include <sstream>
#include <iomanip>
#include <mutex>

// ============================================================
// 过滤 system/developer 消息（仅 agent_type == "main" 时）
// ============================================================
json GenieRoutingGateway::FilterSystemMessagesForInspection(const json &request, const std::string &agent_type)
{
    // 子 Agent 或非 main agent：不过滤，直接返回原始请求
    if (agent_type != "main")
    {
        return request;
    }

    // 主 Agent：过滤 system/developer 消息
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

// ============================================================
// Step 1: 敏感检测
// ============================================================
InspectionResult GenieRoutingGateway::Step1_Inspect(const json &request,
                                             const InspectionContext &ctx)
{
    return inspector_.Inspect(request, ctx);
}

// ============================================================
// Step 2: 复杂性评估
// ============================================================
ComplexityResult GenieRoutingGateway::Step2_EvaluateComplexity(const json &request,
                                                        const InspectionContext &ctx)
{
    json messages = request.value("messages", json::array());
    json tools = request.value("tools", json::array());
    return complexity_evaluator_.Evaluate(messages, tools, ctx);
}

// ============================================================
// Step 3: 路由决策
// ============================================================
RouteDecision GenieRoutingGateway::Step3_Route(const InspectionResult &inspection,
                                       const ComplexityResult &complexity,
                                       const std::string &agent_type)
{
    // 公有云可用性 = 配置开启 AND 运行时熔断器未触发
    bool public_cloud_available = cloud_client_.IsAvailable();
    // 企业云可用性 = 配置开启 AND 运行时熔断器未触发
    bool enterprise_cloud_available = enterprise_cloud_client_.IsAvailable();
    // 本地可用性 = 配置开启 AND 模型句柄有效
    // 使用可注入的本地可用性检测函数（默认检查模型句柄是否有效）
    // 测试时可通过 SetLocalAvailabilityChecker 注入 false，验证 S2+本地不可用 等场景
    bool local_available = local_model_config_.enabled && local_availability_checker_();

    return router_.Route(inspection, complexity,
                         enterprise_cloud_available,
                         public_cloud_available,
                         local_available,
                         agent_type);
}

// ============================================================
// IsSessionStickyToCloud：查询 sticky 路由状态
// ============================================================
bool GenieRoutingGateway::IsSessionStickyToCloud(const std::string &session_id)
{
    // session_id 为空或 sticky 路由未启用时，直接返回 false
    if (session_id.empty() || !routing_config_.sticky_routing.enabled)
    {
        return false;
    }

    std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
    auto it = sticky_sessions_.find(session_id);
    if (it == sticky_sessions_.end())
    {
        return false;
    }

    // 检查 TTL 是否过期
    if (std::chrono::steady_clock::now() >= it->second.expires_at)
    {
        return false;
    }

    // 检查是否锁定到任意云端（企业云或公有云）
    return it->second.target == RouteTarget::ENTERPRISE_CLOUD ||
           it->second.target == RouteTarget::PUBLIC_CLOUD;
}

// ============================================================
// Step 4: 迭代脱敏闭环
// ============================================================
DesensitizationResult GenieRoutingGateway::Step4_Desensitize(const json &request,
                                                       const InspectionResult &inspection,
                                                       const InspectionContext &ctx)
{
    // 迭代脱敏闭环实现
    // 每轮基于当前副本重新生成 spans，再执行替换，直到无规则命中或达到最大轮数
    json current = request;
    int rounds = 0;

    // 维护本次请求的累积映射表（本地副本），
    // 用于传给下一轮 Desensitizer，使其计数器跳过已使用的 mock 值，
    // 避免不同轮次分配相同的 mock 占位符（如两轮都分配 mock_user_1@example.com）。
    std::unordered_map<std::string, std::string> accumulated_mapping;

    while (true) {
        // 1) 生成 spans（基于当前副本）
        InspectionContext ctx2;
        ctx2.request_id = ctx.request_id;
        ctx2.session_id = ctx.session_id;
        ctx2.rule_engine_only = true;    // 关键：禁止本地模型辅助判断，避免保守升级干扰
        ctx2.collect_spans = true;       // 产出 spans 供 Desensitizer 使用
        ctx2.is_internal_inspection = false;

        InspectionResult insp = inspector_.Inspect(current, ctx2);

        // 2) 若无规则命中，直接成功
        if (insp.hit_rule_ids.empty()) {
            DesensitizationResult ok;
            ok.success = true;
            ok.desensitized_request = current;
            if (rounds > 0) {
                ok.applied_strategies.push_back("iterative_" + std::to_string(rounds) + "_rounds");
            }
            return ok;
        }

        // 3) 若命中 S2，立即失败（S2 内容不可上云）
        if (insp.sensitivity_level == SensitivityLevel::S2) {
            DesensitizationResult fail;
            fail.success = false;
            fail.failure_reason = "s2_found_in_iteration";
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed: S2 content found in iteration round=" << rounds << std::endl;
            return fail;
        }

        // 4) 执行本轮脱敏（基于 spans）
        // 传入累积映射表，使 Desensitizer 计数器跳过已使用的 mock 值
        const std::unordered_map<std::string, std::string> *prev_mapping =
            accumulated_mapping.empty() ? nullptr : &accumulated_mapping;
        DesensitizationResult out = desensitizer_.Apply(current, insp, prev_mapping);
        if (!out.success) {
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed in round=" << rounds
                                          << ": " << out.failure_reason << std::endl;
            return out;
        }

        // 5) 校验（不产出 spans，只检查规则命中）
        InspectionContext ctx3 = ctx2;
        ctx3.collect_spans = false;
        InspectionResult verify = inspector_.Inspect(out.desensitized_request, ctx3);

        // 每轮脱敏成功后立即合并映射表到 Session，
        // 避免迭代脱敏时中间轮次的映射丢失（只在最后一轮合并会导致前几轮的映射被遗漏）
        // 同时更新本地累积映射表，供下一轮 Desensitizer 使用
        if (out.use_format_preserving && !out.mapping_table.empty()) {
            MergeDesensitizationMapping(ctx.session_id, out.mapping_table);
            // 更新本地累积映射表（不覆盖已有 key，保留最早轮次的真实值）
            for (const auto &[mock, real] : out.mapping_table) {
                accumulated_mapping.emplace(mock, real);  // emplace 不覆盖已有 key
            }
        }

        if (verify.hit_rule_ids.empty()) {
            // 脱敏成功，所有轮次的映射已在上方逐轮合并
            return out;
        }

        if (verify.sensitivity_level == SensitivityLevel::S2) {
            out.success = false;
            out.failure_reason = "s2_after_desensitize";
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Desensitization failed: S2 after desensitize in round=" << rounds << std::endl;
            return out;
        }

        // 6) 检查是否允许迭代
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

        // 7) 下一轮输入为上一轮副本
        current = out.desensitized_request;
        My_Log{} << "[GenieRoutingGateway] Desensitization round=" << rounds << " completed, still has hits, retrying..." << std::endl;
    }
}

// ============================================================
// [Cloud-only 模式] DesensitizeForCloudOnly：对请求执行脱敏
// 主 Agent 场景自动过滤 system/developer 消息，脱敏后再插回。
// 被 cloud-only 路径的 S1 和 S2 分支共同调用。
// ============================================================
DesensitizationResult GenieRoutingGateway::DesensitizeForCloudOnly(
    const json &request,
    const InspectionResult &inspection,
    const InspectionContext &ctx,
    const std::string &agent_type,
    const std::string &log_tag)
{
    // 使用统一的过滤函数
    json request_for_desensitize = FilterSystemMessagesForInspection(request, agent_type);
    json system_messages = json::array();

    // 主 Agent 场景：提取 system/developer 消息（用于脱敏后插回）
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

    // 脱敏成功后：将 system/developer 消息插回 desensitized_request 头部
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

// ============================================================
// Step 5: 执行（LOCAL / CLOUD / ERROR）
// ============================================================
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
            // 使用 RouteDecision 中的错误码和 HTTP 状态码（由 ModelRouter::ApplyMatrix 填充）
            std::string code = decision.error_code.empty()
                ? "sensitive_content_local_unavailable" : decision.error_code;
            int http_status = (decision.error_http_status > 0) ? decision.error_http_status : 403;

            // 根据错误码选择对应的 message 模板（对应 §10.3 错误码字典）
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
            // 企业内网云处理（C1 级别任务）
            return ExecuteCloudTierCase(CloudTier::ENTERPRISE, decision.need_desensitize, original_request,
                                         desensitized, http_res, handled_by_cloud, user_session_id);
        }

        case RouteTarget::PUBLIC_CLOUD:
        {
            // 外部公有云处理（C2 级别任务）
            return ExecuteCloudTierCase(CloudTier::PUBLIC, decision.need_desensitize, original_request,
                                         desensitized, http_res, handled_by_cloud, user_session_id);
        }

        case RouteTarget::LOCAL:
        default:
        {
            // 脱敏失败回退 LOCAL 后，若 LOCAL 也不可用，返回对应错误（HTTP 403）
            // decision.local_available 由 Step4_Route 通过 local_availability_checker_() 设置
            if (!decision.local_available)
            {
                // 使用预设的错误码（desensitization_failed 或 sensitive_content_local_unavailable）
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

            // 本地处理：继续走原有流程
            My_Log{} << "[GenieRoutingGateway] Route=LOCAL, continuing with local processing" << std::endl;
            handled_by_cloud = false;
            return true;
        }
    }
}

// ============================================================
// Step5_Execute 中 ENTERPRISE_CLOUD / PUBLIC_CLOUD 分支共用的执行逻辑
// （两者结构一致，仅配置对象、路由目标和日志文案不同，按 tier 参数化）
// ============================================================
bool GenieRoutingGateway::ExecuteCloudTierCase(CloudTier tier,
                                                bool need_desensitize,
                                                const json &original_request,
                                                const DesensitizationResult &desensitized,
                                                httplib::Response &http_res,
                                                bool &handled_by_cloud,
                                                const std::string &user_session_id)
{
    const bool is_enterprise = (tier == CloudTier::ENTERPRISE);
    const RouteTarget route_target = is_enterprise ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
    const char *route_name = is_enterprise ? "ENTERPRISE_CLOUD" : "PUBLIC_CLOUD";
    const char *cloud_desc = is_enterprise ? "enterprise cloud model" : "public cloud model";
    const char *cloud_label = is_enterprise ? "enterprise cloud" : "public cloud";
    const char *policy_log_prefix = is_enterprise ? "Enterprise cloud prompt policy: " : "Public cloud prompt policy: ";
    const char *prepared_log_prefix = is_enterprise ? "Enterprise cloud prompt prepared: " : "Public cloud prompt prepared: ";
    const char *failed_log_prefix = is_enterprise ? "Enterprise cloud prompt preparation failed: " : "Public cloud prompt preparation failed: ";
    const char *initial_message = is_enterprise ? "正在企业云推理..." : "正在公有云推理...";

    My_Log{} << "[GenieRoutingGateway] Route=" << route_name << ", sending to " << cloud_desc << std::endl;

    // 防御性检查：若需要脱敏但脱敏失败，禁止将原始敏感内容发送到云端
    if (need_desensitize && !desensitized.success)
    {
        My_Log{My_Log::Level::kError}
            << "[GenieRoutingGateway] SECURITY GUARD: need_desensitize=true but desensitization failed. "
            << "Refusing to send original request to " << cloud_label << ". Falling back to local." << std::endl;
        handled_by_cloud = false;
        return true;  // 回退到本地处理
    }

    // 使用脱敏后的请求（如果需要脱敏）
    const json &base_request = (need_desensitize && desensitized.success)
                                 ? desensitized.desensitized_request
                                 : original_request;

    // 检查是否为 streaming 请求
    bool is_stream = false;
    if (original_request.contains("stream") && original_request["stream"].is_boolean())
    {
        is_stream = original_request["stream"].get<bool>();
    }

    // ── 统一提示词优化策略 ────────────────────────────────────────────
    // 根据客户端来源决定是否对云端请求执行提示词优化
    PromptProcessingPolicy policy = ResolvePromptPolicy(client_source_, route_target);
    const char* policy_str = (policy == PromptProcessingPolicy::OPTIMIZED)
                               ? "OPTIMIZED" : "PASS_THROUGH";
    My_Log{} << "[GenieRoutingGateway] " << policy_log_prefix << policy_str << std::endl;

    json final_request = base_request;
    if (policy == PromptProcessingPolicy::OPTIMIZED)
    {
        // 获取云端 context_size（0 时使用默认值）
        int ctx_size = is_enterprise
            ? ((enterprise_cloud_config_.context_size > 0)
                   ? enterprise_cloud_config_.context_size
                   : EnterpriseCloudModelConfig::DEFAULT_ENTERPRISE_CLOUD_CONTEXT_SIZE)
            : ((cloud_config_.context_size > 0)
                   ? cloud_config_.context_size
                   : CloudModelConfig::DEFAULT_CLOUD_CONTEXT_SIZE);

        auto prep_result = prompt_prep_service_.PrepareForCloud(base_request, ctx_size);
        if (prep_result.success)
        {
            final_request = prep_result.prepared_request;
            My_Log{} << "[GenieRoutingGateway] " << prepared_log_prefix
                     << "system_optimized=" << prep_result.prompt_optimized
                     << ", messages_trimmed=" << prep_result.messages_trimmed
                     << ", context_size=" << prep_result.used_context_size << std::endl;
        }
        else
        {
            My_Log{My_Log::Level::kWarning}
                << "[GenieRoutingGateway] " << failed_log_prefix
                << prep_result.failure_reason
                << ". Falling back to original request." << std::endl;
        }
    }

    return ExecuteCloudRequest(final_request, is_stream, http_res,
                               handled_by_cloud, user_session_id,
                               nullptr, "inference", initial_message, tier);
}

// ============================================================
// 格式化当前 UTC 时间为审计日志时间戳字符串
// ============================================================
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

// ============================================================
// Step 6: 审计日志
// ============================================================
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
    AuditLogger::AuditRecord record;
    record.request_id = ctx.request_id;
    record.session_id = ctx.session_id;
    record.timestamp = FormatAuditTimestampUtc();

    record.route_decision = decision.target;
    record.sensitivity_level = inspection.sensitivity_level;
    record.complexity_level = complexity.complexity_level;
    record.desensitized = decision.need_desensitize;
    // 使用实际的脱敏结果，而非假设成功
    record.desensitize_success = decision.need_desensitize && desensitized.success;
    record.desensitize_strategies = desensitized.applied_strategies;
    record.desensitize_failure_reason = desensitized.failure_reason;
    record.latency_ms = latency_ms;
    record.http_status = http_status;
    record.hit_rule_ids = inspection.hit_rule_ids;
    record.hit_categories = inspection.hit_categories;
    record.route_reason = decision.decision_reason;
    record.rule_engine_failed = inspection.rule_engine_failed;
    // 工具输出回流门禁触发标志：从 InspectionResult 传递到 AuditRecord
    record.tool_output_escalation = inspection.tool_output_escalation;
    // 超大字段扫描告警
    record.oversized_field_warning = inspection.oversized_field_warning;
    record.oversized_fields_count = inspection.oversized_fields_count;
    // 延迟告警：超出 P95 目标值时置为 true（仅可观测性，不触发降级）
    // 不开启本地模型检查时：P95 目标 ≤300ms（规则引擎）
    // 开启本地模型检查时：P95 目标 ≤300000ms（5 分钟，本地模型推理可能耗时较长）
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

    // 根据路由目标记录对应云端端点名称和层级标识
    if (decision.target == RouteTarget::PUBLIC_CLOUD)
    {
        std::string ep = cloud_client_.GetLastUsedEndpoint();
        record.cloud_endpoint = ep.empty() ? "public_cloud_model" : ep;
        record.cloud_tier = "public_cloud";
    }
    else if (decision.target == RouteTarget::ENTERPRISE_CLOUD)
    {
        std::string ep = enterprise_cloud_client_.GetLastUsedEndpoint();
        record.cloud_endpoint = ep.empty() ? "enterprise_cloud_model" : ep;
        record.cloud_tier = "enterprise_cloud";
    }
    else if (decision.target == RouteTarget::POLICY_ERROR)
    {
        record.cloud_tier = "error";
    }
    // else: LOCAL → cloud_tier 保持默认值 "local"

    record.sticky_route_hit = sticky_route_hit;

    // 云端 Token 使用量（仅 CLOUD 路由时有效）
    record.cloud_prompt_tokens     = cloud_prompt_tokens;
    record.cloud_completion_tokens = cloud_completion_tokens;
    record.cloud_total_tokens      = cloud_total_tokens;

    // 增量检查相关字段
    record.incremental_check_used    = incremental_check_used;
    record.incremental_new_messages  = incremental_new_messages;
    record.total_messages_count      = total_messages_count;
    record.incremental_fallback_reason = incremental_fallback_reason;

    audit_logger_.Log(record);
}

// ============================================================
// 辅助函数：从请求中检测 Agent 类型
// 通过检测 system prompt 中是否包含 "agent=main" 来判断。
//
// 重要说明：Agent 类型（"main"/"sub"）是请求级别的逻辑属性，
// 与执行该请求的模型实例的底层硬件类型（CPU/GPU/NPU）无关。
// 任意硬件类型的模型均可在不同请求中分别承担主 Agent 或子 Agent 角色。
// ============================================================
std::string GenieRoutingGateway::DetectAgentTypeFromRequest(const json& request)
{
    // 1. 从 messages 中提取 system prompt
    std::string system_prompt;
    if (request.contains("messages") && request["messages"].is_array()) {
        for (const auto& msg : request["messages"]) {
            if (msg.value("role", "") == "system") {
                system_prompt = msg.value("content", "");
                break;
            }
        }
    }

    // 2. 检测 Agent 类型
    // 判断依据：OpenClaw 主 Agent 的 system prompt 中会由运行时注入
    // "## Runtime" 块，其中包含 "agent=main" 字段。
    // 只要匹配到 "agent=main" 就认定为主 Agent，否则一律视为子 Agent。
    bool is_main_agent = (system_prompt.find("agent=main") != std::string::npos);
    
    std::string agent_type = is_main_agent ? "main" : "sub";
    
    My_Log{My_Log::Level::kDebug} << "[DetectAgentType] Detected: " << agent_type
                                   << " (agent=main " << (is_main_agent ? "found" : "not found")
                                   << " in system prompt)" << std::endl;
    
    return agent_type;
}
