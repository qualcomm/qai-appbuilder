//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "model_router.h"
#include "log.h"
#include <sstream>

ModelRouter::ModelRouter(const RoutingConfig &routing_config,
                          const CloudModelConfig &cloud_config,
                          const EnterpriseCloudModelConfig &enterprise_cloud_config)
        : routing_config_(routing_config),
          cloud_config_(cloud_config),
          enterprise_cloud_config_(enterprise_cloud_config)
{
}

RouteDecision ModelRouter::ApplyMatrix(
        SensitivityLevel s, ComplexityLevel c,
        bool enterprise_cloud_available,
        bool public_cloud_available,
        bool local_available,
        const std::string &agent_type)
{
    RouteDecision decision;
    decision.sensitivity = s;
    decision.complexity = c;
    decision.policy_id = routing_config_.policy_id;
    decision.enterprise_cloud_available = enterprise_cloud_available;
    decision.public_cloud_available = public_cloud_available;
    decision.local_available = local_available;
    decision.need_desensitize = false;

    if (s == SensitivityLevel::S2)
    {
        if (local_available)
        {
            decision.target = RouteTarget::LOCAL;
            decision.decision_reason = "S2: high sensitivity, forced local processing";
        }
        else
        {
            decision.target = RouteTarget::POLICY_ERROR;
            decision.error_code = "sensitive_content_local_unavailable";
            decision.error_http_status = 403;
            decision.decision_reason = "S2: high sensitivity, local unavailable, cannot process safely";
        }
        return decision;
    }

    if (s == SensitivityLevel::S1)
    {
        if (c == ComplexityLevel::C0)
        {

            if (local_available)
            {
                decision.target = RouteTarget::LOCAL;
                decision.decision_reason = "S1+C0: medium sensitivity simple task, prefer local";
            }
            else
            {

                if (routing_config_.fallback.local_unavailable_s1 == "cloud_if_allowed"
                    && enterprise_cloud_available)
                {
                    decision.target = RouteTarget::ENTERPRISE_CLOUD;
                    decision.need_desensitize = true;
                    decision.fallbacks_applied.push_back("local_unavailable_s1_to_enterprise_cloud");
                    decision.decision_reason = "S1+C0: local unavailable, fallback to enterprise cloud (desensitized)";
                }
                else
                {
                    decision.target = RouteTarget::POLICY_ERROR;
                    decision.error_code = "all_routes_unavailable";
                    decision.error_http_status = 503;
                    decision.decision_reason = "S1+C0: local unavailable, enterprise cloud unavailable or not allowed";
                }
            }
        }
        else if (c == ComplexityLevel::C1)
        {

            decision.need_desensitize = routing_config_.enterprise_cloud_require_desensitize;
            if (enterprise_cloud_available)
            {
                decision.target = RouteTarget::ENTERPRISE_CLOUD;
                decision.decision_reason = std::string("S1+C1: medium sensitivity moderate task, ")
                    + (decision.need_desensitize ? "desensitize then " : "")
                    + "enterprise cloud";
            }
            else
            {

                const std::string &ec_policy = routing_config_.fallback.enterprise_cloud_unavailable;
                if (ec_policy == "public_cloud_if_allowed" && public_cloud_available)
                {

                    decision.target = RouteTarget::PUBLIC_CLOUD;
                    decision.need_desensitize = true;
                    decision.fallbacks_applied.push_back("enterprise_cloud_unavailable_to_public_cloud");
                    decision.decision_reason = "S1+C1: enterprise cloud unavailable, fallback to public cloud (desensitized)";
                }
                else if (local_available &&
                         (ec_policy == "local_if_allowed" ||
                          (ec_policy != "fail" && routing_config_.fallback.cloud_unavailable_to_local)))
                {

                    decision.target = RouteTarget::LOCAL;
                    decision.need_desensitize = false;
                    decision.fallbacks_applied.push_back("enterprise_cloud_unavailable_to_local");
                    decision.decision_reason = "S1+C1: enterprise cloud unavailable, fallback to local";
                }
                else if (local_available && ec_policy == "fail")
                {

                    decision.target = RouteTarget::POLICY_ERROR;
                    decision.error_code = "cloud_unavailable_no_fallback";
                    decision.error_http_status = 503;
                    decision.decision_reason = "S1+C1: enterprise cloud unavailable, fallback disabled by policy=fail";
                }
                else
                {
                    decision.target = RouteTarget::POLICY_ERROR;
                    decision.error_code = "all_routes_unavailable";
                    decision.error_http_status = 503;
                    decision.decision_reason = "S1+C1: enterprise cloud unavailable, fallback policy=" + ec_policy;
                }
            }
        }
        else
        {

            decision.need_desensitize = true;
            if (public_cloud_available)
            {
                decision.target = RouteTarget::PUBLIC_CLOUD;
                decision.decision_reason = "S1+C2: medium sensitivity complex task, desensitize then public cloud";
            }
            else
            {

                const std::string &pc_policy = routing_config_.fallback.public_cloud_unavailable;
                if (pc_policy == "enterprise_cloud_if_allowed" && enterprise_cloud_available)
                {
                    decision.target = RouteTarget::ENTERPRISE_CLOUD;
                    decision.fallbacks_applied.push_back("public_cloud_unavailable_to_enterprise_cloud");
                    decision.decision_reason = "S1+C2: public cloud unavailable, fallback to enterprise cloud (desensitized)";
                }
                else if (local_available &&
                         (pc_policy == "local_if_allowed" ||
                          (pc_policy != "fail" && routing_config_.fallback.cloud_unavailable_to_local)))
                {
                    decision.target = RouteTarget::LOCAL;
                    decision.need_desensitize = false;
                    decision.fallbacks_applied.push_back("all_clouds_unavailable_to_local");
                    decision.decision_reason = "S1+C2: all clouds unavailable, fallback to local";
                }
                else if (local_available && pc_policy == "fail")
                {

                    decision.target = RouteTarget::POLICY_ERROR;
                    decision.error_code = "cloud_unavailable_no_fallback";
                    decision.error_http_status = 503;
                    decision.decision_reason = "S1+C2: public cloud unavailable, fallback disabled by policy=fail";
                }
                else
                {
                    decision.target = RouteTarget::POLICY_ERROR;
                    decision.error_code = "all_routes_unavailable";
                    decision.error_http_status = 503;
                    decision.decision_reason = "S1+C2: all routes unavailable, fallback policy=" + pc_policy;
                }
            }
        }
        return decision;
    }

    if (c == ComplexityLevel::C0)
    {

        if (local_available)
        {
            decision.target = RouteTarget::LOCAL;
            decision.decision_reason = "S0+C0: no sensitivity simple task, prefer local";
        }
        else if (enterprise_cloud_available)
        {
            decision.target = RouteTarget::ENTERPRISE_CLOUD;
            decision.fallbacks_applied.push_back("local_unavailable_s0_to_enterprise_cloud");
            decision.decision_reason = "S0+C0: local unavailable, fallback to enterprise cloud";
        }
        else if (public_cloud_available)
        {
            decision.target = RouteTarget::PUBLIC_CLOUD;
            decision.fallbacks_applied.push_back("local_unavailable_s0_to_public_cloud");
            decision.decision_reason = "S0+C0: local and enterprise cloud unavailable, fallback to public cloud";
        }
        else
        {
            decision.target = RouteTarget::POLICY_ERROR;
            decision.error_code = "all_routes_unavailable";
            decision.error_http_status = 503;
            decision.decision_reason = "S0+C0: all routes unavailable";
        }
    }
    else if (c == ComplexityLevel::C1)
    {

        if (enterprise_cloud_available)
        {
            decision.target = RouteTarget::ENTERPRISE_CLOUD;
            decision.decision_reason = "S0+C1: no sensitivity moderate task, prefer enterprise cloud";
        }
        else
        {

            const std::string &ec_policy = routing_config_.fallback.enterprise_cloud_unavailable;
            if (ec_policy == "public_cloud_if_allowed" && public_cloud_available)
            {

                decision.target = RouteTarget::PUBLIC_CLOUD;
                decision.fallbacks_applied.push_back("enterprise_cloud_unavailable_to_public_cloud");
                decision.decision_reason = "S0+C1: enterprise cloud unavailable, fallback to public cloud";
            }
            else if (local_available &&
                     (ec_policy == "local_if_allowed" ||
                      (ec_policy != "fail" && routing_config_.fallback.cloud_unavailable_to_local)))
            {
                decision.target = RouteTarget::LOCAL;
                decision.fallbacks_applied.push_back("enterprise_cloud_unavailable_to_local");
                decision.decision_reason = "S0+C1: enterprise cloud unavailable, fallback to local";
            }
            else if (local_available && ec_policy == "fail")
            {

                decision.target = RouteTarget::POLICY_ERROR;
                decision.error_code = "cloud_unavailable_no_fallback";
                decision.error_http_status = 503;
                decision.decision_reason = "S0+C1: enterprise cloud unavailable, fallback disabled by policy=fail";
            }
            else
            {
                decision.target = RouteTarget::POLICY_ERROR;
                decision.error_code = "all_routes_unavailable";
                decision.error_http_status = 503;
                decision.decision_reason = "S0+C1: enterprise cloud unavailable, fallback policy=" + ec_policy;
            }
        }
    }
    else
    {

        if (public_cloud_available)
        {
            decision.target = RouteTarget::PUBLIC_CLOUD;
            decision.decision_reason = "S0+C2: no sensitivity complex task, prefer public cloud";
        }
        else
        {

            const std::string &pc_policy = routing_config_.fallback.public_cloud_unavailable;
            if (pc_policy == "enterprise_cloud_if_allowed" && enterprise_cloud_available)
            {
                decision.target = RouteTarget::ENTERPRISE_CLOUD;
                decision.fallbacks_applied.push_back("public_cloud_unavailable_to_enterprise_cloud");
                decision.decision_reason = "S0+C2: public cloud unavailable, fallback to enterprise cloud";
            }
            else if (local_available &&
                     (pc_policy == "local_if_allowed" ||
                      (pc_policy != "fail" && routing_config_.fallback.cloud_unavailable_to_local)))
            {
                decision.target = RouteTarget::LOCAL;
                decision.fallbacks_applied.push_back("all_clouds_unavailable_to_local");
                decision.decision_reason = "S0+C2: all clouds unavailable, fallback to local";
            }
            else if (local_available && pc_policy == "fail")
            {
                decision.target = RouteTarget::POLICY_ERROR;
                decision.error_code = "cloud_unavailable_no_fallback";
                decision.error_http_status = 503;
                decision.decision_reason = "S0+C2: public cloud unavailable, fallback disabled by policy=fail";
            }
            else
            {
                decision.target = RouteTarget::POLICY_ERROR;
                decision.error_code = "all_routes_unavailable";
                decision.error_http_status = 503;
                decision.decision_reason = "S0+C2: all routes unavailable, fallback policy=" + pc_policy;
            }
        }
    }

    if (routing_config_.prefer_local_for_simple &&
        (decision.target == RouteTarget::ENTERPRISE_CLOUD || decision.target == RouteTarget::PUBLIC_CLOUD) &&
        c == ComplexityLevel::C0 &&
        local_available)
    {
        decision.target = RouteTarget::LOCAL;
        decision.fallbacks_applied.push_back("prefer_local_for_simple");
        decision.decision_reason += " (overridden by prefer_local_for_simple)";
    }

    return decision;
}

RouteDecision ModelRouter::Route(
        const InspectionResult &inspection,
        const ComplexityResult &complexity,
        bool enterprise_cloud_available,
        bool public_cloud_available,
        bool local_available,
        const std::string &agent_type)
{
    RouteDecision decision;

    if (agent_type == "sub") {

        bool allow_enterprise_cloud = routing_config_.agent_routing.sub_agent_allow_cloud_on_c2 &&
                                      complexity.complexity_level == ComplexityLevel::C1 &&
                                      enterprise_cloud_available &&
                                      inspection.sensitivity_level != SensitivityLevel::S2;

        bool allow_public_cloud = routing_config_.agent_routing.sub_agent_allow_cloud_on_c2 &&
                                  complexity.complexity_level == ComplexityLevel::C2 &&
                                  public_cloud_available &&
                                  inspection.sensitivity_level != SensitivityLevel::S2;

        if (allow_enterprise_cloud || allow_public_cloud) {

            decision = ApplyMatrix(
                inspection.sensitivity_level,
                complexity.complexity_level,
                enterprise_cloud_available,
                public_cloud_available,
                local_available,
                "main"
            );
            decision.decision_reason = "[sub_agent_cloud] " + decision.decision_reason;
        } else {

            decision.sensitivity = inspection.sensitivity_level;
            decision.complexity = complexity.complexity_level;
            decision.policy_id = routing_config_.policy_id;
            decision.enterprise_cloud_available = enterprise_cloud_available;
            decision.public_cloud_available = public_cloud_available;
            decision.local_available = local_available;
            decision.need_desensitize = false;

            if (inspection.sensitivity_level == SensitivityLevel::S2) {

                if (local_available) {
                    decision.target = RouteTarget::LOCAL;
                    decision.decision_reason = "sub_agent: S2 forced local";
                } else {
                    decision.target = RouteTarget::POLICY_ERROR;
                    decision.error_code = "sensitive_content_local_unavailable";
                    decision.error_http_status = 403;
                    decision.decision_reason = "sub_agent: S2 local unavailable";
                }
            } else {

                if (local_available) {
                    decision.target = RouteTarget::LOCAL;
                    decision.decision_reason = "sub_agent: prefer local (S0/S1 or C0/C1)";
                } else {

                    if (routing_config_.fallback.local_unavailable_s0 == "cloud_if_allowed" &&
                        enterprise_cloud_available &&
                        inspection.sensitivity_level == SensitivityLevel::S0) {
                        decision.target = RouteTarget::ENTERPRISE_CLOUD;
                        decision.fallbacks_applied.push_back("sub_agent_local_unavailable_s0_to_enterprise_cloud");
                        decision.decision_reason = "sub_agent: local unavailable, fallback to enterprise cloud (S0)";
                    } else if (routing_config_.fallback.local_unavailable_s1 == "cloud_if_allowed" &&
                               enterprise_cloud_available &&
                               inspection.sensitivity_level == SensitivityLevel::S1) {
                        decision.target = RouteTarget::ENTERPRISE_CLOUD;
                        decision.need_desensitize = true;
                        decision.fallbacks_applied.push_back("sub_agent_local_unavailable_s1_to_enterprise_cloud");
                        decision.decision_reason = "sub_agent: local unavailable, fallback to enterprise cloud (S1, desensitized)";
                    } else {
                        decision.target = RouteTarget::POLICY_ERROR;
                        decision.error_code = "all_routes_unavailable";
                        decision.error_http_status = 503;
                        decision.decision_reason = "sub_agent: local unavailable, no fallback";
                    }
                }
            }
        }
    } else {

        decision = ApplyMatrix(
            inspection.sensitivity_level,
            complexity.complexity_level,
            enterprise_cloud_available,
            public_cloud_available,
            local_available,
            agent_type
        );
    }

    My_Log{} << "[ModelRouter] Route decision: "
             << "agent_type=" << agent_type
             << ", target=" << to_string(decision.target)
             << ", sensitivity=" << to_string(decision.sensitivity)
             << ", complexity=" << to_string(decision.complexity)
             << ", need_desensitize=" << decision.need_desensitize
             << ", enterprise_cloud_available=" << enterprise_cloud_available
             << ", public_cloud_available=" << public_cloud_available
             << ", reason=" << decision.decision_reason
             << std::endl;

    return decision;
}
