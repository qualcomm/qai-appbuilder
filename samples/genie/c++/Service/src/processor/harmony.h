//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef HS_PROCESSOR_H
#define HS_PROCESSOR_H

#include "core/processor.h"

class HarmonyProcessor : public ModelProcessor
{
public:
    HarmonyProcessor();

    ~HarmonyProcessor() override;

    std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                   bool isToolResponse,
                                                   std::string &toolResponse) override;

    void Clean() final;

private:
    class Impl;

    Impl *impl_;

    std::string m_analysisText;
    std::string m_finalText;

    void processChunk(const std::string &chunk);

    enum class State
    {
        INIT,
        IN_CHANNEL,
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
    size_t outputtedLength = 0;     // 记录已输出的长度，用于计算新增内容
    std::string pendingBuffer;      // 用于暂存未完成标签的内容

    size_t findTag(const std::string &tag);

    void ResetState();
};

#endif // HS_PROCESSOR_H
    