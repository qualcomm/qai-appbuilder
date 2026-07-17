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

// ============================================================
// Desensitizer：内容脱敏处理器
// 基础策略：
//   - mask：手机号 138****1234，邮箱 a***@xx.com
//   - placeholder：API Key → <API_KEY>，Token → <TOKEN>
//   - delete：私钥块整体删除
// 结构化占位符策略：
//   - structured_placeholder：基于 SensitiveSpan 的结构化占位符映射（<PHONE_1> 等）
//   - summarize：调用本地模型对长段落进行脱敏重写（带超时与降级控制）
// ============================================================
class Desensitizer
{
public:
    explicit Desensitizer(const RoutingConfig::DesensitizationConfig &config,
                          IModelConfig *model_config = nullptr);

    // 主脱敏入口
    // original_request: 完整的原始请求 JSON（含 messages、tools 等）
    // inspection: 敏感检测结果（用于确定需要处理的类别，以及 spans）
    // existing_mapping: [迭代脱敏] 前几轮已累积的映射表（mock→real），用于初始化计数器，
    //                   避免新轮次分配与已有 mock 值冲突的占位符
    DesensitizationResult Apply(const json &original_request,
                                 const InspectionResult &inspection,
                                 const std::unordered_map<std::string, std::string> *existing_mapping = nullptr);

private:
    // 手机号脱敏：138****1234
    std::string MaskPhone(const std::string &text);

    // 邮箱脱敏：a***@xx.com
    std::string MaskEmail(const std::string &text);

    // 银行卡号脱敏：**** **** **** 1234
    std::string MaskBankCard(const std::string &text);

    // 身份证脱敏：110***********1234
    std::string MaskIdCard(const std::string &text);

    // API Key 替换为占位符
    std::string MaskApiKey(const std::string &text);

    // Bearer Token 替换为占位符
    std::string MaskToken(const std::string &text);

    // 删除私钥块
    std::string DeletePrivateKey(const std::string &text);

    // 密码关键词脱敏
    std::string MaskPassword(const std::string &text);

    // 内网 IP/域名脱敏：RFC 1918 私有 IP → <INTERNAL_IP>，内网域名 → <INTERNAL_DOMAIN>
    std::string MaskInternalUrl(const std::string &text);

    // 本地路径脱敏：Windows/Unix 用户目录路径 → <LOCAL_PATH>
    std::string MaskLocalPath(const std::string &text);

    // 设备标识脱敏：MAC 地址 → <MAC_ADDR>，IMEI → <IMEI>
    std::string MaskDeviceId(const std::string &text);

    // 图片数据脱敏：data URI 格式的内嵌图片 → <IMAGE_DATA_REMOVED>
    std::string MaskImageData(const std::string &text);

    // 处理单个文本字符串（应用所有脱敏策略，兼容模式）
    std::string ProcessText(const std::string &text);

    // 处理 messages 数组（兼容模式）
    json ProcessMessages(const json &messages);

    // 处理单个 message 的 content（兼容模式）
    json ProcessMessageContent(const json &content);

    // 格式化结构化占位符（如 <PHONE_1>）
    // 根据 config_.placeholder_style 模板生成占位符
    std::string FormatPlaceholder(SensitiveEntityType t, int index) const;

    // 基于 spans 的结构化占位符替换
    // 返回 true 表示成功，out 填充脱敏结果
    // existing_mapping: [迭代脱敏] 前几轮已累积的映射表，用于初始化计数器偏移
    bool ApplyStructuredPlaceholders(const json &original_request,
                                      const InspectionResult &inspection,
                                      DesensitizationResult &out,
                                      const std::unordered_map<std::string, std::string> *existing_mapping = nullptr);

    // 摘要化脱敏（调用本地模型对长段落进行脱敏重写）
    // 返回脱敏后的文本，失败时返回空字符串（调用方降级处理）
    std::string SummarizeText(const std::string &text, int timeout_ms);

    // 格式保留脱敏：生成符合语义格式的 Mock Data
    // 替代 FormatPlaceholder，用于工具调用场景
    // matched: 原始匹配值（用于 PATH 类型只替换用户名部分，保留路径结构）
    std::string FormatMockData(SensitiveEntityType t, int index, const std::string &matched = "") const;

    // 生成指定长度的随机字母数字字符串（供 API_KEY/TOKEN mock 数据复用）
    std::string GenerateRandomAlnum(int len) const;
    
    // 检查是否应使用格式保留脱敏
    // 当请求包含 tools 字段且配置启用时返回 true
    bool ShouldUseFormatPreserving(const json &request) const;

    RoutingConfig::DesensitizationConfig config_;
    IModelConfig *model_config_;  // 可为 nullptr（未提供本地模型时，summarize 策略不可用）
};

#endif // DESENSITIZER_H
