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

    std::string TakeReasoningDelta() override;

    std::string GetReasoningContent() const override { return m_internalAnalysisBuffer; }

    std::string GetFinalContent() const { return m_finalText; }

    bool IsToolCall() const { return m_isToolCall; }

    std::string GetToolCallContent() const { return m_toolCallContent; }

    void FinalizeToolCall();

    std::string FinalizeFinalChannel();

    static std::string BuildSystemMessage(
        const std::string& knowledge_cutoff,
        const std::string& current_date,
        const std::string& reasoning_level,
        bool has_tools
    );

    static std::string BuildDeveloperMessage(
        const std::string& instructions,
        const json& tools
    );

    static std::string BuildUserMessage(const std::string& content);

    static std::string BuildAssistantMessage(
        const std::string& channel,
        const std::string& content
    );

    static std::string BuildToolMessage(
        const std::string& tool_name,
        const std::string& content
    );

    static std::string ConvertToolsToTypeScript(const json& tools);

    std::string GetMessageForHistory() const;

private:
    class Impl;

    Impl *impl_;

    std::string m_finalText;
    std::string m_internalAnalysisBuffer;
    size_t m_reasoningSent = 0;
    std::string m_commentaryText;
    bool m_isToolCall = false;
    std::string m_toolCallContent;
    std::string m_toolCallFunctionName;

    std::vector<std::string> m_analysisMessages;
    std::vector<std::string> m_finalMessages;
    std::vector<std::string> m_commentaryMessages;

    void processChunk(const std::string &chunk);

    enum class State
    {
        INIT,
        IN_ROLE,
        IN_CHANNEL,
        IN_TO_PARAM,
        IN_CONSTRAIN,
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
    std::string buffer;
    std::string currentMessage;
    std::string currentChannelStr;
    std::string currentRole;
    std::string currentToParam;
    std::string currentConstrain;
    size_t outputtedLength = 0;
    std::string pendingBuffer;

    std::chrono::steady_clock::time_point lastChunkTime_;
    static constexpr int STREAM_TIMEOUT_SECONDS = 30;

    size_t findTag(const std::string &tag);

    void ResetState();

    std::string parseToParam(const std::string& header);

    static std::string ConvertJsonTypeToTS(const json& param_def);
};

#endif
