//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef AUDIT_LOGGER_H
#define AUDIT_LOGGER_H

#include "../security/content_security_types.h"
#include "../../model/model_config.h"
#include <string>
#include <vector>
#include <cstdint>
#include <unordered_map>
#include <mutex>
#include <chrono>

// ============================================================
// AuditLogger：结构化审计日志记录器
// 使用现有 My_Log 输出结构化 JSON
// 严禁记录原文敏感内容
// 内存指标收集与定期汇总输出
// ============================================================
class AuditLogger
{
public:
    // 支持传入 MetricsConfig 以启用指标汇总
    explicit AuditLogger(const RoutingConfig::MetricsConfig &metrics_config = {});

    struct AuditRecord {
        std::string request_id;
        std::string timestamp;
        std::string session_id;
        RouteTarget route_decision = RouteTarget::LOCAL;
        SensitivityLevel sensitivity_level = SensitivityLevel::S0;
        ComplexityLevel complexity_level = ComplexityLevel::C0;
        bool desensitized = false;
        std::vector<std::string> desensitize_strategies;
        bool desensitize_success = false;
        std::string desensitize_failure_reason;
        std::string cloud_endpoint;
        int64_t latency_ms = 0;
        int http_status = 0;
        int retry_count = 0;
        std::vector<std::string> hit_rule_ids;
        std::vector<std::string> hit_categories;
        std::string route_reason;
        // 规则引擎异常标志
        // 当 ContentSecurityInspector::Inspect 的 try-catch 捕获到异常时设置为 true，
        // 表示规则引擎发生崩溃/异常，结果已强制升级为 S2（最严格）
        bool rule_engine_failed = false;
        // 工具输出回流门禁触发标志（对应需求文档 §5.4）
        // 若任意 role=="tool" 消息被判为 S2 导致整体等级强制升级，则为 true
        bool tool_output_escalation = false;
        // 延迟超出 P95 目标值告警标志（对应需求文档 §16）
        // 若 latency_ms 超出 P95 目标值（规则引擎 ≤50ms / 含辅助判断 ≤300ms），置为 true
        // 仅为可观测性手段，不触发降级或熔断（与需求文档 §16 "非硬约束"一致）
        bool latency_warning = false;
        // 超大字段扫描告警
        bool oversized_field_warning = false;
        int oversized_fields_count = 0;
        bool keywords_dict_reloaded = false;
        int keywords_dict_rules_count = 0;
        // 事后路由回退标志
        bool local_output_overflow = false;
        bool tool_call_retries_exceeded = false;
        // 预路由回退标志
        // true 表示本次请求因本地输入溢出（压缩后仍超出上下文窗口）触发预路由回退
        bool local_input_overflow = false;
        // 会话级路由锁定命中标志
        // true 表示本次请求命中了 session sticky 路由（跳过了 Step1~Step3 重新评估）
        bool sticky_route_hit = false;
        // 云端层级标识："local" / "enterprise_cloud" / "public_cloud" / "error"
        std::string cloud_tier = "local";
        // 云端 Token 使用量（仅 route_decision==ENTERPRISE_CLOUD/PUBLIC_CLOUD 时有效）
        // 0 表示未获取到 token 数据（云端未返回 usage 字段，或本次为本地路由）
        int64_t cloud_prompt_tokens = 0;
        int64_t cloud_completion_tokens = 0;
        int64_t cloud_total_tokens = 0;
        bool incremental_check_used = false;        // 是否使用了增量检查（false 表示全量检查）
        int incremental_new_messages = 0;           // 增量检查时的新消息数量（全量检查时为 0）
        int total_messages_count = 0;               // 完整历史消息总数（用于计算增量比例）
        std::string incremental_fallback_reason;    // 降级原因（若发生降级，如 "s2_detected"、"message_count_decreased"）
    };

    // 记录审计日志（同时更新内存指标）
    void Log(const AuditRecord &record);

private:
    // 将 vector<string> 转为 JSON 数组字符串
    static std::string VectorToJsonArray(const std::vector<std::string> &vec);

    // 更新内存指标（每次 Log 调用时触发）
    void RecordMetrics(const AuditRecord &record);

    // 输出指标汇总（以 JSON 格式写入日志）
    void OutputMetricsSummary();

    // 计算延迟百分位数（p50/p95）
    // 注意：此方法会对 latency_samples_ 的副本排序，不修改原始数据
    static int64_t CalcPercentile(std::vector<int64_t> samples, double percentile);

    // 指标配置
    RoutingConfig::MetricsConfig metrics_config_;

    // 内存指标状态（mutex 保护）
    mutable std::mutex metrics_mutex_;

    // 请求计数
    int total_requests_ = 0;

    // 路由分布
    int route_local_ = 0;
    int route_enterprise_cloud_ = 0;
    int route_public_cloud_ = 0;      // 公有云路由计数（原 route_cloud_）
    int route_cloud_ = 0;             // 兼容旧代码，等于 route_enterprise_cloud_ + route_public_cloud_
    int route_error_ = 0;

    // 敏感等级分布
    int s0_count_ = 0;
    int s1_count_ = 0;
    int s2_count_ = 0;

    // 复杂度分布
    int c0_count_ = 0;
    int c1_count_ = 0;
    int c2_count_ = 0;

    // 脱敏统计
    int desensitize_success_ = 0;
    int desensitize_fail_ = 0;
    std::unordered_map<std::string, int> fail_reason_counts_;  // 失败原因 → 计数

    // 延迟样本（环形缓冲区，大小由 metrics_config_.latency_sample_size 控制）
    std::vector<int64_t> latency_samples_;
    int latency_sample_pos_ = 0;  // 环形缓冲区写入位置

    // 云端统计
    int cloud_total_ = 0;
    int cloud_timeout_count_ = 0;  // latency_warning=true 的云端请求
    int cloud_5xx_count_ = 0;      // http_status >= 500 的云端请求
    // 云端 Token 消耗累计统计（跨所有请求）
    int64_t cloud_prompt_tokens_total_ = 0;
    int64_t cloud_completion_tokens_total_ = 0;
    int64_t cloud_tokens_total_ = 0;

    // S2 拦截统计
    int s2_block_count_ = 0;  // sensitivity=S2 且 route=POLICY_ERROR 的请求

    // 上次汇总输出时间（用于按时间触发）
    std::chrono::steady_clock::time_point last_summary_time_;
};

#endif // AUDIT_LOGGER_H
