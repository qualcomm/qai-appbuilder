//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// gateway.cpp
//
// 职责（主文件）：
//   - 构造函数 / 析构函数
//   - SetLocalAvailabilityChecker
//
// 其余功能已拆分到以下文件：
//   gateway_routing.cpp              — HandleChatCompletion（主路由入口：Step1~Step6 编排、增量检查、S2 清洗、sticky 路由）
//   gateway_session.cpp              — Session ID 管理 + 限流计数器 + 脱敏映射表
//   gateway_history.cpp              — 历史清洗（本地历史 + S2 轮次清洗）
//   gateway_cloud.cpp                — 云端请求执行（流式 + 非流式 + 响应还原）
//   gateway_steps.cpp                — Step1~Step6 + DesensitizeForCloudOnly
//   gateway_incremental.cpp          — 增量检查（DetermineCheckScope 等）
//   gateway_overflow.cpp             — HandleLocalOutputOverflow + HandleLocalInputOverflow
//
//==============================================================================

#include "gateway.h"
#include "log.h"

// ============================================================
// 辅助函数：将 EnterpriseCloudModelConfig 转换为 CloudModelConfig
// 用于复用 CloudModelClient 实现，避免代码重复
// ============================================================
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

// ============================================================
// 构造函数
// ============================================================
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
    // 默认本地可用性检测：检查是否有任何已加载的模型可用。
    // 修复：在多模型场景下，model_config.get_genie_model_handle() 只返回最后加载的单模型句柄，
    // 不能代表所有模型的可用性。改为调用虚方法 IsLocalModelAvailable()，
    // ModelManager 重写此方法以检查 loaded_models_ 是否非空（多模型模式）。
    // 测试时可通过 SetLocalAvailabilityChecker 注入返回 false 的函数，
    // 以验证 S2+本地不可用（HTTP 403）等场景。
    //
    // 缓存本地可用性状态，避免每个请求在路由决策阶段持有全局 models_mutex_。
    // 在当前架构下，模型在服务启动时加载，运行期间不变，因此缓存是安全的。
    // 若未来支持运行时动态加载/卸载模型，需改用原子变量或其他无锁机制。
    bool local_available_cached = model_config.IsLocalModelAvailable();
    local_availability_checker_ = [local_available_cached]() -> bool {
        return local_available_cached;  // 无锁读取缓存值
    };

    My_Log{} << "[GenieRoutingGateway] Initialized, routing.enabled=" << routing_config.enabled
             << ", local_model.enabled=" << local_model_config_.enabled
             << ", cloud_model.enabled=" << cloud_config.enabled
             << ", use_local_model_fallback(sensitivity)="
             << routing_config.sensitivity_detection.use_local_model_fallback << std::endl;
}

void GenieRoutingGateway::SetLocalAvailabilityChecker(std::function<bool()> checker)
{
    local_availability_checker_ = std::move(checker);
}

// ============================================================
// 析构函数
// ============================================================
GenieRoutingGateway::~GenieRoutingGateway()
{
    // 通知所有已 detach 的后台线程：GenieRoutingGateway 即将析构，不得再访问成员变量。
    // 后台线程持有 alive_ 的 shared_ptr 副本，检测到 false 后会提前退出。
    alive_->store(false);
    My_Log{} << "[GenieRoutingGateway] Destructor: alive flag set to false, detached threads will exit safely" << std::endl;
}
