//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "harmony.h"
#include "log.h"
#include <nlohmann/json.hpp>
#include "../response/response_tools.h"
#include <sstream>
#include <atomic>
#include <regex>
#include <random>

using json = nlohmann::ordered_json;

static bool IsPreamble(const std::string& content);

static size_t FindSafeUtf8Boundary(const std::string& str, size_t pos) {
    if (pos == 0) {
        return 0;
    }

    if (pos > str.length()) {
        pos = str.length();
    }

    size_t checkPos = pos - 1;
    unsigned char c = static_cast<unsigned char>(str[checkPos]);

    if (c < 0x80) {
        return pos;
    }

    if (c >= 0xC0) {

        int bytesNeeded;
        if (c < 0xE0) bytesNeeded = 2;
        else if (c < 0xF0) bytesNeeded = 3;
        else bytesNeeded = 4;

        if (checkPos + bytesNeeded <= str.length()) {

            return pos;
        } else {

            return checkPos;
        }
    }

    while (checkPos > 0) {
        checkPos--;
        c = static_cast<unsigned char>(str[checkPos]);

        if (c < 0x80) {
            return checkPos + 1;
        }

        if (c >= 0xC0) {

            int bytesNeeded;
            if (c < 0xE0) bytesNeeded = 2;
            else if (c < 0xF0) bytesNeeded = 3;
            else bytesNeeded = 4;

            if (checkPos + bytesNeeded <= pos) {

                return pos;
            } else {

                return checkPos;
            }
        }
    }

    return 0;
}

static std::atomic<uint64_t> g_tool_call_id_counter{0};

static std::string GenerateToolCallId(const std::string& function_name = "") {

    static thread_local std::mt19937_64 gen(std::random_device{}());
    static thread_local std::uniform_int_distribution<uint64_t> dis;

    uint64_t id = g_tool_call_id_counter.fetch_add(1, std::memory_order_relaxed);
    auto now = std::chrono::system_clock::now();
    auto timestamp = std::chrono::duration_cast<std::chrono::microseconds>(
        now.time_since_epoch()
    ).count();
    uint64_t random_part = dis(gen);

    std::ostringstream oss;
    oss << "call_";

    if (!function_name.empty()) {
        oss << function_name << "_";
    }

    oss << timestamp << "_" << id << "_" << std::hex << random_part;
    return oss.str();
}

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
        m_isFinal = m_isCommentary = m_isFunctions = m_isAnalysis = false;
    }
private:
    bool m_isFinal = false;
    bool m_isCommentary = false;
    bool m_isFunctions = false;
    bool m_isAnalysis = false;
    HarmonyProcessor *parent_;
};

HarmonyProcessor::HarmonyProcessor() :
        impl_{new Impl{this}},
        currentState(State::INIT)
{
}

std::tuple<bool, std::string> HarmonyProcessor::preprocessStream(std::string &chunkText,
                                                                 bool isToolResponse,
                                                                 std::string &toolResponse)
{

    size_t prevLength = m_finalText.length();

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

    std::string newContent;
    if (m_finalText.length() > prevLength) {
        newContent = m_finalText.substr(prevLength);
    }
    chunkText = newContent;

    if (m_isToolCall)
    {
        return std::make_tuple(true, m_toolCallContent);
    }

    return std::make_tuple(false, "");
}

size_t HarmonyProcessor::findTag(const std::string &tag)
{
    if (buffer.empty()) return std::string::npos;

    auto it = std::search(buffer.begin(), buffer.end(), tag.begin(), tag.end());
    return it == buffer.end() ? std::string::npos : std::distance(buffer.begin(), it);
}

void HarmonyProcessor::ResetState()
{
    currentState = State::INIT;
    currentChannel = ChannelType::UNKNOWN;
    currentChannelStr.clear();
    currentMessage.clear();
    currentRole.clear();
    currentToParam.clear();
    currentConstrain.clear();
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

    auto now = std::chrono::steady_clock::now();

    if (currentState != State::INIT &&
        lastChunkTime_.time_since_epoch().count() > 0) {
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            now - lastChunkTime_
        ).count();

        if (elapsed > STREAM_TIMEOUT_SECONDS) {
            My_Log{My_Log::Level::kDebug}
                << "Stream timeout detected (" << elapsed << "s), forcing message completion" << std::endl;

            if (currentState == State::IN_MESSAGE && !currentMessage.empty()) {
                My_Log{My_Log::Level::kDebug}
                    << "Incomplete message: " << currentMessage << std::endl;

                switch (currentChannel) {
                    case ChannelType::ANALYSIS:
                        impl_->analysisCallback(currentMessage);
                        break;
                    case ChannelType::FINAL:
                        impl_->finalCallback(currentMessage);
                        break;
                    case ChannelType::COMMENTARY:
                        impl_->commentaryCallback(currentMessage);
                        break;
                    default:
                        break;
                }
            }

            ResetState();
        }
    }

    lastChunkTime_ = now;

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
                static const std::string constrainTag = "<|constrain|>";

                size_t msgPos = findTag(messageTag);
                size_t constrainPos = findTag(constrainTag);

                if (constrainPos != std::string::npos &&
                    (msgPos == std::string::npos || constrainPos < msgPos))
                {

                    std::string header = buffer.substr(0, constrainPos);
                    currentChannelStr += header;

                    currentToParam = parseToParam(currentChannelStr);

                    currentChannel = Impl::determineChannelType(currentChannelStr);

                    My_Log{My_Log::Level::kDebug} << "[Channel Parsed] channelStr: " << currentChannelStr
                             << ", toParam: " << currentToParam
                             << ", channelType: " << static_cast<int>(currentChannel) << std::endl;

                    pendingBuffer = pendingBuffer.substr(constrainPos + constrainTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_CONSTRAIN;
                    currentConstrain.clear();
                }
                else if (msgPos != std::string::npos)
                {

                    std::string header = buffer.substr(0, msgPos);
                    currentChannelStr += header;

                    currentToParam = parseToParam(currentChannelStr);

                    currentChannel = Impl::determineChannelType(currentChannelStr);

                    My_Log{My_Log::Level::kDebug} << "[Channel Parsed] channelStr: " << currentChannelStr
                             << ", toParam: " << currentToParam
                             << ", channelType: " << static_cast<int>(currentChannel) << std::endl;

                    pendingBuffer = pendingBuffer.substr(msgPos + messageTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_MESSAGE;
                    currentMessage.clear();
                    outputtedLength = 0;
                }
                else
                {

                    size_t maxTagLength = std::max(messageTag.length(), constrainTag.length());
                    if (pendingBuffer.length() > maxTagLength * 2)
                    {
                        currentChannelStr += pendingBuffer.substr(0, pendingBuffer.length() - maxTagLength);
                        pendingBuffer = pendingBuffer.substr(pendingBuffer.length() - maxTagLength);
                        buffer = pendingBuffer;
                    }
                    return;
                }
                break;
            }

            case State::IN_CONSTRAIN:
            {
                size_t msgPos = findTag(messageTag);
                if (msgPos != std::string::npos)
                {

                    currentConstrain = buffer.substr(0, msgPos);

                    pendingBuffer = pendingBuffer.substr(msgPos + messageTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_MESSAGE;
                    currentMessage.clear();
                    outputtedLength = 0;
                }
                else
                {

                    if (pendingBuffer.length() > messageTag.length() * 2)
                    {
                        currentConstrain += pendingBuffer.substr(0, pendingBuffer.length() - messageTag.length());
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

                            if (currentMessage.length() > outputtedLength)
                            {
                                impl_->commentaryCallback(currentMessage.substr(outputtedLength));
                                outputtedLength = currentMessage.length();
                            }

                            if (!currentToParam.empty() && currentToParam.find("functions.") == 0)
                            {
                                std::string funcName = currentToParam.substr(10);

                                My_Log{My_Log::Level::kDebug} << "[Message Complete] Processing tool call for function: " << funcName << std::endl;

                                My_Log{My_Log::Level::kDebug} << "[Message Complete] currentMessage: " << currentMessage << std::endl;

                                m_isToolCall = true;

                                json tool_call_json;
                                tool_call_json["name"] = funcName;

                                std::string fixed_msg = ResponseTools::fixBackslashes(currentMessage, true);

                                std::string repaired_msg = ResponseTools::repairJson(fixed_msg);
                                bool args_parsed_ok = true;
                                try {
                                    json args = json::parse(repaired_msg);

                                    if (args.is_object()
                                        && args.contains("name") && args["name"].is_string()
                                        && args["name"].get<std::string>() == funcName
                                        && args.contains("arguments"))
                                    {
                                        tool_call_json["arguments"] = args["arguments"];
                                        My_Log{My_Log::Level::kDebug} << "[Message Complete] Detected wrapped format {name,arguments}, unwrapped inner arguments" << std::endl;
                                    }
                                    else
                                    {
                                        tool_call_json["arguments"] = args;
                                    }
                                    My_Log{My_Log::Level::kDebug} << "[Message Complete] Successfully parsed arguments as JSON" << std::endl;
                                } catch (const std::exception& e) {
                                    args_parsed_ok = false;
                                    My_Log{My_Log::Level::kDebug}
                                        << "[Message Complete] Failed to parse arguments as JSON: " << e.what() << std::endl;
                                }

                                if (args_parsed_ok)
                                {
                                    m_toolCallContent = "<tool_call>" + tool_call_json.dump() + "</tool_call>";
                                }
                                else
                                {
                                    m_toolCallContent = "<tool_call>{\"name\":" + json(funcName).dump()
                                        + ",\"arguments\":" + currentMessage + "}</tool_call>";
                                }
                                m_toolCallFunctionName = funcName;

                                My_Log{My_Log::Level::kDebug} << "[Tool Call] Function: " << funcName << std::endl;
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] Arguments: " << currentMessage << std::endl;
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] Formatted content: " << m_toolCallContent << std::endl;
                            }

                            else if (!currentToParam.empty() && currentToParam == "functions")
                            {
                                My_Log{My_Log::Level::kDebug} << "[Message Complete] Non-standard tool call format: to=functions, parsing name from JSON body" << std::endl;
                                My_Log{My_Log::Level::kDebug} << "[Message Complete] currentMessage: " << currentMessage << std::endl;

                                std::string fixed_msg = ResponseTools::fixBackslashes(currentMessage, true);
                                std::string repaired_msg = ResponseTools::repairJson(fixed_msg);
                                try {
                                    json body = json::parse(repaired_msg);
                                    if (body.contains("name") && body["name"].is_string()) {
                                        std::string funcName = body["name"].get<std::string>();
                                        m_isToolCall = true;
                                        json tool_call_json;
                                        tool_call_json["name"] = funcName;
                                        if (body.contains("arguments")) {
                                            tool_call_json["arguments"] = body["arguments"];
                                        } else {
                                            tool_call_json["arguments"] = json::object();
                                        }
                                        m_toolCallContent = "<tool_call>" + tool_call_json.dump() + "</tool_call>";
                                        m_toolCallFunctionName = funcName;
                                        My_Log{My_Log::Level::kDebug} << "[Tool Call] Non-standard format resolved. Function: " << funcName << std::endl;
                                        My_Log{My_Log::Level::kDebug} << "[Tool Call] Formatted content: " << m_toolCallContent << std::endl;
                                    } else {

                                        My_Log{My_Log::Level::kWarning} << "[Message Complete] Non-standard tool call: JSON body has no 'name' field. Marking as unknown tool call for Layer1/2 recovery instead of dropping." << std::endl;
                                        m_isToolCall = true;
                                        m_toolCallContent = "<tool_call>{\"name\":\"\",\"arguments\":" + currentMessage + "}</tool_call>";
                                        m_toolCallFunctionName = "";
                                    }
                                } catch (const std::exception& e) {

                                    My_Log{My_Log::Level::kWarning} << "[Message Complete] Non-standard tool call: failed to parse JSON body: " << e.what() << ". Marking as unknown tool call for Layer1/2 recovery instead of dropping." << std::endl;
                                    m_isToolCall = true;
                                    m_toolCallContent = "<tool_call>{\"name\":\"\",\"arguments\":" + currentMessage + "}</tool_call>";
                                    m_toolCallFunctionName = "";
                                }
                            }
                            break;
                        case ChannelType::FUNCTIONS:
                            impl_->functionsCallback(currentMessage);
                            break;
                        default:
                            break;
                    }

                    std::string historyMessage = "<|start|>assistant";

                    if (!currentChannelStr.empty()) {
                        historyMessage += "<|channel|>" + currentChannelStr;
                    }

                    if (!currentConstrain.empty()) {
                        historyMessage += " <|constrain|>" + currentConstrain;
                    }

                    historyMessage += "<|message|>" + currentMessage;

                    historyMessage += "<|end|>";

                    if (currentChannel == ChannelType::ANALYSIS) {
                        m_analysisMessages.push_back(historyMessage);
                    } else if (currentChannel == ChannelType::FINAL) {
                        m_finalMessages.push_back(historyMessage);
                    } else if (currentChannel == ChannelType::COMMENTARY) {
                        m_commentaryMessages.push_back(historyMessage);
                    }

                    pendingBuffer = pendingBuffer.substr(endPos + endMarker.length());
                    buffer = pendingBuffer;

                    currentState = State::INIT;
                    currentChannel = ChannelType::UNKNOWN;
                    currentChannelStr.clear();
                    currentMessage.clear();
                    currentToParam.clear();
                    currentConstrain.clear();
                    outputtedLength = 0;
                }
                else
                {

                    std::string newContent = buffer;

                    size_t maxSafeLength = newContent.length();
                    for (const auto &tag: {endMarker, callTag, returnTag})
                    {

                        if (tag.substr(0, 2) == "<|")
                        {
                            for (size_t i = 2; i < tag.length(); ++i)
                            {
                                std::string prefix = tag.substr(0, i);
                                size_t pos = newContent.find(prefix);
                                if (pos != std::string::npos && pos < maxSafeLength)
                                {
                                    maxSafeLength = pos;
                                }
                            }
                        }
                    }

                    if (maxSafeLength > outputtedLength)
                    {

                        size_t safeLength = FindSafeUtf8Boundary(newContent, maxSafeLength);

                        if (safeLength > outputtedLength)
                        {
                            std::string newOutput = newContent.substr(outputtedLength, safeLength - outputtedLength);
                            outputtedLength = safeLength;

                            if (currentChannel == ChannelType::ANALYSIS)
                            {
                                impl_->analysisCallback(newOutput);
                            }
                            else if (currentChannel == ChannelType::FINAL)
                            {
                                impl_->finalCallback(newOutput);
                            }
                            else if (currentChannel == ChannelType::COMMENTARY)
                            {

                                impl_->commentaryCallback(newOutput);
                            }
                        }
                    }

                    if (pendingBuffer.length() > 1024 * 1024)
                    {
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
    m_finalText.clear();
    m_internalAnalysisBuffer.clear();
    m_reasoningSent = 0;
    m_commentaryText.clear();
    m_isToolCall = false;
    m_toolCallContent.clear();
    m_toolCallFunctionName.clear();

    m_analysisMessages.clear();
    m_finalMessages.clear();
    m_commentaryMessages.clear();

    ResetState();
}

std::string HarmonyProcessor::TakeReasoningDelta()
{
    if (m_reasoningSent >= m_internalAnalysisBuffer.size())
    {
        return {};
    }
    const size_t end = FindSafeUtf8Boundary(m_internalAnalysisBuffer, m_internalAnalysisBuffer.size());
    if (end <= m_reasoningSent)
    {
        return {};
    }
    std::string delta = m_internalAnalysisBuffer.substr(m_reasoningSent, end - m_reasoningSent);
    m_reasoningSent = end;
    return delta;
}

void HarmonyProcessor::FinalizeToolCall()
{

    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Called:"
                 << " state=" << static_cast<int>(currentState)
                 << ", channel=" << static_cast<int>(currentChannel)
                 << ", currentToParam='" << currentToParam << "'"
                 << ", currentChannelStr='" << currentChannelStr << "'"
                 << ", currentMessage.length=" << currentMessage.length()
                 << ", m_commentaryText.length=" << m_commentaryText.length()
                 << ", pendingBuffer.length=" << pendingBuffer.length()
                 << ", outputtedLength=" << outputtedLength
                 << ", m_isToolCall=" << m_isToolCall
                 << std::endl;
    }

    if (m_isToolCall && !m_toolCallContent.empty())
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeToolCall] Tool call already correctly set during stream processing, skipping re-processing."
                << " func='" << m_toolCallFunctionName << "'"
                << ", content.length=" << m_toolCallContent.length() << std::endl;
        }
        return;
    }

    std::string funcName;
    bool isToolCall = false;
    bool isNonStandardFormat = false;
    std::string detectedFromMessage;

    if (!currentToParam.empty() && currentToParam.find("functions.") == 0)
    {
        funcName = currentToParam.substr(10);
        isToolCall = true;
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected tool call from currentToParam: " << funcName << std::endl;
        }
    }

    else if (!currentToParam.empty() && currentToParam == "functions")
    {
        isToolCall = true;
        isNonStandardFormat = true;
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected non-standard tool call from currentToParam=functions, will extract name from JSON body" << std::endl;
        }
    }

    else if (currentChannel == ChannelType::COMMENTARY && !currentMessage.empty())
    {

        size_t toPos = currentChannelStr.find("to=functions.");
        if (toPos != std::string::npos)
        {
            size_t start = toPos + 13;
            size_t end = currentChannelStr.find_first_of(" <|", start);
            if (end == std::string::npos)
                end = currentChannelStr.length();

            funcName = currentChannelStr.substr(start, end - start);
            isToolCall = true;
            if (ResponseTools::log_inference_stream)
            {
                My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected tool call from currentChannelStr: " << funcName << std::endl;
            }
        }

        else
        {
            size_t toPos2 = currentChannelStr.find("to=functions");
            if (toPos2 != std::string::npos)
            {

                size_t afterPos = toPos2 + 12;
                if (afterPos >= currentChannelStr.length() || currentChannelStr[afterPos] == ' ' || currentChannelStr[afterPos] == '<')
                {
                    isToolCall = true;
                    isNonStandardFormat = true;
                    if (ResponseTools::log_inference_stream)
                    {
                        My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected non-standard tool call from currentChannelStr=to=functions, will extract name from JSON body" << std::endl;
                    }
                }
            }
        }
    }

    else if (!m_commentaryText.empty())
    {

        if (!m_commentaryMessages.empty())
        {
            const std::string& lastMsg = m_commentaryMessages.back();
            size_t toPos = lastMsg.find("to=functions.");
            if (toPos != std::string::npos)
            {
                size_t start = toPos + 13;
                size_t end = lastMsg.find_first_of(" <|", start);
                if (end == std::string::npos)
                    end = lastMsg.length();

                funcName = lastMsg.substr(start, end - start);
                isToolCall = true;
                detectedFromMessage = lastMsg;
                if (ResponseTools::log_inference_stream)
                {
                    My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected tool call from m_commentaryMessages: " << funcName << std::endl;
                }
            }

            else
            {
                size_t toPos2 = lastMsg.find("to=functions");
                if (toPos2 != std::string::npos)
                {
                    size_t afterPos = toPos2 + 12;
                    if (afterPos >= lastMsg.length() || lastMsg[afterPos] == ' ' || lastMsg[afterPos] == '<')
                    {
                        isToolCall = true;
                        isNonStandardFormat = true;
                        detectedFromMessage = lastMsg;
                        if (ResponseTools::log_inference_stream)
                        {
                            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected non-standard tool call from m_commentaryMessages=to=functions, will extract name from JSON body" << std::endl;
                        }
                    }
                }
            }
        }
    }

    if (isToolCall && (!funcName.empty() || isNonStandardFormat))
    {

        std::string toolContent;
        std::string toolContentSource;

        if (!currentMessage.empty())
        {
            toolContent = currentMessage;
            toolContentSource = "currentMessage";
        }
        else if (!detectedFromMessage.empty())
        {

            size_t msgPos = detectedFromMessage.find("<|message|>");
            size_t endPos = detectedFromMessage.rfind("<|end|>");
            if (msgPos != std::string::npos && endPos != std::string::npos
                && endPos > msgPos + 11)
            {
                toolContent = detectedFromMessage.substr(msgPos + 11, endPos - (msgPos + 11));
                toolContentSource = "m_commentaryMessages[last].<|message|> content";
            }
            else
            {

                toolContent = m_commentaryText;
                toolContentSource = "m_commentaryText(fallback, message extraction failed)";
                My_Log{My_Log::Level::kWarning}
                    << "[FinalizeToolCall] Failed to extract content from detectedFromMessage, "
                    << "falling back to m_commentaryText. "
                    << "detectedFromMessage='" << detectedFromMessage.substr(0, 80) << "'" << std::endl;
            }

        }
        else if (!m_commentaryText.empty())
        {
            toolContent = m_commentaryText;
            toolContentSource = "m_commentaryText";
        }
        else if (!pendingBuffer.empty())
        {

            toolContent = pendingBuffer.length() > outputtedLength
                          ? pendingBuffer.substr(outputtedLength)
                          : pendingBuffer;
            toolContentSource = "pendingBuffer(EOG fallback, offset=" + std::to_string(outputtedLength) + ")";
        }

        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] toolContent source: " << toolContentSource
                     << ", length=" << toolContent.length()
                     << ", preview='" << toolContent.substr(0, std::min(toolContent.length(), size_t(80))) << "'"
                     << std::endl;
        }

        if (toolContent.empty())
        {
            My_Log{My_Log::Level::kWarning}
                << "[FinalizeToolCall] Tool call detected (func='" << funcName
                << "') but no content available from any source "
                << "(currentMessage=" << currentMessage.length()
                << ", m_commentaryText=" << m_commentaryText.length()
                << ", pendingBuffer=" << pendingBuffer.length()
                << ", outputtedLength=" << outputtedLength << ")" << std::endl;
            return;
        }

        if (isNonStandardFormat)
        {
            std::string fixed_content = ResponseTools::fixBackslashes(toolContent, true);
            std::string repaired_content = ResponseTools::repairJson(fixed_content);
            try {
                json body = json::parse(repaired_content);
                if (body.contains("name") && body["name"].is_string()) {
                    funcName = body["name"].get<std::string>();
                    m_isToolCall = true;
                    json tool_call_json;
                    tool_call_json["name"] = funcName;
                    if (body.contains("arguments")) {
                        tool_call_json["arguments"] = body["arguments"];
                    } else {
                        tool_call_json["arguments"] = json::object();
                    }
                    m_toolCallContent = "<tool_call>" + tool_call_json.dump() + "</tool_call>";
                    m_toolCallFunctionName = funcName;
                    if (ResponseTools::log_inference_stream)
                    {
                        My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Non-standard format resolved. Function: " << funcName
                                 << ", content='" << m_toolCallContent << "'" << std::endl;
                    }
                } else {

                    My_Log{My_Log::Level::kWarning} << "[FinalizeToolCall] Non-standard tool call: JSON body has no 'name' field. Marking as unknown tool call for Layer1/2 recovery instead of dropping." << std::endl;
                    m_isToolCall = true;
                    m_toolCallContent = "<tool_call>{\"name\":\"\",\"arguments\":" + toolContent + "}</tool_call>";
                    m_toolCallFunctionName = "";
                }
            } catch (const std::exception& e) {

                My_Log{My_Log::Level::kWarning} << "[FinalizeToolCall] Non-standard tool call: failed to parse JSON body: " << e.what() << ". Marking as unknown tool call for Layer1/2 recovery instead of dropping." << std::endl;
                m_isToolCall = true;
                m_toolCallContent = "<tool_call>{\"name\":\"\",\"arguments\":" + toolContent + "}</tool_call>";
                m_toolCallFunctionName = "";
            }
            return;
        }

        m_isToolCall = true;

        json tool_call_json;
        tool_call_json["name"] = funcName;

        std::string fixed_content = ResponseTools::fixBackslashes(toolContent, true);

        std::string repaired_content = ResponseTools::repairJson(fixed_content);
        bool args_parsed_ok = true;
        try {
            json args = json::parse(repaired_content);

            if (args.is_object()
                && args.contains("name") && args["name"].is_string()
                && args["name"].get<std::string>() == funcName
                && args.contains("arguments"))
            {
                tool_call_json["arguments"] = args["arguments"];
                if (ResponseTools::log_inference_stream)
                {
                    My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected wrapped format {name,arguments}, unwrapped inner arguments"
                             << " (source=" << toolContentSource << ")" << std::endl;
                }
            }
            else
            {
                tool_call_json["arguments"] = args;
            }
            if (ResponseTools::log_inference_stream)
            {
                My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Successfully parsed arguments as JSON"
                         << " (source=" << toolContentSource << ")" << std::endl;
            }
        } catch (const std::exception& e) {
            args_parsed_ok = false;
            My_Log{My_Log::Level::kWarning}
                << "[FinalizeToolCall] Failed to parse arguments as JSON: " << e.what()
                << " (source=" << toolContentSource << ")" << std::endl;
            My_Log{My_Log::Level::kWarning}
                << "[FinalizeToolCall] Raw content: '" << toolContent << "'" << std::endl;
        }

        if (args_parsed_ok)
        {
            m_toolCallContent = "<tool_call>" + tool_call_json.dump() + "</tool_call>";
        }
        else
        {
            m_toolCallContent = "<tool_call>{\"name\":" + json(funcName).dump()
                + ",\"arguments\":" + toolContent + "}</tool_call>";
        }
        m_toolCallFunctionName = funcName;

        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] ✓ Tool call built:"
                     << " func='" << funcName << "'"
                     << ", content='" << m_toolCallContent << "'" << std::endl;
        }
    }
    else
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] No tool call detected"
                     << " (currentToParam='" << currentToParam << "'"
                     << ", channel=" << static_cast<int>(currentChannel)
                     << ", channelStr='" << currentChannelStr << "'"
                     << ", currentMessage.length=" << currentMessage.length()
                     << ", m_commentaryText.length=" << m_commentaryText.length()
                     << ", m_commentaryMessages.size=" << m_commentaryMessages.size()
                     << ")" << std::endl;
        }
    }
}

std::string HarmonyProcessor::FinalizeFinalChannel()
{

    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo}
            << "[FinalizeFinalChannel] Called: state=" << static_cast<int>(currentState)
            << ", channel=" << static_cast<int>(currentChannel)
            << ", pendingBuffer.length=" << pendingBuffer.length()
            << ", outputtedLength=" << outputtedLength
            << ", m_finalText.length=" << m_finalText.length() << std::endl;
    }

    if (currentState != State::IN_MESSAGE || currentChannel != ChannelType::FINAL)
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeFinalChannel] Skip: not in final channel IN_MESSAGE state. "
                << "No pending final content to flush." << std::endl;
        }
        return "";
    }

    std::string remaining = pendingBuffer;
    if (remaining.empty() || remaining.length() <= outputtedLength)
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeFinalChannel] Skip: no remaining content "
                << "(remaining.length=" << remaining.length()
                << ", outputtedLength=" << outputtedLength << ")" << std::endl;
        }
        return "";
    }

    std::string unoutputed = remaining.substr(outputtedLength);

    static const std::vector<std::string> stop_tags = {"<|return|>", "<|end|>", "<|call|>"};
    size_t safeLength = unoutputed.length();
    for (const auto& tag : stop_tags)
    {

        for (size_t i = 2; i < tag.length(); ++i)
        {
            std::string prefix = tag.substr(0, i);
            if (unoutputed.length() >= prefix.length())
            {
                std::string tail = unoutputed.substr(unoutputed.length() - prefix.length());
                if (tail == prefix)
                {
                    size_t candidate = unoutputed.length() - prefix.length();
                    if (candidate < safeLength)
                    {
                        safeLength = candidate;
                        if (ResponseTools::log_inference_stream)
                        {
                            My_Log{My_Log::Level::kInfo}
                                << "[FinalizeFinalChannel] Trimming trailing tag prefix \"" << prefix
                                << "\" from end of content (length=" << unoutputed.length()
                                << ", safe=" << candidate << ")" << std::endl;
                        }
                    }
                }
            }
        }
    }

    if (safeLength == 0)
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeFinalChannel] Skip: all remaining content is tag prefix, nothing safe to flush. "
                << "unoutputed=\"" << unoutputed << "\"" << std::endl;
        }
        return "";
    }

    safeLength = FindSafeUtf8Boundary(unoutputed, safeLength);
    if (safeLength == 0)
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeFinalChannel] Skip: UTF-8 boundary check resulted in 0 safe bytes." << std::endl;
        }
        return "";
    }

    std::string toFlush = unoutputed.substr(0, safeLength);

    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo}
            << "[FinalizeFinalChannel] Flushing " << toFlush.length()
            << " bytes (of " << unoutputed.length() << " remaining) to final channel. "
            << "Content preview: \"" << toFlush.substr(0, std::min(toFlush.length(), size_t(80))) << "\"" << std::endl;
    }

    size_t prevFinalLength = m_finalText.length();

    impl_->finalCallback(toFlush);
    outputtedLength += safeLength;

    std::string newContent;
    if (m_finalText.length() > prevFinalLength)
    {
        newContent = m_finalText.substr(prevFinalLength);
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeFinalChannel] Successfully flushed " << newContent.length()
                << " bytes to client output." << std::endl;
        }
    }
    else
    {
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo}
                << "[FinalizeFinalChannel] Warning: finalCallback did not add content to m_finalText." << std::endl;
        }
    }

    return newContent;
}

void HarmonyProcessor::Impl::handleAnalysis(const std::string &content)
{

    if (!m_isAnalysis)
    {
        m_isAnalysis = true;
        My_Log{My_Log::Level::kDebug} << "[Analysis Channel - Internal Only - Not for Client]" << std::endl;
    }

    parent_->m_internalAnalysisBuffer += content;

    if (!parent_->m_finalText.empty())
    {

        if (parent_->m_finalText.find(content) != std::string::npos)
        {
            My_Log{My_Log::Level::kError}
                << "[SECURITY] CRITICAL: Analysis content leaked to client output!" << std::endl;
            My_Log{My_Log::Level::kError}
                << "[SECURITY] This is a security violation. Analysis content contains unsafe material." << std::endl;
            My_Log{My_Log::Level::kError}
                << "[SECURITY] Leaked content length: " << content.length() << " bytes" << std::endl;

            size_t pos = parent_->m_finalText.find(content);
            parent_->m_finalText.erase(pos, content.length());

            My_Log{My_Log::Level::kError}
                << "[SECURITY] Emergency fix applied: removed leaked content from output" << std::endl;
            My_Log{My_Log::Level::kError}
                << "[SECURITY] Please report this bug to the development team!" << std::endl;
        }
    }

    My_Log{My_Log::Level::kDebug} << "[Analysis Content] " << content << std::endl;
}

void HarmonyProcessor::Impl::handleFinal(const std::string &content)
{
    if (!m_isFinal)
    {
        m_isFinal = true;
    }

    parent_->m_finalText += content;
}

void HarmonyProcessor::Impl::handleCommentary(const std::string &content)
{

    if (!parent_->currentToParam.empty() &&
        parent_->currentToParam.find("functions.") == 0)
    {

        if (!m_isCommentary)
        {
            m_isCommentary = true;
        }

        parent_->m_commentaryText += content;

    }
    else
    {

        if (!m_isCommentary)
        {
            m_isCommentary = true;
        }
        parent_->m_commentaryText += content;

        if (IsPreamble(content)) {
            parent_->m_finalText += content;
        }
    }
}

static bool IsPreamble(const std::string& content) {

    if (!content.empty() && (content[0] == '{' || content[0] == '[')) {
        try {

            auto parsed = json::parse(content);
            (void)parsed;
            return false;
        } catch (...) {

        }
    }

    static const std::vector<std::string> preamble_keywords = {
        "Action plan:",
        "**Action plan**",
        "Will start executing",
        "Will execute",
        "Step by step:",
        "Let me",
        "I will",
        "I'll",
        "First,",
        "Then,",
        "Finally,",
        "---",
        "Planning to",
        "Going to",
        "About to"
    };

    for (const auto& keyword : preamble_keywords) {
        if (content.find(keyword) != std::string::npos) {
            return true;
        }
    }

    std::regex numbered_list_regex(R"(^\s*\d+\.\s)");
    if (std::regex_search(content, numbered_list_regex)) {
        return true;
    }

    std::regex multiline_list_regex(R"(\d+\.\s[^\n]+\n\s*\d+\.\s)");
    if (std::regex_search(content, multiline_list_regex)) {
        return true;
    }

    return false;
}

void HarmonyProcessor::Impl::handleFunctions(const std::string &content)
{

    try
    {
        json funcCall = json::parse(content);

        if (funcCall.contains("name") && funcCall.contains("arguments"))
        {
            std::string funcName = funcCall["name"];
            json funcArgs = funcCall["arguments"];

            parent_->m_isToolCall = true;

            parent_->m_toolCallContent = content;
            parent_->m_toolCallFunctionName = funcName;

            My_Log{My_Log::Level::kDebug} << "[Function Call] " << funcName
                     << " with args: " << funcArgs.dump() << std::endl;
        }
    }
    catch (const std::exception& e)
    {
        My_Log{My_Log::Level::kError}
            << "Failed to parse function call: " << e.what() << std::endl;
    }
}

HarmonyProcessor::ChannelType HarmonyProcessor::Impl::determineChannelType(const std::string &channelStr)
{

    if (channelStr.find("to=functions.") != std::string::npos)
    {

        return ChannelType::COMMENTARY;
    }

    if (channelStr.find("functions.") != std::string::npos)
    {
        return ChannelType::FUNCTIONS;
    }

    if (channelStr.find("commentary") != std::string::npos)
    {
        return ChannelType::COMMENTARY;
    }
    else if (channelStr.find("analysis") != std::string::npos)
    {
        return ChannelType::ANALYSIS;
    }
    else if (channelStr.find("final") != std::string::npos)
    {
        return ChannelType::FINAL;
    }

    return ChannelType::UNKNOWN;
}

std::string HarmonyProcessor::parseToParam(const std::string& header)
{
    size_t toPos = header.find("to=");
    if (toPos == std::string::npos)
        return "";

    size_t start = toPos + 3;
    size_t end = header.find_first_of(" <|", start);
    if (end == std::string::npos)
        end = header.length();

    return header.substr(start, end - start);
}

std::string HarmonyProcessor::BuildSystemMessage(
    const std::string& knowledge_cutoff,
    const std::string& current_date,
    const std::string& reasoning_level,
    bool has_tools)
{
    std::string system_msg = "<|start|>system<|message|>You are ChatGPT, a large language model trained by OpenAI.\n";
    system_msg += "Knowledge cutoff: " + knowledge_cutoff + "\n";
    system_msg += "Current date: " + current_date + "\n\n";
    system_msg += "Reasoning: " + reasoning_level + "\n\n";
    system_msg += "# Valid channels: analysis, commentary, final. Channel must be included for every message.";

    if (has_tools) {
        system_msg += "\nCalls to these tools must go to the commentary channel: 'functions'.";
    }

    system_msg += "<|end|>";
    return system_msg;
}

std::string HarmonyProcessor::BuildDeveloperMessage(
    const std::string& instructions,
    const json& tools)
{
    std::string developer_msg = "<|start|>developer<|message|># Instructions\n\n";
    developer_msg += instructions;

    if (!tools.is_null() && !tools.empty()) {
        developer_msg += "\n\n";
        developer_msg += ConvertToolsToTypeScript(tools);
    }

    developer_msg += "<|end|>";
    return developer_msg;
}

std::string HarmonyProcessor::BuildUserMessage(const std::string& content)
{
    return "<|start|>user<|message|>" + content + "<|end|>";
}

std::string HarmonyProcessor::BuildAssistantMessage(
    const std::string& channel,
    const std::string& content)
{

    std::string msg = "<|start|>assistant<|channel|>" + channel + "<|message|>" + content;
    msg += "<|end|>";
    return msg;
}

std::string HarmonyProcessor::BuildToolMessage(
    const std::string& tool_name,
    const std::string& content)
{

    std::string full_name = tool_name;
    if (full_name.find("functions.") != 0) {
        full_name = "functions." + full_name;
        My_Log{My_Log::Level::kDebug} << "[Tool Response] Added 'functions.' prefix to tool name: " << full_name << std::endl;
    }

    std::string result = "<|start|>" + full_name + " to=assistant<|channel|>commentary<|message|>" + content + "<|end|>";

    My_Log{My_Log::Level::kDebug} << "[Tool Response] Built message for: " << full_name << std::endl;

    return result;
}

std::string HarmonyProcessor::ConvertJsonTypeToTS(const json& param_def)
{
    std::string json_type = param_def.value("type", "any");

    if (json_type == "string") return "string";
    if (json_type == "number" || json_type == "integer") return "number";
    if (json_type == "boolean") return "boolean";

    if (json_type == "array")
    {

        if (param_def.contains("items"))
        {
            const auto& items = param_def["items"];
            std::string item_type = ConvertJsonTypeToTS(items);
            return item_type + "[]";
        }
        return "any[]";
    }

    if (json_type == "object")
    {

        return "any";
    }

    return "any";
}

std::string HarmonyProcessor::ConvertToolsToTypeScript(const json& tools)
{
    std::ostringstream oss;
    auto append_default_comment = [&oss](const json& param_def) {
        if (param_def.contains("default")) {
            if (param_def["default"].is_string()) {
                oss << ", // default: " << param_def["default"].get<std::string>();
            } else {
                oss << ", // default: " << param_def["default"].dump();
            }
        }
    };
    oss << "# Tools\n\n## functions\n\nnamespace functions {\n\n";

    for (const auto& tool : tools) {
        if (!tool.contains("function")) continue;

        const auto& func = tool["function"];
        std::string name = func.value("name", "");
        std::string description = func.value("description", "");

        if (name.empty()) continue;

        if (!description.empty()) {
            oss << "// " << description << "\n";
        }

        if (func.contains("parameters") && func["parameters"].contains("properties") &&
            !func["parameters"]["properties"].empty()) {

            const auto& parameters = func["parameters"];
            const auto& properties = parameters["properties"];

            oss << "type " << name << " = (_: {\n";

            std::vector<std::string> required_params;
            if (parameters.contains("required") && parameters["required"].is_array()) {
                for (const auto& req : parameters["required"]) {
                    if (req.is_string()) {
                        required_params.push_back(req.get<std::string>());
                    }
                }
            }

            for (auto it = properties.begin(); it != properties.end(); ++it) {
                std::string param_name = it.key();
                const auto& param_def = it.value();

                if (param_def.contains("description")) {
                    oss << "  // " << param_def["description"].get<std::string>() << "\n";
                }

                oss << "  " << param_name;

                bool is_required = std::find(required_params.begin(), required_params.end(), param_name)
                                   != required_params.end();
                if (!is_required) {
                    oss << "?";
                }

                oss << ": ";

                if (param_def.contains("enum") && param_def["enum"].is_array()) {
                    const auto& enum_values = param_def["enum"];
                    for (size_t i = 0; i < enum_values.size(); ++i) {
                        if (i > 0) oss << " | ";
                        oss << "\"" << enum_values[i].get<std::string>() << "\"";
                    }

                    append_default_comment(param_def);
                } else {
                    oss << ConvertJsonTypeToTS(param_def);
                    append_default_comment(param_def);
                }

                oss << "\n";
            }

            oss << "}) => any;\n\n";
        } else {

            oss << "type " << name << " = () => any;\n\n";
        }
    }

    oss << "} // namespace functions";
    return oss.str();
}

std::string HarmonyProcessor::GetMessageForHistory() const
{
    std::string result;

    if (m_isToolCall) {

        for (const auto& msg : m_analysisMessages) {
            result += msg;
        }
        for (const auto& msg : m_commentaryMessages) {
            result += msg;
        }
        for (const auto& msg : m_finalMessages) {
            result += msg;
        }
    } else {

        for (const auto& msg : m_finalMessages) {
            result += msg;
        }
    }

    return result;
}
