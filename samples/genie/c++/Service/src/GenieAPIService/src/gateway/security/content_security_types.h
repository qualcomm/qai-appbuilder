//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CONTENT_SECURITY_TYPES_H
#define CONTENT_SECURITY_TYPES_H

#include <string>
#include <vector>
#include <unordered_map>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

// ============================================================
// 敏感等级
// S0: 无敏感内容
// S1: 中等敏感（PII、FINANCIAL 等）
// S2: 高度敏感（SECRET、私钥等）
// ============================================================
// ============================================================
// 敏感实体类型
// ============================================================
enum class SensitiveEntityType {
    PHONE, EMAIL, IDCARD, BANKCARD, API_KEY, TOKEN, PRIVATE_KEY, PASSWORD_VALUE,
    PASSWORD_KW, INTERNAL_URL, PATH, DEVICE_ID,
    KEYWORD,   // 关键词词典命中（通用，用于 structured_placeholder 脱敏）
    UNKNOWN
};

// ============================================================
// 敏感命中片段
// 仅内存、仅本请求生命周期，不得序列化、不得落盘、不得回传
// ============================================================
struct SensitiveSpan {
    SensitiveEntityType type = SensitiveEntityType::UNKNOWN;
    std::string rule_id;        // e.g. R_PHONE_CN
    size_t start = 0;           // byte offset in utf-8 string（相对于所在字段的偏移）
    size_t end = 0;             // [start, end)
    std::string matched;        // matched substring (keep in memory only, never log)
    bool from_tool_output = false; // role==tool
    std::string field_path;     // JSON Pointer 风格，如 "/messages/0/content"
};

enum class SensitivityLevel { S0, S1, S2 };

// ============================================================
// 复杂性等级
// C0: 简单任务
// C1: 中等复杂
// C2: 高度复杂（需要云端处理）
// ============================================================
enum class ComplexityLevel { C0, C1, C2 };

// ============================================================
// 路由目标
// ============================================================
// 注意：Windows 头文件中 ERROR 被定义为宏（值为 0）。
// 此处使用 POLICY_ERROR 作为枚举值名称。
//
// 原 CLOUD 拆分为两个层级：
//   ENTERPRISE_CLOUD：企业内网模型服务器（C1 级别任务）
//   PUBLIC_CLOUD    ：外部公有云模型（C2 级别任务）
enum class RouteTarget {
    LOCAL,              // 本地端侧模型（C0 级别任务）
    ENTERPRISE_CLOUD,   // 企业内网模型服务器（C1 级别任务）
    PUBLIC_CLOUD,       // 外部公有云模型（C2 级别任务）
    POLICY_ERROR        // 策略拒绝（403/503）
};

// ============================================================
// 敏感检测结果
// ============================================================
struct InspectionResult {
    SensitivityLevel sensitivity_level = SensitivityLevel::S0;
    std::vector<std::string> hit_categories;   // 例如 ["PII","SECRET"]
    std::vector<std::string> hit_rule_ids;     // 例如 ["R_PHONE_CN"]
    std::string model_confidence;              // "low|med|high"
    std::string summary_reason;
    // 规则引擎异常标志：当 Inspect 的 try-catch 捕获到异常时为 true，
    // 表示规则引擎崩溃，结果已强制升级为 S2（最严格）
    bool rule_engine_failed = false;
    // 工具输出回流门禁触发标志（对应需求文档 §5.4）
    // ContentSecurityInspector::Inspect 在检测到任意 role=="tool" 消息被判为 S2
    // 导致整体等级强制升级时，将此字段置为 true；
    // GenieRoutingGateway::Step6_Audit 读取此字段写入 AuditRecord::tool_output_escalation
    bool tool_output_escalation = false;
    // 超大字段扫描告警
    bool oversized_field_warning = false;
    int oversized_fields_count = 0;
    // 敏感命中片段列表（仅内存，不序列化，不写日志）
    // 仅当 InspectionContext.collect_spans=true 时填充
    std::vector<SensitiveSpan> spans;
    // 关键词词典热更新标志（仅本次请求）
    bool keywords_dict_reloaded = false;
    // 本次检测加载的词典规则数量
    int keywords_dict_rules_count = 0;
};

// ============================================================
// 脱敏结果
// ============================================================
struct DesensitizationResult {
    bool success = false;
    json desensitized_request;                 // 脱敏后的完整请求（含 messages）
    std::vector<std::string> applied_strategies;
    std::string failure_reason;
    // 结构化占位符映射表（仅内存，不序列化，不落盘，不回传）
    // key: 占位符（如 "<PHONE_1>"）或 Mock Data（如 "mock_user_1@example.com"），
    // value: 原始敏感值（严禁写入日志）
    std::unordered_map<std::string, std::string> mapping_table;
    // 是否使用格式保留脱敏（用于标识 mapping_table 的 key 类型）
    bool use_format_preserving = false;
};

// ============================================================
// 复杂性评估结果
// ============================================================
struct ComplexityResult {
    ComplexityLevel complexity_level = ComplexityLevel::C0;
    std::string reason;
};

// ============================================================
// 路由决策结果
// ============================================================
struct RouteDecision {
    RouteTarget target = RouteTarget::LOCAL;
    SensitivityLevel sensitivity = SensitivityLevel::S0;
    ComplexityLevel complexity = ComplexityLevel::C0;
    std::string policy_id;
    bool need_desensitize = false;
    std::string decision_reason;
    // 原 cloud_available 拆分为两个字段
    bool enterprise_cloud_available = false;  // 企业内网云可用性
    bool public_cloud_available = false;      // 外部公有云可用性
    bool local_available = true;
    std::vector<std::string> fallbacks_applied;
    // 当 target == POLICY_ERROR 时使用，对应 §10.3 错误码字典
    // 取值：sensitive_content_local_unavailable / desensitization_failed /
    //       all_routes_unavailable / cloud_unavailable_no_fallback
    std::string error_code;
    // 当 target == POLICY_ERROR 时使用，对应 HTTP 状态码（403 或 503）
    int error_http_status = 0;
};

// ============================================================
// 内部检测上下文标志（防递归）
// ============================================================
struct InspectionContext {
    bool is_internal_inspection = false;
    // rule_engine_only=true 时仅运行规则引擎/词典，不触发本地模型辅助判断
    bool rule_engine_only = false;
    // collect_spans=true 时 Inspector 在规则命中时输出 InspectionResult.spans
    // 默认 false（不产出 spans，以降低开销）
    bool collect_spans = false;
    std::string request_id;

    // ── 服务端生成的两级 Session ID（不依赖客户端传入）──────────────────────
    // session_id（user_session_id）：
    //   用于 sticky_routing + rate_limit。
    //   生命周期：从最新一条 user 消息开始，直到下一条 user 消息到来。
    //   每次请求的最后一条消息是 user 时，生成新的 user_session_id；
    //   否则（工具调用轮次）沿用上一个 user_session_id。
    std::string session_id;

    // global_session_id：
    //   用于 incremental_check（会话级安全状态缓存）。
    //   生命周期：从"过滤 system/developer 后只有1条 user 消息"时开始，
    //   直到下一次满足该条件（客户端清空历史，开始新会话）。
    //   在同一 global session 内，incremental_check 可复用历史安全检查结果。
    std::string global_session_id;
};

// ============================================================
// 辅助函数：枚举转字符串
// ============================================================
inline std::string to_string(SensitivityLevel level)
{
    switch (level)
    {
        case SensitivityLevel::S0: return "S0";
        case SensitivityLevel::S1: return "S1";
        case SensitivityLevel::S2: return "S2";
        default: return "S0";
    }
}

inline std::string to_string(ComplexityLevel level)
{
    switch (level)
    {
        case ComplexityLevel::C0: return "C0";
        case ComplexityLevel::C1: return "C1";
        case ComplexityLevel::C2: return "C2";
        default: return "C0";
    }
}

inline std::string to_string(RouteTarget target)
{
    switch (target)
    {
        case RouteTarget::LOCAL:            return "LOCAL";
        case RouteTarget::ENTERPRISE_CLOUD: return "ENTERPRISE_CLOUD";
        case RouteTarget::PUBLIC_CLOUD:     return "PUBLIC_CLOUD";
        case RouteTarget::POLICY_ERROR:     return "POLICY_ERROR";
        default: return "LOCAL";
    }
}

#endif // CONTENT_SECURITY_TYPES_H
