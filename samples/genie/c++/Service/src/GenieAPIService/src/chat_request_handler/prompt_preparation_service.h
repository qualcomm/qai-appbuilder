//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// prompt_preparation_service.h
//
// 职责：
//   统一提示词预处理服务（Prompt Preparation Pipeline）
//
//   将原始 OpenAI chat/completions 请求中的 messages 进行优化，
//   输出适合云端模型（企业云/公网云）的标准 OpenAI API 格式 messages 数组。
//
// 与 ModelInputBuilder::BuildPrompt() 的区别：
//   - BuildPrompt()：输出单字符串文本 prompt（用于本地推理引擎，含 <|im_start|> 等特殊 token）
//   - PrepareForCloud()：输出优化后的 messages 数组（用于 OpenAI API，保留完整 JSON 格式）
//
// 核心设计原则：
//   1. 只优化 system 消息内容（重建 Skill Catalog + 工具规则）
//   2. 其余消息（user/assistant/tool）完整保留原始 OpenAI JSON 格式
//   3. assistant 的 tool_calls 字段、tool 的 tool_call_id 字段均不丢失
//   4. 不使用 GenieChatMessage 向量（该结构仅适用于本地模型文本 prompt）
//
//==============================================================================

#ifndef PROMPT_PREPARATION_SERVICE_H
#define PROMPT_PREPARATION_SERVICE_H

#include "../model/model_config.h"
#include "prompt_optimizer.h"
#include <nlohmann/json.hpp>
#include <string>

using json = nlohmann::ordered_json;

// ============================================================
// 提示词预处理结果
// ============================================================
struct PreparedPromptResult {
    bool success = false;               // 是否成功
    json prepared_request;              // 优化后的完整请求（含 messages + tools）
    std::string optimized_system_prompt; // 优化后的 system prompt（用于日志）
    int used_context_size = 0;          // 实际使用的 context_size（用于日志）
    bool prompt_optimized = false;      // system prompt 是否被优化
    bool messages_trimmed = false;      // 消息历史是否被裁剪
    bool tools_filtered = false;        // 工具定义是否被过滤
    std::string failure_reason;         // 失败原因（success=false 时有效）
    // agent 类型（"main" 或 "sub"），用于日志标签
    // 优化后的 system prompt 不含 "agent=main" 标记，需通过此字段传递
    std::string agent_type = "sub";
};

// ============================================================
// PromptPreparationService
//
// 无状态服务，每次调用独立处理，线程安全。
// 依赖 IModelConfig 读取优化配置（system_context、prompt_sections 等）。
// ============================================================
class PromptPreparationService
{
public:
    // 构造函数
    // model_config：全局模型配置（用于读取 prompt_optimization 配置）
    explicit PromptPreparationService(IModelConfig &model_config);

    // ============================================================
    // PrepareForCloud：对云端请求执行统一提示词优化
    //
    // 输入：
    //   request      - 原始 OpenAI 请求（含 messages + tools）
    //   context_size - 目标模型的上下文窗口大小（tokens）
    //                  0 = 使用默认值（由调用方传入对应云端配置的默认值）
    //
    // 输出：PreparedPromptResult
    //   - prepared_request：优化后的请求，可直接发送给云端 API
    //   - success=false：优化失败，调用方应使用原始 request
    //
    // 优化步骤：
    //   1. 提取 system/developer 消息内容
    //   2. 检测 agent 类型（main/sub）
    //   3. 重建 system prompt（BuildSystemContext）
    //   4. 提取非 system 消息（保留完整 OpenAI JSON 格式）
    //   5. 按 context_size 裁剪消息历史（保持 OpenAI 格式）
    //   6. 重组 messages 数组（优化后的 system + 原始格式的其余消息）
    // ============================================================
    PreparedPromptResult PrepareForCloud(const json &request, int context_size);

private:
    IModelConfig &model_config_;
    PromptOptimizer optimizer_;

    // 从 messages 中提取 system/developer 消息内容
    // 返回合并后的 system prompt 字符串
    static std::string ExtractSystemPrompt(const json &messages);

    // 从 messages 中提取非 system/developer 消息
    // 保留完整 OpenAI JSON 格式（含 tool_calls、tool_call_id 等字段）
    static json ExtractNonSystemMessages(const json &messages);

    // 将优化后的 system prompt 与原始非 system 消息重组为 OpenAI messages 数组
    // 非 system 消息完整保留原始 JSON 格式，不做任何格式转换
    static json RebuildMessagesForCloud(const std::string &optimized_system_prompt,
                                         const json &non_system_messages);

    // 将本地模型专用格式转换为 OpenAI API 标准格式
    //
    // BuildSystemContext() 生成的 system prompt 包含本地模型（Qwen3）专用的
    // <tool_call> 格式示例，这对云端模型（OpenAI API function calling）是错误的。
    // 此函数将这些本地格式替换为云端模型能理解的自然语言描述。
    //
    // 转换内容：
    //   - <tool_call>{"name":"read","arguments":{"path":"..."}} → call read("...")
    //   - 移除 system prompt 中所有 <tool_call>...</tool_call> 块
    static std::string AdaptSystemPromptForCloud(const std::string &system_prompt);
};

#endif // PROMPT_PREPARATION_SERVICE_H
