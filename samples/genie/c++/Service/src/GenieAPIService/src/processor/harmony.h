//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef HS_PROCESSOR_H
#define HS_PROCESSOR_H

#include "processor.h"
#include <nlohmann/json.hpp>
#include <chrono>

using json = nlohmann::ordered_json;

class HarmonyProcessor : public ModelProcessor
{
public:
    HarmonyProcessor();

    ~HarmonyProcessor() override;

    std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                   bool isToolResponse,
                                                   std::string &toolResponse) override;

    void Clean() final;
    
    // 新增：获取完整的消息用于历史存储
    std::string GetCompleteMessage() const { return m_completeMessage; }
    
    // 新增：获取仅 final 通道的内容
    std::string GetFinalContent() const { return m_finalText; }
    
    // 新增：获取 analysis 通道的内容（用于 CoT 管理）
    std::string GetAnalysisContent() const { return m_internalAnalysisBuffer; }
    
    // 新增：检查是否是工具调用
    bool IsToolCall() const { return m_isToolCall; }
    
    // 新增：获取工具调用内容（<tool_call> 格式）
    std::string GetToolCallContent() const { return m_toolCallContent; }
    
    // 新增：获取前言内容
    std::string GetCommentaryText() const { return m_commentaryText; }
    
    // 新增：强制完成工具调用处理（用于流结束时）
    void FinalizeToolCall();
    
    // 新增：强制 flush final 通道残留内容（用于 EOG/流结束时）
    // 当模型以 EOG token 结束生成而非 <|return|> 文本标记时，
    // 状态机停留在 IN_MESSAGE 状态，pendingBuffer 中可能有未输出的 final 内容。
    // 此方法强制将这些内容 flush 到 m_finalText，并返回新增的内容供调用方发送给客户端。
    // 返回值：新增的 final 内容（若无残留则返回空字符串）
    std::string FinalizeFinalChannel();
    
    // ========== Harmony 格式构建功能 ==========
    
    // 构建 system 消息
    static std::string BuildSystemMessage(
        const std::string& knowledge_cutoff,
        const std::string& current_date,
        const std::string& reasoning_level,
        bool has_tools
    );
    
    // 构建 developer 消息
    static std::string BuildDeveloperMessage(
        const std::string& instructions,
        const json& tools
    );
    
    // 构建 user 消息
    static std::string BuildUserMessage(const std::string& content);
    
    // 构建 assistant 消息（用于历史）
    // 移除 is_final 参数，历史消息统一使用 <|end|>
    // 根据 Harmony 规范（openai-harmony.md line 211-217）：
    // - <|return|> 和 <|call|> 是解码时的停止标记
    // - 历史存储时必须统一替换为 <|end|>
    // - 标准格式：<|start|>{header}<|message|>{content}<|end|>
    static std::string BuildAssistantMessage(
        const std::string& channel,
        const std::string& content
    );
    
    // 构建 tool 消息
    static std::string BuildToolMessage(
        const std::string& tool_name,
        const std::string& content
    );
    
    // 转换 OpenAI 工具定义为 Harmony TypeScript 格式
    static std::string ConvertToolsToTypeScript(const json& tools);
    
    // 判断是否应该保留 CoT（工具调用时保留）
    bool ShouldKeepCoT() const { return m_isToolCall; }
    
    // 获取用于历史存储的消息（根据规则处理 CoT）
    std::string GetMessageForHistory() const;

private:
    class Impl;

    Impl *impl_;

    std::string m_finalText;        // final 通道内容
    std::string m_completeMessage;  // 完整消息（用于历史）
    std::string m_internalAnalysisBuffer; // analysis 内容（不展示给用户）
    std::string m_commentaryText;   // commentary 内容
    bool m_isToolCall = false;      // 是否是工具调用
    std::string m_toolCallContent;  // 工具调用内容
    std::string m_toolCallFunctionName; // 工具调用函数名
    
    // CoT 管理：分别存储不同通道的消息
    std::vector<std::string> m_analysisMessages;   // analysis 通道消息列表
    std::vector<std::string> m_finalMessages;      // final 通道消息列表
    std::vector<std::string> m_commentaryMessages; // commentary 通道消息列表

    void processChunk(const std::string &chunk);

    enum class State
    {
        INIT,
        IN_ROLE,        // 新增：解析角色（包括 functions.xxx）
        IN_CHANNEL,
        IN_TO_PARAM,    // 新增：解析 to= 参数
        IN_CONSTRAIN,   // 新增：解析 <|constrain|> 标记
        IN_MESSAGE,
        UNKNOWN
    };

    enum class ChannelType
    {
        ANALYSIS,
        FINAL,
        COMMENTARY,
        FUNCTIONS,
        UNKNOWN
    };

    State currentState;
    ChannelType currentChannel{ChannelType::UNKNOWN};
    std::string buffer;             // 用于累积输入
    std::string currentMessage;     // 当前消息内容
    std::string currentChannelStr;  // 当前通道字符串
    std::string currentRole;        // 当前角色（可能是 functions.xxx）
    std::string currentToParam;     // to= 参数值
    std::string currentConstrain;   // constrain 类型（如 json）
    size_t outputtedLength = 0;     // 记录已输出的长度，用于计算新增内容
    std::string pendingBuffer;      // 用于暂存未完成标签的内容
    
    // 流式输出完整性检查
    std::chrono::steady_clock::time_point lastChunkTime_;  // 最后一次接收数据的时间
    static constexpr int STREAM_TIMEOUT_SECONDS = 30;      // 流式输出超时时间（秒）

    size_t findTag(const std::string &tag);

    void ResetState();
    
    // 新增：解析 to= 参数
    std::string parseToParam(const std::string& header);
    
    // 新增：辅助函数 - 转换 JSON 类型到 TypeScript 类型
    static std::string ConvertJsonTypeToTS(const json& param_def);
};

#endif // HS_PROCESSOR_H
