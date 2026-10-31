//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"

static CloudModelConfig ToCloudModelConfig(const EnterpriseCloudModelConfig &ec)
{
    CloudModelConfig cc;
    cc.enabled = ec.enabled;
    cc.base_url = ec.base_url;
    cc.api_key = ec.api_key;
    cc.model = ec.model;
    cc.timeout_seconds = ec.timeout_seconds;
    cc.stream_timeout_seconds = ec.stream_timeout_seconds;
    cc.retry.max = ec.retry.max;
    cc.retry.backoff_ms = ec.retry.backoff_ms;
    cc.retry.max_total_attempts = ec.retry.max_total_attempts;
    cc.retry.retry_on_429_switch_endpoint = ec.retry.retry_on_429_switch_endpoint;
    cc.circuit_breaker.failure_threshold = ec.circuit_breaker.failure_threshold;
    cc.circuit_breaker.cooldown_seconds = ec.circuit_breaker.cooldown_seconds;
    cc.rate_limit.max_inferences_per_task = ec.rate_limit.max_inferences_per_task;
    cc.rate_limit.max_tokens_per_task = ec.rate_limit.max_tokens_per_task;
    for (const auto &ep : ec.endpoints)
    {
        CloudModelConfig::Endpoint cep;
        cep.name = ep.name;
        cep.base_url = ep.base_url;
        cep.model = ep.model;
        cc.endpoints.push_back(cep);
    }
    cc.log_debug = ec.log_debug;
    return cc;
}

GenieRoutingGateway::GenieRoutingGateway(IModelConfig &model_config,
                        const RoutingConfig &routing_config,
                        const CloudModelConfig &cloud_config,
                        const EnterpriseCloudModelConfig &enterprise_cloud_config)
        : model_config_(model_config),
          inspector_(routing_config.sensitivity_detection,
                     routing_config.cache,
                     routing_config.policy_id,
                     routing_config.sensitivity_detection.use_local_model_fallback
                         ? &model_config : nullptr),
          desensitizer_(routing_config.desensitization, &model_config),
          complexity_evaluator_(routing_config.complexity, &model_config),
          router_(routing_config, cloud_config, enterprise_cloud_config),
          cloud_client_(cloud_config),
          enterprise_cloud_client_(ToCloudModelConfig(enterprise_cloud_config)),
          audit_logger_(routing_config.metrics),
          routing_config_(routing_config),
          cloud_config_(cloud_config),
          enterprise_cloud_config_(enterprise_cloud_config),
          local_model_config_(model_config.GetLocalModelConfig()),
          alive_(std::make_shared<std::atomic<bool>>(true)),
          prompt_prep_service_(model_config)
{
    My_Log{} << "[GenieRoutingGateway] Initialized, routing.enabled=" << routing_config.enabled
             << ", local_model.enabled=" << local_model_config_.enabled
             << ", cloud_model.enabled=" << cloud_config.enabled
             << ", use_local_model_fallback(sensitivity)="
             << routing_config.sensitivity_detection.use_local_model_fallback << std::endl;
}

GenieRoutingGateway::~GenieRoutingGateway()
{
    alive_->store(false);
    My_Log{} << "[GenieRoutingGateway] Destructor: alive flag set to false, detached threads will exit safely" << std::endl;
}
