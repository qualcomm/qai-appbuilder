//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CLOUD_MODEL_CLIENT_H
#define CLOUD_MODEL_CLIENT_H

#include "../security/content_security_types.h"
#include "../../model/model_config.h"
#include <functional>
#include <string>
#include <chrono>
#include <mutex>
#include <vector>
#include <unordered_map>

// ============================================================
// CloudModelClient：云端模型调用客户端
// 支持非 streaming 调用与 SSE streaming 调用
// 支持多端点 Fallback、熔断与冷却、429 策略、总尝试次数上限
// ============================================================
class CloudModelClient
{
public:
    explicit CloudModelClient(const CloudModelConfig &config);

    // 非 streaming 调用
    // request: 完整的 chat completion 请求 JSON
    // response: 云端返回的响应 JSON
    // error_msg: 错误信息（失败时填充）
    // 返回 true 表示成功
    bool ChatCompletion(const json &request,
                         json &response,
                         std::string &error_msg);

    // SSE streaming 调用
    // request: 完整的 chat completion 请求 JSON（stream 字段将被强制设为 true）
    // on_chunk: 每收到一个 SSE data 行时的回调，返回 false 表示中止传输
    //           chunk 为原始 SSE data 行内容（不含 "data: " 前缀）
    // error_msg: 错误信息（失败时填充）
    // 返回 true 表示成功完成（收到 [DONE] 标记）
    bool ChatCompletionStream(const json &request,
                               std::function<bool(const std::string &chunk)> on_chunk,
                               std::string &error_msg);

    // 检查云端是否可用（检查是否有任意端点不在冷却期）
    // 注意：声明为非 const，因为此方法会在可用性恢复时修改内部状态。
    bool IsAvailable();

    // 获取最后一次成功使用的端点名称（供审计日志使用）
    std::string GetLastUsedEndpoint() const;

private:
    // 端点熔断状态
    struct EndpointState {
        int consecutive_failures = 0;
        std::chrono::steady_clock::time_point cooldown_until{};
    };

    // 单次 HTTP 请求结果
    struct HttpResult {
        bool ok = false;
        int http_status = 0;
        std::string error_msg;
    };

    // 执行 SSE streaming HTTP 请求（含重试逻辑）
    bool DoStreamRequest(const std::string &url,
                          const json &request,
                          std::function<bool(const std::string &chunk)> on_chunk,
                          std::string &error_msg);

    // 构建可用端点顺序列表（跳过冷却期内的端点）
    std::vector<int> BuildEndpointOrder();

    // 计算有效最大总尝试次数
    int CalcMaxTotalAttempts() const;

    // 标记端点失败（增加连续失败计数）
    void MarkFailure(const std::string &endpoint_name);

    // 重置端点失败计数
    void ResetFailure(const std::string &endpoint_name);

    // 检查是否应触发熔断
    bool ShouldCircuitBreak(const std::string &endpoint_name) const;

    // 进入冷却期
    void EnterCooldown(const std::string &endpoint_name);

    // Backoff 等待
    void Backoff(int attempt = 1);

    // 执行单次 HTTP 请求（非 streaming）
    HttpResult DoSingleHttp(const CloudModelConfig::Endpoint &endpoint,
                             const json &request,
                             json &response,
                             std::string &err);

    // 执行单次 HTTP 请求（streaming）
    HttpResult DoSingleHttpStream(const CloudModelConfig::Endpoint &endpoint,
                                   const json &request,
                                   std::function<bool(const std::string &chunk)> on_chunk,
                                   std::string &err);

    // 带 Fallback 的请求执行（非 streaming）
    bool DoRequestWithFallback(const json &request,
                                json &response,
                                std::string &err);

    // 带 Fallback 的请求执行（streaming）
    bool DoStreamRequestWithFallback(const json &request,
                                      std::function<bool(const std::string &chunk)> on_chunk,
                                      std::string &err);

    // 解析 URL 为 host + path
    static bool ParseUrl(const std::string &url,
                         std::string &scheme,
                         std::string &host,
                         int &port,
                         std::string &path);

    // 构建云端请求 URL（确保以 /chat/completions 结尾）
    static std::string BuildEndpointUrl(const std::string &base_url);

    CloudModelConfig config_;

    // 多端点状态管理
    mutable std::mutex endpoints_mutex_;
    std::unordered_map<std::string, EndpointState> endpoint_states_;
    std::string last_used_endpoint_;
};

#endif // CLOUD_MODEL_CLIENT_H
