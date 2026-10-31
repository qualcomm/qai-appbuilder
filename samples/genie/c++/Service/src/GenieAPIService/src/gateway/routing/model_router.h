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

class ModelRouter
{
public:
    explicit ModelRouter(const RoutingConfig &routing_config,
                          const CloudModelConfig &cloud_config,
                          const EnterpriseCloudModelConfig &enterprise_cloud_config);

    RouteDecision Route(const InspectionResult &inspection,
                         const ComplexityResult &complexity,
                         bool enterprise_cloud_available,
                         bool public_cloud_available,
                         bool local_available,
                         const std::string &agent_type = "main");

private:
    RouteDecision ApplyMatrix(SensitivityLevel s, ComplexityLevel c,
                               bool enterprise_cloud_available,
                               bool public_cloud_available,
                               bool local_available,
                               const std::string &agent_type = "main");

    RoutingConfig routing_config_;
    CloudModelConfig cloud_config_;
    EnterpriseCloudModelConfig enterprise_cloud_config_;
};

#endif
