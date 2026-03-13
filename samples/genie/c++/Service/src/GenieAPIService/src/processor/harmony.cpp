//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "harmony.h"
#include "log.h"

/* @formatter:off */
class HarmonyProcessor::Impl
{
public:
    explicit Impl(HarmonyProcessor *parent) :
        parent_{parent},
        analysisCallback([this](const std::string& content) { handleAnalysis(content); }),
        finalCallback([this](const std::string& content) { handleFinal(content); }),
        commentaryCallback([this](const std::string& content) { handleCommentary(content);}),
        functionsCallback([this](const std::string& content) { handleFunctions(content);})
    {}

    using AnalysisCallback = std::function<void(const std::string &)>;
    using FinalCallback = std::function<void(const std::string &)>;
    using CommentaryCallback = std::function<void(const std::string &)>;
    using FunctionsCallback = std::function<void(const std::string &)>;

    AnalysisCallback analysisCallback;
    FinalCallback finalCallback;
    CommentaryCallback commentaryCallback;
    FunctionsCallback functionsCallback;

    void handleAnalysis(const std::string &content);

    void handleFinal(const std::string &content);

    void handleCommentary(const std::string &content);

    void handleFunctions(const std::string &content);

    static  ChannelType determineChannelType(const std::string &channelStr);

    void Clean(){
        m_isFinal = m_isFunctions  =  m_isFunctions =  m_isAnalysis =false;
    }
private:
    bool m_isFinal = false;
    bool m_isCommentary = false;
    bool m_isFunctions = false;
    bool m_isAnalysis = false;
    HarmonyProcessor *parent_;
};
/* @formatter:on */


HarmonyProcessor::HarmonyProcessor() :
        impl_{new Impl{this}},
        currentState(State::INIT)
{
}

std::tuple<bool, std::string> HarmonyProcessor::PreProcessStream(std::string &chunkText,
                                                                 bool isToolResponse,
                                                                 std::string &toolResponse)
{
    m_analysisText.clear();
    m_finalText.clear();

    if (start_tag_.empty())
    {
        goto ahead;
    }

    if (chunkText.find("<|channel|>") != std::string::npos)
    {
        chunkText = start_tag_ + chunkText;
        start_tag_.clear();
    }

    ahead:
    processChunk(chunkText);
    chunkText = m_analysisText + m_finalText;

    return std::make_tuple(false, chunkText);
}

size_t HarmonyProcessor::findTag(const std::string &tag)
{
    if (buffer.empty())
        return std::string::npos;

    // return buffer.find(tag);
    auto it = std::search(buffer.begin(), buffer.end(), tag.begin(), tag.end());
    return it == buffer.end() ? std::string::npos : std::distance(buffer.begin(), it);
}

void HarmonyProcessor::ResetState()
{
    currentState = State::INIT;
    currentChannel = ChannelType::UNKNOWN;
    currentChannelStr.clear();
    currentMessage.clear();
    outputtedLength = 0;
    pendingBuffer.clear();
    buffer.clear();
}

void HarmonyProcessor::processChunk(const std::string &chunk)
{
    static const std::string startTag = "<|start|>assistant<|channel|>";
    static const std::string startFunctionsTag = "<|start|>functions.";
    static const std::string messageTag = "<|message|>";
    static const std::string endTag = "<|end|>";
    static const std::string returnTag = "<|return|>";
    static const std::string callTag = "<|call|>";

    pendingBuffer += chunk;
    buffer = pendingBuffer;

    while (true)
    {
        switch (currentState)
        {
            case State::INIT:
            {
                size_t startPos = findTag(startTag);
                size_t funcPos = findTag(startFunctionsTag);

                // 优先处理先出现的标签
                if (startPos != std::string::npos && (funcPos == std::string::npos || startPos < funcPos))
                {
                    pendingBuffer = pendingBuffer.substr(startPos + startTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_CHANNEL;
                    currentChannelStr.clear();
                }
                else if (funcPos != std::string::npos)
                {
                    pendingBuffer = pendingBuffer.substr(funcPos + startFunctionsTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_CHANNEL;
                    currentChannelStr = "functions.";
                }
                else
                {
                    // 保留足够长的缓冲区以确保能识别标签
                    if (pendingBuffer.length() > std::max(startTag.length(), startFunctionsTag.length()) * 2)
                    {
                        size_t keepLength = std::max(startTag.length(), startFunctionsTag.length());
                        pendingBuffer = pendingBuffer.substr(pendingBuffer.length() - keepLength);
                        buffer = pendingBuffer;
                    }
                    return;
                }
                break;
            }

            case State::IN_CHANNEL:
            {
                size_t msgPos = findTag(messageTag);
                if (msgPos != std::string::npos)
                {
                    currentChannelStr += buffer.substr(0, msgPos);
                    currentChannel = Impl::determineChannelType(currentChannelStr);

                    pendingBuffer = pendingBuffer.substr(msgPos + messageTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_MESSAGE;
                    currentMessage.clear();
                    outputtedLength = 0;
                }
                else
                {
                    // 确保保留足够长度以识别messageTag
                    if (pendingBuffer.length() > messageTag.length() * 2)
                    {
                        currentChannelStr += pendingBuffer.substr(0, pendingBuffer.length() - messageTag.length());
                        pendingBuffer = pendingBuffer.substr(pendingBuffer.length() - messageTag.length());
                        buffer = pendingBuffer;
                    }
                    return;
                }
                break;
            }

            case State::IN_MESSAGE:
            {
                std::string endMarker;
                if (currentChannel == ChannelType::FINAL)
                {
                    endMarker = returnTag;
                }
                else
                {
                    size_t callPos = findTag(callTag);
                    size_t endPos = findTag(endTag);

                    if (callPos != std::string::npos && (endPos == std::string::npos || callPos < endPos))
                    {
                        endMarker = callTag;
                    }
                    else
                    {
                        endMarker = endTag;
                    }
                }

                size_t endPos = findTag(endMarker);
                if (endPos != std::string::npos)
                {
                    currentMessage = buffer.substr(0, endPos);
                    // std::cout << "\n buffer: " << buffer << std::endl;

                    // 处理所有通道的完整消息
                    switch (currentChannel)
                    {
                        case ChannelType::ANALYSIS:
                            if (currentMessage.length() > outputtedLength)
                            {
                                impl_->analysisCallback(currentMessage.substr(outputtedLength));
                                outputtedLength = currentMessage.length();
                            }
                            break;
                        case ChannelType::FINAL:
                            if (currentMessage.length() > outputtedLength)
                            {
                                impl_->finalCallback(currentMessage.substr(outputtedLength));
                                outputtedLength = currentMessage.length();
                            }
                            break;
                        case ChannelType::COMMENTARY:
                            impl_->commentaryCallback(currentMessage);
                            break;
                        case ChannelType::FUNCTIONS:
                            impl_->functionsCallback(currentMessage);
                            break;
                        default:
                            break;
                    }

                    pendingBuffer = pendingBuffer.substr(endPos + endMarker.length());
                    buffer = pendingBuffer;

                    // 不在这里调用 resetState()，而是手动重置必要的状态
                    currentState = State::INIT;
                    currentChannel = ChannelType::UNKNOWN;
                    currentChannelStr.clear();
                    currentMessage.clear();
                    outputtedLength = 0;
                }
                else
                {
                    // 处理部分消息，只输出新增内容
                    std::string newContent = buffer;

                    // 检查是否有标签片段，避免输出标签内容
                    size_t maxSafeLength = newContent.length();
                    for (const auto &tag: {endMarker, callTag, returnTag})
                    {
                        // 只检查以`<|`开头的前缀，避免单独`<`被误判
                        if (tag.substr(0, 2) == "<|")
                        {  // 确保是标签格式
                            for (size_t i = 2; i < tag.length(); ++i)
                            {  // 从`<|`之后开始检查
                                std::string prefix = tag.substr(0, i);
                                size_t pos = newContent.find(prefix);
                                if (pos != std::string::npos && pos < maxSafeLength)
                                {
                                    maxSafeLength = pos;
                                }
                            }
                        }
                    }

                    // 确保至少有一些内容可以输出
                    if (maxSafeLength > outputtedLength)
                    {
                        std::string newOutput = newContent.substr(outputtedLength, maxSafeLength - outputtedLength);
                        outputtedLength = maxSafeLength;

                        if (currentChannel == ChannelType::ANALYSIS)
                        {
                            impl_->analysisCallback(newOutput);
                        }
                        else if (currentChannel == ChannelType::FINAL)
                        {
                            impl_->finalCallback(newOutput);
                        }
                    }

                    // 限制缓冲区大小，防止内存溢出
                    if (pendingBuffer.length() > 1024 * 1024)
                    {  // 1MB上限
                        pendingBuffer = pendingBuffer.substr(pendingBuffer.length() - (endMarker.length() * 2));
                        buffer = pendingBuffer;
                    }
                    return;
                }
                break;
            }

            default:
                ResetState();
                break;
        }
    }
}

HarmonyProcessor::~HarmonyProcessor()
{
    delete impl_;
}

void HarmonyProcessor::Clean()
{
    impl_->Clean();
    start_tag_ = "<|start|>assistant";
    ResetState();
}

void HarmonyProcessor::Impl::handleAnalysis(const std::string &content)
{
    if (!m_isAnalysis)
    {
        m_isAnalysis = true;
        parent_->m_analysisText = "### {💭Thinking💭}\n";
    }
    parent_->m_analysisText += content;
}

void HarmonyProcessor::Impl::handleFinal(const std::string &content)
{
    if (!m_isFinal)
    {
        m_isFinal = true;
        parent_->m_finalText = "<br><br>\n### {💡Answer💡}\n";
    }
    parent_->m_finalText += content;
}

void HarmonyProcessor::Impl::handleCommentary(const std::string &content)
{
    // 对commentary进行特殊处理，这里只是记录到日志
    // std::cout << "[Commentary] " << content << std::endl;
}

void HarmonyProcessor::Impl::handleFunctions(const std::string &content)
{
    // 对函数调用结果进行特殊处理
    // std::cout << "[Functions] " << content << std::endl;
}

HarmonyProcessor::ChannelType HarmonyProcessor::Impl::determineChannelType(const std::string &channelStr)
{
    if (channelStr.find("analysis") != std::string::npos)
    {
        return ChannelType::ANALYSIS;
    }
    else if (channelStr.find("final") != std::string::npos)
    {
        return ChannelType::FINAL;
    }
    else if (channelStr.find("commentary") != std::string::npos)
    {
        return ChannelType::COMMENTARY;
    }
    else if (channelStr.find("functions.") != std::string::npos)
    {
        return ChannelType::FUNCTIONS;
    }
    return ChannelType::UNKNOWN;
}


