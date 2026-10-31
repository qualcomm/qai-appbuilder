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

enum class SensitiveEntityType {
    PHONE, EMAIL, IDCARD, BANKCARD, API_KEY, TOKEN, PRIVATE_KEY, PASSWORD_VALUE,
    PASSWORD_KW, INTERNAL_URL, PATH, DEVICE_ID,
    KEYWORD,
    UNKNOWN
};

struct SensitiveSpan {
    SensitiveEntityType type = SensitiveEntityType::UNKNOWN;
    std::string rule_id;
    size_t start = 0;
    size_t end = 0;
    std::string matched;
    bool from_tool_output = false;
    std::string field_path;
};

enum class SensitivityLevel { S0, S1, S2 };

enum class ComplexityLevel { C0, C1, C2 };

enum class RouteTarget {
    LOCAL,
    ENTERPRISE_CLOUD,
    PUBLIC_CLOUD,
    POLICY_ERROR
};

struct InspectionResult {
    SensitivityLevel sensitivity_level = SensitivityLevel::S0;
    std::vector<std::string> hit_categories;
    std::vector<std::string> hit_rule_ids;
    std::string model_confidence;
    std::string summary_reason;
    bool rule_engine_failed = false;
    bool tool_output_escalation = false;
    bool oversized_field_warning = false;
    int oversized_fields_count = 0;
    std::vector<SensitiveSpan> spans;
    bool keywords_dict_reloaded = false;
    int keywords_dict_rules_count = 0;
};

struct DesensitizationResult {
    bool success = false;
    json desensitized_request;
    std::vector<std::string> applied_strategies;
    std::string failure_reason;
    std::unordered_map<std::string, std::string> mapping_table;
    bool use_format_preserving = false;
};

struct ComplexityResult {
    ComplexityLevel complexity_level = ComplexityLevel::C0;
    std::string reason;
};

struct RouteDecision {
    RouteTarget target = RouteTarget::LOCAL;
    SensitivityLevel sensitivity = SensitivityLevel::S0;
    ComplexityLevel complexity = ComplexityLevel::C0;
    std::string policy_id;
    bool need_desensitize = false;
    std::string decision_reason;
    bool enterprise_cloud_available = false;
    bool public_cloud_available = false;
    bool local_available = true;
    std::vector<std::string> fallbacks_applied;
    std::string error_code;
    int error_http_status = 0;
};

struct InspectionContext {
    bool is_internal_inspection = false;
    bool rule_engine_only = false;
    bool collect_spans = false;
    std::string request_id;
    std::string session_id;
    std::string global_session_id;
};

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

#endif
