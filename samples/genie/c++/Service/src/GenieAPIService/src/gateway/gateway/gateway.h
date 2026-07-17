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

// ============================================================
// 会话级路由锁定条目
// 记录某个 session 的路由锁定状态（目标路由 + 过期时间）
// ============================================================
struct StickyRouteEntry {
    RouteTarget target = RouteTarget::LOCAL;
    SensitivityLevel sensitivity = SensitivityLevel::S0;
    std::chrono::steady_clock::time_point expires_at;
    // 第一次 fallback 时客户端发来的消息总数（本地/云端历史边界）
    // 用于 CleanLocalHistoryForCloudFallback：只清理索引 < fallback_msg_count 的本地历史，
    // 保留索引 >= fallback_msg_count 的云端历史（云端模型已完成的工具调用不应被清除）。
    // 0 表示尚未记录边界（第一次 fallback 时使用，清理全部本地历史）。
    int fallback_msg_count = 0;
};

// ============================================================
// 服务端生成的两级 Session ID 状态条目
// 以"历史消息前缀哈希"为 key，记录当前会话的两个 session ID：
//   - user_session_id：用于 sticky_routing + rate_limit
//   - global_session_id：用于 incremental_check
// ============================================================
struct ServerSessionEntry {
    std::string user_session_id;    // 用于 sticky_routing + rate_limit
    std::string global_session_id;  // 用于 incremental_check
    std::chrono::steady_clock::time_point last_updated;
    // 格式保留脱敏的映射表（Mock Data -> 原始数据）
    // 仅内存，不序列化，不落盘，Session 到期自动清理
    std::unordered_map<std::string, std::string> desensitization_mapping;
};

// ============================================================
// 云端推理限流：每轮任务（session）的推理计数器
// 记录当前任务已消耗的推理次数和 Token 数（含分项统计）
// ============================================================
struct CloudRateLimitCounter {
    int inference_count = 0;        // 已执行的推理（工具调用）次数
    int64_t prompt_tokens = 0;      // 已消耗的 Prompt（输入）Token 数
    int64_t completion_tokens = 0;  // 已消耗的 Completion（输出）Token 数
    int64_t total_tokens = 0;       // 已消耗的总 Token 数（prompt + completion）
    std::chrono::steady_clock::time_point last_updated;
};

// ============================================================
// 安全检查范围枚举
// 仅在 GenieRoutingGateway 内部使用，不需要跨组件共享
// ============================================================
enum class CheckScope {
    FULL,        // 全量检查（完整历史）
    INCREMENTAL  // 增量检查（仅最新消息 + 工具输出）
};

// ============================================================
// 会话级安全检查状态（内存缓存，不持久化）
// 用于跟踪每个 session 的历史敏感等级和已检查消息数量，
// 以支持增量检查（只检查新增消息，而不是完整历史）
// ============================================================
struct SessionSecurityState {
    // 历史最高敏感等级（S0/S1/S2）
    SensitivityLevel max_sensitivity_seen = SensitivityLevel::S0;

    // 已检查的消息数量（用于确定增量起点 + 检测历史篡改）
    int checked_message_count = 0;

    // 最后更新时间（用于 TTL 过期清理）
    std::chrono::steady_clock::time_point last_updated;

    // 是否启用增量模式（false 表示需要全量检查）
    bool incremental_mode_enabled = true;

    // 降级原因（用于审计日志）
    std::string fallback_reason;

    // S2 轮次范围记录
    // 每个 S2 轮的消息索引范围 [start_idx, end_idx)：
    //   start_idx = 该轮第一条 user 消息在 messages 数组中的索引（含）
    //   end_idx   = 下一轮第一条 user 消息的索引（不含），或历史末尾
    // 当新轮次无 S2 时，可将这些范围内的消息从历史中清洗掉，再上云。
    struct S2TurnRange {
        int start_idx = 0;  // 该轮起始 user 消息的索引（含）
        int end_idx = 0;    // 该轮结束位置（不含，即下一轮 user 消息的索引）
    };
    std::vector<S2TurnRange> s2_turn_ranges;
};

// ============================================================
// GenieRoutingGateway：内容安全检查 + 智能路由总编排器
//
// 编排流程（实际执行顺序）：
//   Step1: 敏感检测（基于原文）
//   Step2: 复杂性评估（基于原文）
//   Step3: 路由决策
//   Step4: 生成云端副本并脱敏（按需，仅 CLOUD 路径）
//   Step5: 执行（LOCAL / CLOUD / ERROR）
//   Step6: 审计日志
// ============================================================
// ============================================================
// 云端层级枚举（仅 GenieRoutingGateway 内部使用）
// 用于 ExecuteCloudRequest 区分企业内网云和外部公有云
// ============================================================
enum class CloudTier {
    ENTERPRISE,  // 企业内网云（对应 ENTERPRISE_CLOUD 路由目标）
    PUBLIC       // 外部公有云（对应 PUBLIC_CLOUD 路由目标）
};

// ============================================================
// 客户端来源枚举
//
// 识别策略：
//   QAI_AGENT_FORGE  - 优先检测 HTTP 请求头 "X-Genie-Client: QAIAgentForge"
//   OPENCLAW         - 内容特征兜底：system prompt 含 "Sender (untrusted metadata):"
//                      或 OpenClaw 时间戳前缀 "[Tue 2026..."
//   UNKNOWN          - 无法识别（按 OPENCLAW 的 PASSTHROUGH 策略处理公网云，
//                      其余路由走 OPTIMIZED）
// ============================================================
enum class ClientSource {
    UNKNOWN,          // 无法识别来源
    QAI_AGENT_FORGE,  // QAIAgentForge 客户端（通过请求头识别）
    OPENCLAW          // OpenClaw 客户端（通过内容特征识别）
};

// ============================================================
// 提示词处理策略枚举
//
// OPTIMIZED    - 走统一提示词优化流水线（重建 system prompt + 压缩消息）
// PASS_THROUGH - 直接透传原始 messages，不做任何优化
// ============================================================
enum class PromptProcessingPolicy {
    OPTIMIZED,     // 统一优化：重建 system prompt + 工具过滤 + 消息压缩
    PASS_THROUGH   // 原始透传：直接发送 original_request.messages
};

class GenieRoutingGateway
{
public:
    GenieRoutingGateway(IModelConfig &model_config,
               const RoutingConfig &routing_config,
               const CloudModelConfig &cloud_config,
               const EnterpriseCloudModelConfig &enterprise_cloud_config);

    // 析构函数：将 alive_ 标志设为 false，通知所有 detach 后台线程安全退出
    ~GenieRoutingGateway();

    // 主入口：替代原 ChatCompletions 中的本地调用逻辑
    // 返回 false 表示需要直接返回错误（S2+本地不可用 或 S1+脱敏失败+本地不可用 场景）
    // handled_by_cloud=true 表示已由云端处理，调用方无需继续本地流程
    bool HandleChatCompletion(const json &request,
                               const httplib::Request &http_req,
                               httplib::Response &http_res,
                               ResponseDispatcher &dispatcher,
                               ModelInputBuilder &input_builder,
                               bool &handled_by_cloud);

    // 事后路由回退（本地能力不足）
    // 当本地推理完成后（或在处理前预判到工具调用超限），检测到输出溢出或工具调用超限时调用。
    // 返回 true 表示成功回退到云端，http_res 已被重写；
    // 返回 false 表示因策略（S2）或云端不可用等原因未能回退，调用方应保留本地结果。
    // sink != nullptr 表示调用方已在流式响应中（set_chunked_content_provider 回调内），
    // 此时可直接向现有 sink 写入云端 SSE 数据，无需重新设置 chunked provider。
    bool HandleLocalOutputOverflow(const json &request,
                                   const httplib::Request &http_req,
                                   httplib::Response &http_res,
                                   bool is_tool_call_retries_exceeded,
                                   httplib::DataSink *sink = nullptr);

    // 预路由回退（本地输入溢出）
    // 当 ModelInputBuilder::Build 因压缩后仍超出上下文窗口而抛出异常时调用。
    // 与 HandleLocalOutputOverflow 的区别：此时本地推理尚未开始，无截断响应可保留，
    // 必须路由云端或返回错误（503）。
    // 返回 true 表示已处理（云端成功或已写入错误响应），调用方直接 return；
    // 返回 false 表示 routing 未启用，调用方走原有异常处理逻辑。
    bool HandleLocalInputOverflow(const json &request,
                                  const httplib::Request &http_req,
                                  httplib::Response &http_res,
                                  ResponseDispatcher &dispatcher);

    // 检查指定 session 是否已锁定到云端路由（sticky CLOUD session）
    // 用于在 tool_call_retries 检查时跳过对云端路由请求的限制：
    //   - 若路由功能未启用或 session_id 为空，返回 false
    //   - 若 sticky session 存在且未过期且目标为 CLOUD，返回 true
    //   - 否则返回 false
    // 此方法仅做内存查询，无 Step1/Step2/Step3 开销
    bool IsSessionStickyToCloud(const std::string &session_id);

    // 服务端 Session ID 生成入口（替代从 request 读取 session_id）
    // 根据请求消息历史，生成/查找两级 session ID，注入到 ctx 中：
    //   ctx.session_id        → user_session_id（用于 sticky_routing + rate_limit）
    //   ctx.global_session_id → global_session_id（用于 incremental_check）
    // request：完整的请求 JSON（含 messages 数组）
    void ResolveServerSessionIds(const json &request, InspectionContext &ctx);

    // 设置本地可用性检测函数（用于测试注入或未来动态模型卸载场景）
    // 默认实现：检查 model_config.get_genie_model_handle().lock() != nullptr
    // 测试时可通过注入返回 false 的函数，以验证 S2+本地不可用（HTTP 403）等场景：
    //   agent->SetLocalAvailabilityChecker([]{ return false; });
    void SetLocalAvailabilityChecker(std::function<bool()> checker);

private:
    // 步骤 1：敏感检测
    InspectionResult Step1_Inspect(const json &request,
                                    const InspectionContext &ctx);

    // 步骤 2：复杂性评估（基于原文）
    ComplexityResult Step2_EvaluateComplexity(const json &request,  
                                               const InspectionContext &ctx);

    // 步骤 3：路由决策
    RouteDecision Step3_Route(const InspectionResult &inspection,
                               const ComplexityResult &complexity,
                               const std::string &agent_type = "main");

    // 步骤 4：生成云端副本并脱敏
    // 传入 ctx 进行二次扫描
    DesensitizationResult Step4_Desensitize(const json &request,
                                             const InspectionResult &inspection,
                                             const InspectionContext &ctx);

    // 步骤 5：执行（LOCAL / CLOUD / ERROR）
    // user_session_id：服务端生成的 user_session_id，传递给 ExecuteCloudRequest 用于 rate_limit
    bool Step5_Execute(const RouteDecision &decision,
                        const json &original_request,
                        const DesensitizationResult &desensitized,
                        const httplib::Request &http_req,
                        httplib::Response &http_res,
                        ResponseDispatcher &dispatcher,
                        ModelInputBuilder &input_builder,
                        bool &handled_by_cloud,
                        const std::string &user_session_id);

    // 辅助执行云端请求（提取自 Step5_Execute）
    // sink != nullptr 时，流式响应同步写入现有 sink（已在 chunked provider 回调中）；
    // sink == nullptr 时，通过 http_res.set_chunked_content_provider 异步发起新流。
    // initial_status / initial_message：流式场景下第一次 provider 调用时发送的状态事件；
    //   默认为 "inference" / "正在云端推理..."，可由调用方覆盖（如 cloud_fallback 场景）。
    // user_session_id：服务端生成的 user_session_id（用于 rate_limit 计数），
    //   替代原来从 cloud_request["session_id"] 读取的方式（客户端不传 session_id）。
    // cloud_tier：指定使用企业云还是公有云客户端（默认 PUBLIC）
    bool ExecuteCloudRequest(const json &cloud_request,
                             bool is_stream,
                             httplib::Response &http_res,
                             bool &handled_by_cloud,
                             const std::string &user_session_id,
                             httplib::DataSink *sink = nullptr,
                             const std::string &initial_status = "inference",
                             const std::string &initial_message = "正在云端推理...",
                             CloudTier cloud_tier = CloudTier::PUBLIC);

    // Step5_Execute 中 ENTERPRISE_CLOUD / PUBLIC_CLOUD 分支共用的执行逻辑
    // （两者结构一致，仅配置对象、路由目标和日志文案不同，按 tier 参数化）
    bool ExecuteCloudTierCase(CloudTier tier,
                               bool need_desensitize,
                               const json &original_request,
                               const DesensitizationResult &desensitized,
                               httplib::Response &http_res,
                               bool &handled_by_cloud,
                               const std::string &user_session_id);

    // 步骤 6：审计
    // 新增 sticky_route_hit 参数，记录是否命中 session sticky 路由
    // cloud_prompt_tokens/cloud_completion_tokens/cloud_total_tokens：云端 Token 使用量（0 表示未获取）
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

    // 生成策略拒绝/服务不可用错误响应
    // code: 对应 §10.3 错误码字典（sensitive_content_local_unavailable / desensitization_failed /
    //       all_routes_unavailable / cloud_unavailable_no_fallback）
    // http_status: 403（策略拒绝）或 503（服务不可用）
    // is_stream: 若为 true，以 SSE 格式发送错误（data: {...}\n\ndata: [DONE]\n\n），
    //            确保流式请求的客户端能正确接收错误信息
    void SendPolicyViolationError(httplib::Response &res,
                                   const std::string &code,
                                   const std::string &message,
                                   int http_status = 403,
                                   bool is_stream = false);

    // 处理云端响应，转换为标准格式返回给客户端
    bool HandleCloudResponse(const json &cloud_response,
                              httplib::Response &http_res);

    // 生成 request_id
    static std::string GenerateRequestId();

    // 格式化当前 UTC 时间为审计日志时间戳字符串（ISO8601，如 "2026-07-08T07:27:00Z"）
    static std::string FormatAuditTimestampUtc();

    // 清理本地历史记录（事后路由回退时使用）
    // 将 messages 中本地模型产生的中间 tool_calls/tool 消息剔除，
    // 只保留最后一条 user 消息及之前的对话历史，以及不含 tool_calls 的 assistant 文本回复。
    //
    // fallback_boundary 参数说明：
    //   = 0（默认）：第一次 fallback，清理 last_user_idx 之后所有 tool/tool_calls 消息
    //   > 0：sticky session 命中，只清理 last_user_idx 之后且索引 < fallback_boundary 的本地历史，
    //        保留索引 >= fallback_boundary 的云端历史（云端模型已完成的工具调用不应被清除）
    //
    // ⚠️ 调用约束：
    //   - 第一次 fallback（HandleLocalOutputOverflow / HandleLocalInputOverflow）：传 fallback_boundary=0
    //   - sticky session 命中（HandleChatCompletion）：传 sticky_entry.fallback_msg_count
    static json CleanLocalHistoryForCloudFallback(const json &request, int fallback_boundary = 0);

    // 从历史消息中清洗掉所有 S2 轮次的消息
    // 参数：
    //   request       - 原始请求（含完整 messages 数组）
    //   s2_turn_ranges - 需要清洗的 S2 轮次范围列表（每个范围为 [start_idx, end_idx)）
    //   log_details   - 是否输出详细日志
    // 返回：清洗后的请求副本（messages 中不含 S2 轮次的消息）
    // 注意：清洗后会在被删除轮次的位置插入一条 system 占位消息，保持上下文连贯性
    static json PurgeS2TurnsFromHistory(
        const json &request,
        const std::vector<SessionSecurityState::S2TurnRange> &s2_turn_ranges,
        bool log_details = false);

    // 从 session 状态中读取 S2 轮次范围，修正 end_idx，执行清洗，并重置 session 状态
    // 参数：
    //   request           - 原始请求（含完整 messages 数组）
    //   global_session_id - 用于查找 session_security_states_ 的 key
    //   current_turn_start - 当前轮起始 user 消息的索引（用于修正最后一个 S2 轮次的 end_idx）
    //                        -1 表示不修正（保持原始 end_idx）
    //   new_max_sensitivity - 清洗后 session 的 max_sensitivity_seen 重置值
    //   log_tag           - 日志标签（用于区分调用来源，如 "Cloud-only S0"）
    // 返回：清洗后的请求副本（若无 S2 轮次记录则返回原始请求副本）
    // 副作用：清洗成功后重置 session 的 s2_turn_ranges、max_sensitivity_seen、incremental_mode_enabled
    json CleanS2TurnsForCloud(
        const json &request,
        const std::string &global_session_id,
        int current_turn_start,
        SensitivityLevel new_max_sensitivity,
        const std::string &log_tag);

    // [Cloud-only 模式] 对请求执行脱敏（主 Agent 场景自动过滤/插回 system/developer 消息）
    // 参数：
    //   request      - 待脱敏的请求（已清洗 S2 轮次）
    //   inspection   - 敏感检测结果（用于脱敏策略）
    //   ctx          - 检测上下文（含 session_id 等）
    //   agent_type   - agent 类型（"main" 或 "sub"）
    //   log_tag      - 日志标签（如 "S1" 或 "S2"）
    // 返回：脱敏结果（success=false 表示脱敏失败）
    DesensitizationResult DesensitizeForCloudOnly(
        const json &request,
        const InspectionResult &inspection,
        const InspectionContext &ctx,
        const std::string &agent_type,
        const std::string &log_tag);

    // 过滤 system/developer 消息（仅 agent_type == "main" 时）
    // 主 Agent 场景下，system/developer 消息由 GenieAPIService 自动重构，
    // 不含用户数据（但可能含内网 IP、路径等服务配置），无需参与安全检查和复杂度评估。
    // 子 Agent（agent_type=sub）的 system prompt 由外部提供，必须保留检查。
    // 参数：
    //   request    - 原始请求（含 messages 数组）
    //   agent_type - agent 类型（"main" 或 "sub"）
    // 返回：过滤后的请求副本（agent_type != "main" 时返回原始请求副本）
    static json FilterSystemMessagesForInspection(const json &request, const std::string &agent_type);

    // 在 Step4_Desensitize 执行完毕后调用，将本轮产生的 mapping_table 合并到 Session
    void MergeDesensitizationMapping(const std::string &session_id,
                                      const std::unordered_map<std::string, std::string> &mapping);
    
    // 获取 Session 的脱敏映射表（用于响应还原）
    // 返回空 map 表示 Session 不存在或无映射表
    std::unordered_map<std::string, std::string> GetDesensitizationMapping(const std::string &session_id) const;
    
    // 非流式响应还原：将 Mock Data 替换回原始数据
    void RestoreCloudResponse(json &response,
                              const std::unordered_map<std::string, std::string> &mapping) const;

    // 链式恢复字符串中的脱敏值，直到稳定或达到最大轮数。
    // 用于：
    //   1. 合并 session 映射表时，将 real 归一化到最终原文；
    //   2. 流式/非流式响应恢复时，处理多轮脱敏产生的嵌套 mock。
    std::string RestoreStringFully(const std::string &text,
                                   const std::unordered_map<std::string, std::string> &mapping) const;
    
    IModelConfig &model_config_;
    ContentSecurityInspector inspector_;
    Desensitizer desensitizer_;
    TaskComplexityEvaluator complexity_evaluator_;
    ModelRouter router_;
    CloudModelClient cloud_client_;                      // 外部公有云客户端
    CloudModelClient enterprise_cloud_client_;           // 企业内网云客户端
    AuditLogger audit_logger_;
    RoutingConfig routing_config_;
    CloudModelConfig cloud_config_;                      // 公有云配置副本（用于限流等）
    EnterpriseCloudModelConfig enterprise_cloud_config_; // 企业云配置副本
    LocalModelConfig local_model_config_;                // 本地模型配置（用于开关控制）

    // 统一提示词预处理服务（用于云端路由的 OPTIMIZED 策略）
    // 在 Step5_Execute 中按需调用，对 QAIAgentForge 和 OpenClaw→企业云 路径执行提示词优化
    PromptPreparationService prompt_prep_service_;

    // 本地可用性检测函数（可注入，用于测试和未来动态检测）
    // 默认实现：检查 model_manager.get_genie_model_handle().lock() != nullptr
    std::function<bool()> local_availability_checker_;

    // 会话级路由锁定状态（session_id → StickyRouteEntry）
    // 仅内存，服务重启后丢失（可接受，新 session 重新评估）
    std::unordered_map<std::string, StickyRouteEntry> sticky_sessions_;
    std::mutex sticky_sessions_mutex_;

    // 云端推理限流：session 级别的推理计数器（session_id → CloudRateLimitCounter）
    // 每轮任务（session）独立计数，超出配置上限后返回错误
    std::unordered_map<std::string, CloudRateLimitCounter> rate_limit_counters_;
    std::mutex rate_limit_mutex_;

    // 检查推理次数和 Token 数限流，并递增推理计数器
    // 返回 false 表示已超限，http_res 已写入 429 错误响应
    // session_id 为空时跳过限流检查（无 session 场景）
    // cloud_tier 用于选择对应云端的 rate_limit 配置
    bool CheckAndIncrementInferenceCount(const std::string &session_id,
                                          httplib::Response &http_res,
                                          CloudTier cloud_tier = CloudTier::PUBLIC);

    // 更新 Token 消耗计数（响应完成后调用，供下次推理请求的限流检查使用）
    // 始终返回 true（Token 超限在下次 CheckAndIncrementInferenceCount 时拦截）
    // prompt_tokens/completion_tokens 为分项统计（可选，0 表示未知）
    // cloud_tier 用于选择对应云端的 rate_limit 配置
    bool UpdateAndCheckTokenCount(const std::string &session_id,
                                   int64_t tokens_used,
                                   httplib::Response &http_res,
                                   int64_t prompt_tokens = 0,
                                   int64_t completion_tokens = 0,
                                   CloudTier cloud_tier = CloudTier::PUBLIC);

    // 重置指定 session 的限流计数器（可选，用于提前释放内存）
    // 注意：CheckAndIncrementInferenceCount 已内置基于 TTL 的自动清理机制，
    // 过期的 counter 会在下次推理请求时自动清除，无需手动调用此函数。
    // 此函数主要用于测试或需要立即释放内存的场景。
    void ResetRateLimitCounter(const std::string &session_id);

    // 存活标志：用于保护 detach 后台线程的生命周期安全。
    // 后台线程捕获此 shared_ptr，在访问 cloud_client_ 前检查标志是否仍为 true。
    // GenieRoutingGateway 析构时将其设为 false，防止已析构后的 Use-After-Free。
    std::shared_ptr<std::atomic<bool>> alive_;

    // ============================================================
    // 会话级安全检查状态缓存（global_session_id → state）
    // 与 sticky_sessions_ 并列，独立管理，服务不同目的：
    //   sticky_sessions_：控制路由目标（LOCAL/CLOUD），key = user_session_id
    //   session_security_states_：控制安全检查范围（全量/增量），key = global_session_id
    // ============================================================
    std::unordered_map<std::string, SessionSecurityState> session_security_states_;
    mutable std::mutex session_security_mutex_;

    // ============================================================
    // 本地输入溢出重试计数（user_session_id → 重试次数）
    // 用于限制云端可恢复错误（503）的重试次数，防止客户端无限重试。
    // 超出 max_input_overflow_retries 后，将 503 升级为 422（永久失败）。
    // ============================================================
    struct InputOverflowRetryEntry {
        int retry_count = 0;                                        // 已重试次数
        std::chrono::steady_clock::time_point last_retry;           // 最后一次重试时间
    };
    std::unordered_map<std::string, InputOverflowRetryEntry> input_overflow_retries_;
    std::mutex input_overflow_retries_mutex_;

    // 检查并递增输入溢出重试计数
    // 返回 true：未超限，可继续返回 503；返回 false：已超限，应返回 422
    // session_id 为空时始终返回 true（无 session 场景不限制）
    bool CheckAndIncrementInputOverflowRetry(const std::string &session_id);

    // 重置指定 session 的输入溢出重试计数（成功路由到云端后调用）
    void ResetInputOverflowRetry(const std::string &session_id);

    // 决定本次请求的安全检查范围（全量 or 增量）
    // out_last_checked_count：输出参数，上次检查的消息数量（增量起点）
    // 注意：使用 global_session_id 查找 session_security_states_
    CheckScope DetermineCheckScope(const json &request,
                                   const std::string &global_session_id,
                                   int &out_last_checked_count);

    // 提取需要检查的增量内容（仅包含新增消息，不包含 tools 定义）
    static json ExtractIncrementalContent(const json &request, int last_checked_count);

    // 检测新消息中是否存在引用历史敏感信息的模式
    // 仅在 max_sensitivity_seen == S1 时调用（S0 无敏感历史，S2 已强制全量）
    static bool DetectSensitiveReference(const json &incremental_messages);

    // 更新会话安全状态（每次安全检查完成后调用）
    // 注意：使用 global_session_id 作为 key
    // turn_start_idx：当前轮次起始 user 消息的索引（-1 表示不记录轮次范围，用于 cloud-only 路径）
    void UpdateSessionSecurityState(const std::string &global_session_id,
                                    const InspectionResult &inspection,
                                    int current_message_count,
                                    int turn_start_idx = -1);

    // ============================================================
    // 服务端 Session ID 管理
    // 以"历史消息前缀哈希"为 key，维护两级 session ID 的映射关系。
    // 不依赖客户端传入 session_id，完全由服务端根据消息历史生成。
    // ============================================================

    // 服务端 session 状态表（history_fingerprint → ServerSessionEntry）
    // history_fingerprint：对除最后一条消息外的所有非 system/developer 消息计算的哈希
    std::unordered_map<std::string, ServerSessionEntry> server_sessions_;
    mutable std::mutex server_sessions_mutex_;

    // 计算历史消息前缀哈希（用于识别同一会话的连续请求）
    // 对 messages 数组中除最后一条外的所有非 system/developer 消息的 role+content 计算哈希。
    // 同一会话的连续请求（客户端每次携带完整历史，只追加新消息）会产生相同的哈希。
    // 返回空字符串表示无历史（只有一条消息或消息为空）。
    static std::string ComputeHistoryFingerprint(const json &messages);

    // 生成唯一的 session ID（时间戳 + 随机数，格式与 GenerateRequestId 类似）
    static std::string GenerateSessionId();

    // ============================================================
    // 辅助函数：从请求中检测 Agent 类型
    // 通过检测 system prompt 中是否包含 "agent=main" 来判断
    // ============================================================
    std::string DetectAgentTypeFromRequest(const json& request);

    // ============================================================
    // 客户端来源识别
    //
    // 识别优先级：
    //   1. HTTP 请求头 "X-Genie-Client: QAIAgentForge" → QAI_AGENT_FORGE
    //   2. system prompt 含 OpenClaw 元数据块特征 → OPENCLAW
    //   3. 无法识别 → UNKNOWN
    //
    // 注意：此函数在 HandleChatCompletion 入口处调用一次，结果存入 client_source_，
    // 供本次请求的所有后续步骤复用，避免重复解析。
    // ============================================================
    static ClientSource DetectClientSource(const httplib::Request &http_req,
                                           const json &request);

    // ============================================================
    // 提示词处理策略决策
    //
    // 策略矩阵：
    //   QAI_AGENT_FORGE + 任意路由目标  → OPTIMIZED
    //   OPENCLAW        + LOCAL          → OPTIMIZED
    //   OPENCLAW        + ENTERPRISE     → OPTIMIZED
    //   OPENCLAW        + PUBLIC_CLOUD   → PASS_THROUGH（保持原有行为）
    //   UNKNOWN         + PUBLIC_CLOUD   → PASS_THROUGH（保守兜底）
    //   UNKNOWN         + 其他           → OPTIMIZED
    // ============================================================
    static PromptProcessingPolicy ResolvePromptPolicy(ClientSource source,
                                                       RouteTarget target);

    // 当前请求的客户端来源（在 HandleChatCompletion 入口处设置，供后续步骤复用）
    ClientSource client_source_ = ClientSource::UNKNOWN;
};

#endif // GATEWAY_H
