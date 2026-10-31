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

class AuditLogger
{
public:
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
        bool rule_engine_failed = false;
        bool tool_output_escalation = false;
        bool latency_warning = false;
        bool oversized_field_warning = false;
        int oversized_fields_count = 0;
        bool keywords_dict_reloaded = false;
        int keywords_dict_rules_count = 0;
        bool local_output_overflow = false;
        bool local_input_overflow = false;
        bool sticky_route_hit = false;
        std::string cloud_tier = "local";
        int64_t cloud_prompt_tokens = 0;
        int64_t cloud_completion_tokens = 0;
        int64_t cloud_total_tokens = 0;
        bool incremental_check_used = false;
        int incremental_new_messages = 0;
        int total_messages_count = 0;
        std::string incremental_fallback_reason;
    };

    void Log(const AuditRecord &record);

private:
    static std::string VectorToJsonArray(const std::vector<std::string> &vec);

    void RecordMetrics(const AuditRecord &record);

    void OutputMetricsSummary();

    static int64_t CalcPercentile(std::vector<int64_t> samples, double percentile);

    RoutingConfig::MetricsConfig metrics_config_;

    mutable std::mutex metrics_mutex_;

    int total_requests_ = 0;

    int route_local_ = 0;
    int route_enterprise_cloud_ = 0;
    int route_public_cloud_ = 0;
    int route_cloud_ = 0;
    int route_error_ = 0;

    int s0_count_ = 0;
    int s1_count_ = 0;
    int s2_count_ = 0;

    int c0_count_ = 0;
    int c1_count_ = 0;
    int c2_count_ = 0;

    int desensitize_success_ = 0;
    int desensitize_fail_ = 0;
    std::unordered_map<std::string, int> fail_reason_counts_;

    std::vector<int64_t> latency_samples_;
    int latency_sample_pos_ = 0;

    int cloud_total_ = 0;
    int cloud_timeout_count_ = 0;
    int cloud_5xx_count_ = 0;
    int64_t cloud_prompt_tokens_total_ = 0;
    int64_t cloud_completion_tokens_total_ = 0;
    int64_t cloud_tokens_total_ = 0;

    int s2_block_count_ = 0;

    std::chrono::steady_clock::time_point last_summary_time_;
};

#endif
