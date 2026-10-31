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

class CloudModelClient
{
public:
    explicit CloudModelClient(const CloudModelConfig &config);

    bool ChatCompletion(const json &request,
                         json &response,
                         std::string &error_msg);

    bool ChatCompletionStream(const json &request,
                               std::function<bool(const std::string &chunk)> on_chunk,
                               std::string &error_msg);

    bool IsAvailable();

    std::string GetLastUsedEndpoint() const;

private:
    struct EndpointState {
        int consecutive_failures = 0;
        std::chrono::steady_clock::time_point cooldown_until{};
    };

    struct HttpResult {
        bool ok = false;
        int http_status = 0;
        std::string error_msg;
    };

    bool DoStreamRequest(const std::string &url,
                          const json &request,
                          std::function<bool(const std::string &chunk)> on_chunk,
                          std::string &error_msg);

    std::vector<int> BuildEndpointOrder();

    int CalcMaxTotalAttempts() const;

    void MarkFailure(const std::string &endpoint_name);

    void ResetFailure(const std::string &endpoint_name);

    bool ShouldCircuitBreak(const std::string &endpoint_name) const;

    void EnterCooldown(const std::string &endpoint_name);

    void Backoff(int attempt = 1);

    HttpResult DoSingleHttp(const CloudModelConfig::Endpoint &endpoint,
                             const json &request,
                             json &response,
                             std::string &err);

    HttpResult DoSingleHttpStream(const CloudModelConfig::Endpoint &endpoint,
                                   const json &request,
                                   std::function<bool(const std::string &chunk)> on_chunk,
                                   std::string &err);

    bool DoRequestWithFallback(const json &request,
                                json &response,
                                std::string &err);

    bool DoStreamRequestWithFallback(const json &request,
                                      std::function<bool(const std::string &chunk)> on_chunk,
                                      std::string &err);

    static bool ParseUrl(const std::string &url,
                         std::string &scheme,
                         std::string &host,
                         int &port,
                         std::string &path);

    static std::string BuildEndpointUrl(const std::string &base_url);

    CloudModelConfig config_;

    mutable std::mutex endpoints_mutex_;
    std::unordered_map<std::string, EndpointState> endpoint_states_;
    std::string last_used_endpoint_;
};

#endif
