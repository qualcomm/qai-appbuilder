//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_ROUTER_H
#define MODEL_ROUTER_H

#include "../security/content_security_types.h"
#include "../../model/model_config.h"

// ============================================================
// ModelRouter：路由决策器
// 根据敏感等级、复杂性等级、云端/本地可用性，决定路由目标
//
// 新路由矩阵（三级路由）：
//   S0 + C0       → LOCAL
//   S0 + C1       → ENTERPRISE_CLOUD（若可用），否则 LOCAL
//   S0 + C2       → PUBLIC_CLOUD（若可用），否则 ENTERPRISE_CLOUD，否则 LOCAL
//   S1 + C0       → LOCAL
//   S1 + C1       → 脱敏 → ENTERPRISE_CLOUD（若可用），否则 LOCAL
//   S1 + C2       → 脱敏 → PUBLIC_CLOUD（若可用），否则 ENTERPRISE_CLOUD，否则 LOCAL
//   S2 + 任意     → LOCAL（若本地不可用 → POLICY_ERROR，安全优先，绝不上任何云端）
// ============================================================
class ModelRouter
{
public:
    explicit ModelRouter(const RoutingConfig &routing_config,
                          const CloudModelConfig &cloud_config,
                          const EnterpriseCloudModelConfig &enterprise_cloud_config);

    // 路由决策主入口
    RouteDecision Route(const InspectionResult &inspection,
                         const ComplexityResult &complexity,
                         bool enterprise_cloud_available,
                         bool public_cloud_available,
                         bool local_available,
                         const std::string &agent_type = "main");

private:
    // 应用路由矩阵
    RouteDecision ApplyMatrix(SensitivityLevel s, ComplexityLevel c,
                               bool enterprise_cloud_available,
                               bool public_cloud_available,
                               bool local_available,
                               const std::string &agent_type = "main");

    RoutingConfig routing_config_;
    CloudModelConfig cloud_config_;                          // 公有云配置
    EnterpriseCloudModelConfig enterprise_cloud_config_;     // 企业内网云配置
};

#endif // MODEL_ROUTER_H
