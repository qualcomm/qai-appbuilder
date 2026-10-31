//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef DESENSITIZER_H
#define DESENSITIZER_H

#include "content_security_types.h"
#include "../../model/model_config.h"
#include <regex>
#include <string>
#include <unordered_map>
#include <functional>

class Desensitizer
{
public:
    explicit Desensitizer(const RoutingConfig::DesensitizationConfig &config,
                          IModelConfig *model_config = nullptr);

    DesensitizationResult Apply(const json &original_request,
                                 const InspectionResult &inspection,
                                 const std::unordered_map<std::string, std::string> *existing_mapping = nullptr);

private:
    std::string MaskPhone(const std::string &text);

    std::string MaskEmail(const std::string &text);

    std::string MaskBankCard(const std::string &text);

    std::string MaskIdCard(const std::string &text);

    std::string MaskApiKey(const std::string &text);

    std::string MaskToken(const std::string &text);

    std::string DeletePrivateKey(const std::string &text);

    std::string MaskPassword(const std::string &text);

    std::string MaskInternalUrl(const std::string &text);

    std::string MaskLocalPath(const std::string &text);

    std::string MaskDeviceId(const std::string &text);

    std::string MaskImageData(const std::string &text);

    std::string ProcessText(const std::string &text);

    json ProcessMessages(const json &messages);

    json ProcessMessageContent(const json &content);

    std::string FormatPlaceholder(SensitiveEntityType t, int index) const;

    bool ApplyStructuredPlaceholders(const json &original_request,
                                      const InspectionResult &inspection,
                                      DesensitizationResult &out,
                                      const std::unordered_map<std::string, std::string> *existing_mapping = nullptr);

    std::string SummarizeText(const std::string &text, int timeout_ms);

    std::string FormatMockData(SensitiveEntityType t, int index, const std::string &matched = "") const;

    std::string GenerateRandomAlnum(int len) const;

    bool ShouldUseFormatPreserving(const json &request) const;

    RoutingConfig::DesensitizationConfig config_;
    IModelConfig *model_config_;
};

#endif
