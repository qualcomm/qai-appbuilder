//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// genie_routing_gateway_overflow.cpp
//
// 职责：
//   - HandleLocalOutputOverflow：事后路由回退（本地输出溢出 / 工具调用超限）
//   - HandleLocalInputOverflow：预路由回退（本地输入溢出）
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include <chrono>
#include <mutex>

// ============================================================
// HandleLocalOutputOverflow：事后路由回退（本地能力不足）
// 当本地推理完成后（或在处理前预判到工具调用超限），检测到输出溢出或工具调用超限时调用。
// ============================================================
bool GenieRoutingGateway::HandleLocalOutputOverflow(const json &request,
                                                    const httplib::Request &http_req,
                                                    httplib::Response &http_res,
                                                    bool is_tool_call_retries_exceeded,
                                                    httplib::DataSink *sink)
{
    My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] HandleLocalOutputOverflow triggered, is_tool_call_retries_exceeded="
                                 << is_tool_call_retries_exceeded
                                 << ", sink=" << (sink ? "provided(stream_fallback)" : "null") << std::endl;

    auto start_time = std::chrono::high_resolution_clock::now();

    InspectionContext ctx;
    ctx.request_id = GenerateRequestId();
    // 服务端生成两级 session ID（不依赖客户端传入 session_id）
    ResolveServerSessionIds(request, ctx);

    // 过滤 system/developer 消息（仅 agent_type == "main" 时）
    std::string agent_type = DetectAgentTypeFromRequest(request);
    json request_for_inspection = FilterSystemMessagesForInspection(request, agent_type);
    if (agent_type == "main" && request.contains("messages") && request_for_inspection.contains("messages"))
    {
        size_t orig = request["messages"].size();
        size_t filtered = request_for_inspection["messages"].size();
        if (orig != filtered)
        {
            My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] HandleLocalOutputOverflow: Main agent skipped "
                << (orig - filtered) << " system/developer message(s) from security inspection" << std::endl;
        }
    }

    // 1. 安全检查（传入过滤后的请求，主 Agent 已排除 system/developer 消息）
    InspectionResult inspection = Step1_Inspect(request_for_inspection, ctx);

    // 2. 判断是否允许回退到云端
    // 优先回退到企业云，企业云不可用时回退到公有云
    bool fallback_to_cloud = false;
    // 公有云可用性 = 配置开启 AND 运行时熔断器未触发
    bool public_cloud_available = cloud_client_.IsAvailable();
    // 企业云可用性 = 配置开启 AND 运行时熔断器未触发
    bool enterprise_cloud_available = enterprise_cloud_client_.IsAvailable();
    // overflow fallback 优先选择企业云，企业云不可用时选公有云
    CloudTier fallback_cloud_tier = enterprise_cloud_available ? CloudTier::ENTERPRISE : CloudTier::PUBLIC;
    bool any_cloud_available = enterprise_cloud_available || public_cloud_available;

    DesensitizationResult desensitized;
    desensitized.success = false;
    bool need_desensitize = false;

    if (inspection.sensitivity_level == SensitivityLevel::S2)
    {
        My_Log{My_Log::Level::kWarning} << "[GenieRoutingGateway] Overflow/retry limit on S2 content. Prohibiting cloud fallback." << std::endl;
        fallback_to_cloud = false;
    }
    else if (!any_cloud_available)
    {
        My_Log{My_Log::Level::kWarning} << "[GenieRoutingGateway] Overflow/retry limit but all clouds are unavailable. Cannot fallback." << std::endl;
        fallback_to_cloud = false;
    }
    else
    {
        if (inspection.sensitivity_level == SensitivityLevel::S1)
        {
            // 脱敏时也需要过滤 system/developer 消息
            // 注意：request_for_inspection 已在前面过滤过，但 Step4_Desensitize 需要完整的 request
            // 因此这里需要重新过滤（或者传入 request_for_inspection，但需要确保后续使用原始 request）
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

    // 记录"本来应该走云端"的意图（用于审计日志准确性）
    // 在 stream=true 场景下，即使无法实际回退，审计也应记录 CLOUD 路由意图
    bool intended_for_cloud = fallback_to_cloud;

    // 流式场景下，响应已开始发送，无法覆盖，仅记录审计日志，不触发云端重试
    // 对应计划文档 §七-A："流式场景（stream=true）：仅记录审计日志，不触发云端重试"
    //
    // 例外1：is_tool_call_retries_exceeded=true 时，检查发生在 HandleChatCompletion
    // 之后、本地推理开始之前，响应尚未启动，可以安全地进行云端重试。
    //
    // 例外2：sink!=nullptr 时，调用方已在 set_chunked_content_provider 回调中，
    // 可以直接向现有 sink 写入云端 SSE 数据，实现无缝流式回退，无需跳过。
    if (is_stream && fallback_to_cloud && !is_tool_call_retries_exceeded && sink == nullptr)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] HandleLocalOutputOverflow: stream=true, sink=null, "
            << "skipping cloud retry (response already started, no sink for inline fallback). Audit only." << std::endl;
        fallback_to_cloud = false;
    }

    bool handled_by_cloud = false;
    if (fallback_to_cloud)
    {
        // 构建云端请求：先选择脱敏后的版本（如有），再清理本地历史
        json cloud_request = (need_desensitize && desensitized.success)
                             ? desensitized.desensitized_request
                             : request;

        // 必须在 CleanLocalHistoryForCloudFallback 之前执行！
        // 原因：session_security_states_ 中记录的 s2_turn_ranges 的 start_idx/end_idx
        // 是基于原始 request 的消息索引。CleanLocalHistoryForCloudFallback 会删除部分消息，
        // 导致消息数组长度变化，若先执行本地历史清理，s2_turn_ranges 中的索引将与
        // cloud_request 中的实际消息位置不匹配，PurgeS2TurnsFromHistory 会按错误索引操作，
        // 可能遗漏应删除的 S2 消息，导致 S2 数据泄露到云端。
        // 正确顺序：先按原始索引清洗 S2 轮次，再清理本地历史（此时索引已无关）。
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

        // clean_local_history_on_fallback 仅适用于本地模型产生的 tool_calls/tool 消息。
        // is_tool_call_retries_exceeded=true 时，工具调用由云端模型发起，历史记录格式正确，
        // 不应清理，否则会导致云端模型丢失所有分析上下文，无法生成最终总结。
        // 注意：S2 清理已在上方完成，此处的本地历史清理不影响 S2 清洗的正确性。
        if (routing_config_.fallback.clean_local_history_on_fallback && !is_tool_call_retries_exceeded)
        {
            cloud_request = CleanLocalHistoryForCloudFallback(cloud_request);
        }

        // 保存原有的本地响应，以防云端调用失败但覆盖了 http_res
        int original_status = http_res.status;
        std::string original_body = http_res.body;

        // 根据触发原因选择不同的初始状态消息：
        //   is_tool_call_retries_exceeded=true：工具调用次数超限，切换到云端
        //   普通本地输出溢出：本地输出截断，切换到云端
        const std::string fallback_status  = is_tool_call_retries_exceeded
                                             ? "tool_call_limit"
                                             : "cloud_fallback";
        const std::string fallback_message = is_tool_call_retries_exceeded
                                             ? "Tool call retries exceeded, switching to cloud model..."
                                             : "Switching to cloud model...";
        // 传入 fallback_cloud_tier（优先企业云，企业云不可用时用公有云）
        bool success = ExecuteCloudRequest(cloud_request, is_stream, http_res, handled_by_cloud,
                                           ctx.session_id, sink, fallback_status, fallback_message,
                                           fallback_cloud_tier);

        // 若 ExecuteCloudRequest 由于 fallback to local 返回 true 但 handled_by_cloud=false，
        // 或者 ExecuteCloudRequest 返回 false（云端失败且策略禁止回退），
        // 都说明事后回退云端失败了。
        // 根据规范，事后回退失败时必须保留原有的本地截断响应，不返回错误。
        if (!success || !handled_by_cloud)
        {
            My_Log{My_Log::Level::kWarning} << "[GenieRoutingGateway] Cloud fallback failed during post-execution. Restoring local truncated response." << std::endl;
            fallback_to_cloud = false;

            // 恢复本地响应
            http_res.status = original_status;
            http_res.body = original_body;
            // 清除可能被 ExecuteCloudRequest 覆盖的 chunked content provider
            if (!is_stream) {
                http_res.set_content(original_body, ResponseDispatcher::MIMETYPE_JSON);
            }
        }
        else
        {
            // 成功回退到云端后，设置 sticky session
            // 确保后续请求（下一轮工具调用等）也走云端，直到任务完成
            if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
            {
                std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);

                // max_sessions 防护
                if ((int)sticky_sessions_.size() >= routing_config_.sticky_routing.max_sessions)
                {
                    sticky_sessions_.erase(sticky_sessions_.begin());
                }

                StickyRouteEntry entry;
                // sticky 路由目标使用实际的 fallback_cloud_tier
                entry.target = (fallback_cloud_tier == CloudTier::ENTERPRISE)
                    ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
                entry.sensitivity = inspection.sensitivity_level;
                entry.expires_at = std::chrono::steady_clock::now() +
                                   std::chrono::seconds(routing_config_.sticky_routing.ttl_seconds);
                // 记录第一次 fallback 时的消息边界：
                // 客户端每次请求都携带完整历史，后续 sticky session 命中时需要通过此边界
                // 区分本地历史（索引 < fallback_msg_count）和云端历史（索引 >= fallback_msg_count），
                // 只清理本地历史，保留云端模型已完成的工具调用记录。
                entry.fallback_msg_count = request.contains("messages") && request["messages"].is_array()
                                           ? (int)request["messages"].size() : 0;
                sticky_sessions_[ctx.session_id] = entry;

                My_Log{} << "[GenieRoutingGateway] Sticky route set after overflow fallback for session="
                         << ctx.session_id << ", fallback_msg_count=" << entry.fallback_msg_count << std::endl;
            }
        }
    }

    auto end_time = std::chrono::high_resolution_clock::now();
    int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end_time - start_time).count();

    // 3. 记录审计日志
    ComplexityResult fake_complexity;
    fake_complexity.complexity_level = ComplexityLevel::C0;

    RouteDecision decision;
    // 审计记录使用 intended_for_cloud 而非 fallback_to_cloud：
    // 在 stream=true 场景下，即使无法实际回退云端，也应记录"本来会走云端"的路由意图，
    // 避免误导性的 route_decision=LOCAL（实际上请求本来就应该走云端）。
    // 根据 fallback_cloud_tier 记录实际路由目标
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
    // 若流式场景跳过了云端重试，在 reason 中说明
    if (is_stream && intended_for_cloud && !fallback_to_cloud)
    {
        decision.decision_reason = "post_execution_fallback_stream_skipped";
    }
    else
    {
        decision.decision_reason = "post_execution_fallback";
    }

    AuditLogger::AuditRecord record;
    record.request_id = ctx.request_id;
    record.session_id = ctx.session_id;

    record.timestamp = FormatAuditTimestampUtc();

    record.route_decision = decision.target;
    record.sensitivity_level = inspection.sensitivity_level;
    record.complexity_level = decision.complexity;

    record.desensitized = decision.need_desensitize;
    if (decision.need_desensitize) {
        record.desensitize_success = desensitized.success;
        record.desensitize_strategies = desensitized.applied_strategies;
        record.desensitize_failure_reason = desensitized.failure_reason;
    }

    record.cloud_endpoint = "";
    // 仅当实际执行了云端请求（handled_by_cloud=true）时才记录 endpoint 和 cloud_tier
    // 若 stream=true 跳过了云端重试，即使 intended_for_cloud=true 也不记录 endpoint
    if (handled_by_cloud) {
        // 根据 fallback_cloud_tier 获取对应客户端的端点名称和层级标识
        if (fallback_cloud_tier == CloudTier::ENTERPRISE) {
            std::string ep = enterprise_cloud_client_.GetLastUsedEndpoint();
            record.cloud_endpoint = ep.empty() ? "enterprise_cloud_model" : ep;
            record.cloud_tier = "enterprise_cloud";
        } else {
            std::string ep = cloud_client_.GetLastUsedEndpoint();
            record.cloud_endpoint = ep.empty() ? "public_cloud_model" : ep;
            record.cloud_tier = "public_cloud";
        }
    }

    record.latency_ms = latency_ms;
    record.http_status = fallback_to_cloud ? http_res.status : 200; // 本地截断视为 200
    record.retry_count = 0;

    record.hit_rule_ids = inspection.hit_rule_ids;
    record.hit_categories = inspection.hit_categories;
    record.route_reason = decision.decision_reason;
    record.rule_engine_failed = inspection.rule_engine_failed;
    record.tool_output_escalation = inspection.tool_output_escalation;
    record.keywords_dict_reloaded = inspection.keywords_dict_reloaded;
    record.keywords_dict_rules_count = inspector_.GetKeywordsRulesCount();

    // 【关键】填充事后路由回退标志
    if (is_tool_call_retries_exceeded) {
        record.tool_call_retries_exceeded = true;
    } else {
        record.local_output_overflow = true;
    }

    audit_logger_.Log(record);

    return fallback_to_cloud;
}

// ============================================================
// HandleLocalInputOverflow：预路由回退（本地输入溢出）
// 当 ModelInputBuilder::Build 因压缩后仍超出上下文窗口而抛出异常时调用。
// ============================================================
bool GenieRoutingGateway::HandleLocalInputOverflow(const json &request,
                                                    const httplib::Request &http_req,
                                                    httplib::Response &http_res,
                                                    ResponseDispatcher &dispatcher)
{
    // routing 未启用时返回 false，调用方走原有异常处理逻辑
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
    // 服务端生成两级 session ID（不依赖客户端传入 session_id）
    ResolveServerSessionIds(request, ctx);

    // 云端可用性检查提前到安全检查之前：
    // 若所有云端均不可用，无论内容是否敏感（S0/S1/S2），最终都无法上云，
    // 直接返回错误，跳过耗时的安全检查（可节省约 3~4 秒的 prompt prefill 时间）。
    // 检查企业云和公有云的可用性
    bool input_public_cloud_available = cloud_client_.IsAvailable();
    bool input_enterprise_cloud_available = enterprise_cloud_client_.IsAvailable();
    bool input_any_cloud_available = input_enterprise_cloud_available || input_public_cloud_available;
    // input overflow fallback 优先选择企业云，企业云不可用时选公有云
    CloudTier input_fallback_cloud_tier = input_enterprise_cloud_available
        ? CloudTier::ENTERPRISE : CloudTier::PUBLIC;

    if (!input_any_cloud_available)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] Input overflow but cloud is unavailable. "
            << "Skipping security check and returning error immediately." << std::endl;

        // 检查重试次数：超出限制时升级为 422（永久失败），阻止客户端无限重试
        // 使用 global_session_id 而非 user_session_id，
        // 因为每次新 user 轮次都会生成新的 user_session_id，导致计数器被重置，
        // 永远无法累积到上限。global_session_id 在同一会话的所有轮次中保持不变。
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

        // 审计日志：sensitivity_level 填 S0（未做安全检查，使用默认值）
        AuditLogger::AuditRecord record_early;
        record_early.request_id = ctx.request_id;
        record_early.session_id = ctx.session_id;
        record_early.timestamp = FormatAuditTimestampUtc();
        record_early.route_decision = RouteTarget::POLICY_ERROR;
        record_early.sensitivity_level = SensitivityLevel::S0;  // 未做安全检查，使用默认值
        record_early.complexity_level = ComplexityLevel::C0;
        record_early.desensitized = false;
        record_early.latency_ms = latency_ms_early;
        record_early.http_status = http_status_cloud_unavail;
        record_early.retry_count = 0;
        record_early.route_reason = "all_routes_unavailable";
        record_early.keywords_dict_rules_count = inspector_.GetKeywordsRulesCount();
        record_early.local_input_overflow = true;
        audit_logger_.Log(record_early);
        return true;
    }

    // 过滤 system/developer 消息（仅 agent_type == "main" 时）
    std::string agent_type = DetectAgentTypeFromRequest(request);
    json request_for_inspection = FilterSystemMessagesForInspection(request, agent_type);
    if (agent_type == "main" && request.contains("messages") && request_for_inspection.contains("messages"))
    {
        size_t orig = request["messages"].size();
        size_t filtered = request_for_inspection["messages"].size();
        if (orig != filtered)
        {
            My_Log{My_Log::Level::kInfo} << "[GenieRoutingGateway] HandleLocalInputOverflow: Main agent skipped "
                << (orig - filtered) << " system/developer message(s) from security inspection" << std::endl;
        }
    }

    // Step1: 敏感检测（传入过滤后的请求，主 Agent 已排除 system/developer 消息）
    // 注意：此处 cloud_available 已确认为 true（否则上方已提前返回）
    InspectionResult inspection = Step1_Inspect(request_for_inspection, ctx);

    // 辅助 lambda：构建并输出审计记录
    auto emit_audit = [&](RouteTarget route_target,
                          bool desensitized_flag,
                          const DesensitizationResult &desens,
                          int http_status,
                          const std::string &reason,
                          int64_t latency_ms_val)
    {
        AuditLogger::AuditRecord record;
        record.request_id = ctx.request_id;
        record.session_id = ctx.session_id;

        record.timestamp = FormatAuditTimestampUtc();

        record.route_decision = route_target;
        record.sensitivity_level = inspection.sensitivity_level;
        record.complexity_level = ComplexityLevel::C0;  // 输入溢出时未做复杂度评估
        record.desensitized = desensitized_flag;
        if (desensitized_flag)
        {
            record.desensitize_success = desens.success;
            record.desensitize_strategies = desens.applied_strategies;
            record.desensitize_failure_reason = desens.failure_reason;
        }
        // 根据路由目标获取对应客户端的端点名称和层级标识
        if (route_target == RouteTarget::ENTERPRISE_CLOUD) {
            record.cloud_endpoint = enterprise_cloud_client_.GetLastUsedEndpoint();
            record.cloud_tier = "enterprise_cloud";
        } else if (route_target == RouteTarget::PUBLIC_CLOUD) {
            record.cloud_endpoint = cloud_client_.GetLastUsedEndpoint();
            record.cloud_tier = "public_cloud";
        } else if (route_target == RouteTarget::POLICY_ERROR) {
            record.cloud_endpoint = "";
            record.cloud_tier = "error";
        } else {
            record.cloud_endpoint = "";
            // cloud_tier 保持默认值 "local"
        }
        record.latency_ms = latency_ms_val;
        record.http_status = http_status;
        record.retry_count = 0;
        record.hit_rule_ids = inspection.hit_rule_ids;
        record.hit_categories = inspection.hit_categories;
        record.route_reason = reason;
        record.rule_engine_failed = inspection.rule_engine_failed;
        record.tool_output_escalation = inspection.tool_output_escalation;
        record.keywords_dict_reloaded = inspection.keywords_dict_reloaded;
        record.keywords_dict_rules_count = inspector_.GetKeywordsRulesCount();
        // 【关键】标记本次请求为本地输入溢出预路由回退
        record.local_input_overflow = true;

        audit_logger_.Log(record);
    };

    // 若 sensitivity == S2：尝试脱敏降级，若失败则禁止触发云端路由，返回 422
    // 注意：此处 cloud_available 已确认为 true（否则上方已提前返回）
    if (inspection.sensitivity_level == SensitivityLevel::S2)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] Input overflow on S2 content. "
            << "Attempting desensitization fallback..." << std::endl;

        // 尝试脱敏降级：
        // 注意：不能调用 Step4_Desensitize，因为其内部会对 S2 内容立即返回失败。
        // 正确做法：直接调用 desensitizer_.Apply() 执行替换，再重新检测是否降级。
        // S2 通常由多个 S1 规则叠加触发（如 R_EMAIL + KW_ENTERPRISE_CONTRACT），
        // 脱敏替换掉 PII 后，组合规则可能不再触发 S2，从而降级为 S1/S0。

        // Step A: 重新检测，收集 spans（用于脱敏替换定位）
        // 过滤 system/developer 消息后再检测
        json request_for_spans = FilterSystemMessagesForInspection(request, agent_type);
        InspectionContext ctx_spans;
        ctx_spans.request_id = ctx.request_id;
        ctx_spans.session_id = ctx.session_id;
        ctx_spans.rule_engine_only = true;   // 只用规则引擎，不触发本地模型
        ctx_spans.collect_spans = true;      // 必须收集 spans，供 desensitizer_.Apply() 使用
        ctx_spans.is_internal_inspection = false;
        InspectionResult inspection_with_spans = inspector_.Inspect(request_for_spans, ctx_spans);

        // Step B: 直接执行脱敏替换（绕过 Step4_Desensitize 的 S2 拦截）
        // 注意：desensitizer_.Apply 作用于过滤后的 request_for_spans
        DesensitizationResult desens_fallback = desensitizer_.Apply(request_for_spans, inspection_with_spans);

        if (desens_fallback.success)
        {
            // Step C: 重新检测脱敏后的内容，验证是否成功降级
            // 脱敏后的内容已不含 system 消息（在 Step A 中已过滤），无需再次过滤
            InspectionContext ctx_verify;
            ctx_verify.request_id = ctx.request_id;
            ctx_verify.session_id = ctx.session_id;
            ctx_verify.rule_engine_only = true;
            ctx_verify.collect_spans = false;
            ctx_verify.is_internal_inspection = false;
            InspectionResult verify = inspector_.Inspect(desens_fallback.desensitized_request, ctx_verify);

            if (verify.sensitivity_level == SensitivityLevel::S2)
            {
                // 脱敏后仍为 S2，无法降级（S2 关键词无法被替换）
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
            // 脱敏成功，将 S2 降级为 S1，继续走云端路由流程
            My_Log{} << "[GenieRoutingGateway] S2 desensitization fallback succeeded on input overflow. "
                     << "Downgrading to S1 and routing to cloud." << std::endl;

            // 直接使用 S2 脱敏结果，跳过后续 S1 重复脱敏步骤
            json cloud_request_s2 = desens_fallback.desensitized_request;

            // [S2 轮次清理] 必须在 CleanLocalHistoryForCloudFallback 之前执行！
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
                    SensitivityLevel::S1,  // 脱敏后等级为 S1
                    "LocalInputOverflow-S2-Desensitized");
            }

            bool is_stream_s2 = request.contains("stream") && request["stream"].is_boolean()
                                && request["stream"].get<bool>();
            bool handled_by_cloud_s2 = false;
            // 传入 input_fallback_cloud_tier
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
                    // ExecuteCloudRequest 直接失败（网络错误等）：发送 502 错误响应
                    SendPolicyViolationError(http_res, "all_routes_unavailable",
                        "Bad Gateway: Cloud request failed after S2 desensitization. "
                        "The upstream cloud service is unavailable.", 502);
                }
                else
                {
                    // success_s2=true 但 handled_by_cloud_s2=false：云端未处理请求
                    // 检查重试次数：超出限制时升级为 422（永久失败），阻止客户端无限重试
                    // 使用 global_session_id 而非 user_session_id，
                    // 因为每次新 user 轮次都会生成新的 user_session_id，导致计数器被重置，
                    // 永远无法累积到上限。global_session_id 在同一会话的所有轮次中保持不变。
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

            // 设置 sticky session
            if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
            {
                std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
                if ((int)sticky_sessions_.size() >= routing_config_.sticky_routing.max_sessions)
                    sticky_sessions_.erase(sticky_sessions_.begin());
                StickyRouteEntry entry;
                // sticky 路由目标使用实际的 input_fallback_cloud_tier
                entry.target = (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
                    ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
                entry.sensitivity = SensitivityLevel::S1;  // 脱敏后等级
                entry.expires_at = std::chrono::steady_clock::now() +
                                   std::chrono::seconds(routing_config_.sticky_routing.ttl_seconds);
                entry.fallback_msg_count = request.contains("messages") && request["messages"].is_array()
                                           ? (int)request["messages"].size() : 0;
                sticky_sessions_[ctx.session_id] = entry;
            }

            // 使用实际路由目标
            RouteTarget s2_route_target = (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
                ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
            emit_audit(s2_route_target, true, desens_fallback, http_res.status,
                       "local_input_overflow_s2_desensitized_routed_to_cloud", latency_ms_s2);
            return true;
        }
        else
        {
            // 脱敏失败，无法降级，禁止云端路由
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] S2 desensitization fallback failed on input overflow. "
                << "Cannot route to cloud. Failure reason: " << desens_fallback.failure_reason << std::endl;

            // 友好提示：说明问题已无法解决，不再重试
            std::string friendly_message =
                "Service Unavailable: The request contains highly sensitive content (S2) and exceeds "
                "the local model's context window. We attempted to desensitize the content to enable "
                "cloud processing, but the desensitization failed. This issue cannot be resolved automatically. "
                "Please try one of the following:\n"
                "1. Reduce the request size by removing unnecessary context or history\n"
                "2. Split your request into smaller parts\n"
                "3. Remove or mask sensitive information manually before submitting";

            // 使用 422（Unprocessable Entity）而非 503（Service Unavailable）：
            // 422 语义为"请求内容无法处理"，属于永久性失败，客户端不应重试。
            // 503 语义为"服务暂时不可用"，客户端通常会重试，会导致无限循环。
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

    // 若 sensitivity == S1：先脱敏再路由云端
    // 注意：此处 cloud_available 已确认为 true（否则函数开头已提前返回）
    DesensitizationResult desensitized;
    bool need_desensitize = false;

    if (inspection.sensitivity_level == SensitivityLevel::S1)
    {
        // 脱敏时也需要过滤 system/developer 消息
        json request_for_desensitize = FilterSystemMessagesForInspection(request, agent_type);
        desensitized = Step4_Desensitize(request_for_desensitize, inspection, ctx);
        if (!desensitized.success)
        {
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Desensitization failed for S1 content on input overflow. "
                << "Returning 422 (desensitization_failed_on_input_overflow)." << std::endl;

            // 使用 422（Unprocessable Entity）而非 503（Service Unavailable）：
            // S1 内容脱敏失败是永久性失败（内容本身无法被脱敏），重试不会改变结果。
            // 503 语义为"服务暂时不可用"，客户端通常会重试，会导致无限循环。
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

    // S0 或 S1（脱敏成功）：路由到云端
    json cloud_request = (need_desensitize && desensitized.success)
                         ? desensitized.desensitized_request
                         : request;

    // [S2 轮次清理] 必须在 CleanLocalHistoryForCloudFallback 之前执行！
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

    // 与 HandleLocalOutputOverflow 保持一致：
    // 根据配置决定是否清理本地模型产生的 tool_calls/tool 历史消息。
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
    // 本地输入溢出路由到云端：发送 cloud_fallback 状态，告知客户端正在切换到云端模型
    // 传入 input_fallback_cloud_tier
    bool success = ExecuteCloudRequest(cloud_request, is_stream, http_res, handled_by_cloud,
                                       ctx.session_id, nullptr,
                                       "cloud_fallback", "Switching to cloud model...",
                                       input_fallback_cloud_tier);

    auto end_time = std::chrono::high_resolution_clock::now();
    int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        end_time - start_time).count();

    if (!success || !handled_by_cloud)
    {
        // 云端路由失败（ExecuteCloudRequest 可能已写入错误响应，或 fallback to local 但 local 已失败）
        // 与 HandleLocalOutputOverflow 不同，此处没有"保留本地截断响应"的选项，
        // 必须返回错误。
        if (success && !handled_by_cloud)
        {
            // ExecuteCloudRequest 返回 true 但 handled_by_cloud=false，
            // 表示云端失败且 fallback_to_local=true，但 local 已经无法处理（输入溢出）。
            // 检查重试次数：超出限制时升级为 422（永久失败），阻止客户端无限重试
            // 使用 global_session_id 而非 user_session_id，
            // 因为每次新 user 轮次都会生成新的 user_session_id，导致计数器被重置，
            // 永远无法累积到上限。global_session_id 在同一会话的所有轮次中保持不变。
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
        // 若 success=false，ExecuteCloudRequest 已写入错误响应（502 等），无需覆盖

        emit_audit(RouteTarget::POLICY_ERROR, need_desensitize, desensitized,
                   http_res.status, "cloud_routing_failed_on_input_overflow", latency_ms);
        return true;
    }

    // 云端路由成功：重置重试计数（下次输入溢出时重新计数）
    // 使用 global_session_id 与 CheckAndIncrementInputOverflowRetry 保持一致
    ResetInputOverflowRetry(ctx.global_session_id);
    My_Log{} << "[GenieRoutingGateway] Cloud routing succeeded for input overflow request." << std::endl;

    // 成功路由到云端后，设置 sticky session
    if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
    {
        std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
        if ((int)sticky_sessions_.size() >= routing_config_.sticky_routing.max_sessions)
        {
            sticky_sessions_.erase(sticky_sessions_.begin());
        }
        StickyRouteEntry entry;
        // sticky 路由目标使用实际的 input_fallback_cloud_tier
        entry.target = (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
            ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
        entry.expires_at = std::chrono::steady_clock::now() +
                           std::chrono::seconds(routing_config_.sticky_routing.ttl_seconds);
        // 记录第一次 fallback 时的消息边界（与 HandleLocalOutputOverflow 保持一致）
        entry.fallback_msg_count = request.contains("messages") && request["messages"].is_array()
                                   ? (int)request["messages"].size() : 0;
        sticky_sessions_[ctx.session_id] = entry;
        My_Log{} << "[GenieRoutingGateway] Sticky route set after input overflow fallback for session="
                 << ctx.session_id << ", fallback_msg_count=" << entry.fallback_msg_count << std::endl;
    }

    // 使用实际路由目标
    RouteTarget input_route_target = (input_fallback_cloud_tier == CloudTier::ENTERPRISE)
        ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
    emit_audit(input_route_target, need_desensitize, desensitized,
               http_res.status, "local_input_overflow_routed_to_cloud", latency_ms);
    return true;
}
