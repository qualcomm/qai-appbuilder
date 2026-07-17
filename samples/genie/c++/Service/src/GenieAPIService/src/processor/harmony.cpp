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

// 前言检测函数声明（在使用前声明）
static bool IsPreamble(const std::string& content);

// 查找安全的 UTF-8 字符边界位置
//
// 功能：确保字符串截断不会发生在多字节字符中间，避免产生无效的 UTF-8 序列
//
// 参数：
//   str - 要检查的字符串
//   pos - 期望的截断位置（substr 的 length 参数，表示"截取到位置 pos 之前"）
//
// 返回：调整后的安全截断位置（保证在完整的 UTF-8 字符边界上）
//
// UTF-8 编码规则：
//   - 单字节字符（ASCII）：  0xxxxxxx (< 0x80)
//   - 多字节字符首字节：      11xxxxxx (>= 0xC0)
//   - 多字节字符后续字节：    10xxxxxx (0x80-0xBF)
//
// 处理逻辑：
//   1. 检查 pos-1 位置的字节（最后一个要包含的字节）
//   2. 如果是单字节字符，直接返回 pos
//   3. 如果是多字节字符首字节，检查字符是否完整；不完整则截断到该字符之前
//   4. 如果是后续字节，向前查找首字节，确保包含完整字符或截断到字符之前
static size_t FindSafeUtf8Boundary(const std::string& str, size_t pos) {
    if (pos == 0) {
        return 0;
    }
    
    // 限制 pos 的最大值
    if (pos > str.length()) {
        pos = str.length();
    }
    
    // 检查位置 pos-1（最后一个要包含的字节）
    size_t checkPos = pos - 1;
    unsigned char c = static_cast<unsigned char>(str[checkPos]);
    
    // 如果是单字节字符（0xxxxxxx），当前位置安全
    if (c < 0x80) {
        return pos;
    }
    
    // 如果是多字节字符的首字节（11xxxxxx），需要检查字符是否完整
    if (c >= 0xC0) {
        // 计算这个字符需要多少个字节
        int bytesNeeded;
        if (c < 0xE0) bytesNeeded = 2;      // 110xxxxx
        else if (c < 0xF0) bytesNeeded = 3; // 1110xxxx
        else bytesNeeded = 4;                // 11110xxx
        
        // 检查是否有足够的字节
        if (checkPos + bytesNeeded <= str.length()) {
            // 字符完整，可以包含
            return pos;
        } else {
            // 字符不完整，截断到这个字符之前
            return checkPos;
        }
    }
    
    // 如果是后续字节（10xxxxxx），向前查找到首字节
    while (checkPos > 0) {
        checkPos--;
        c = static_cast<unsigned char>(str[checkPos]);
        
        // 找到单字节字符，截断到它之后
        if (c < 0x80) {
            return checkPos + 1;
        }
        
        // 找到多字节字符的首字节
        if (c >= 0xC0) {
            // 计算这个字符需要多少个字节
            int bytesNeeded;
            if (c < 0xE0) bytesNeeded = 2;
            else if (c < 0xF0) bytesNeeded = 3;
            else bytesNeeded = 4;
            
            // 检查从 checkPos 到 pos-1 是否包含完整的字符
            if (checkPos + bytesNeeded <= pos) {
                // 字符完整，可以截断到 pos
                return pos;
            } else {
                // 字符不完整，截断到这个字符之前
                return checkPos;
            }
        }
    }
    
    // 如果一直回退到开头都没找到首字节（异常情况），返回 0
    return 0;
}

// 全局计数器用于生成唯一的工具调用 ID
static std::atomic<uint64_t> g_tool_call_id_counter{0};

// 生成唯一的工具调用 ID
// 格式: call_<function_name>_<timestamp_us>_<counter>_<random_hex>
// 使用函数名 + 微秒时间戳 + 递增计数器 + 随机数确保唯一性，线程安全
// 改进 ID 生成，避免高并发冲突，并支持从 ID 提取函数名
static std::string GenerateToolCallId(const std::string& function_name = "") {
    // 线程安全的随机数生成器
    static thread_local std::mt19937_64 gen(std::random_device{}());
    static thread_local std::uniform_int_distribution<uint64_t> dis;
    
    uint64_t id = g_tool_call_id_counter.fetch_add(1, std::memory_order_relaxed);
    auto now = std::chrono::system_clock::now();
    auto timestamp = std::chrono::duration_cast<std::chrono::microseconds>(  // 使用微秒
        now.time_since_epoch()
    ).count();
    uint64_t random_part = dis(gen);
    
    std::ostringstream oss;
    oss << "call_";
    
    // 如果提供了函数名，将其包含在 ID 中（便于后续提取）
    if (!function_name.empty()) {
        oss << function_name << "_";
    }
    
    oss << timestamp << "_" << id << "_" << std::hex << random_part;
    return oss.str();
}

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
        m_isFinal = m_isCommentary = m_isFunctions = m_isAnalysis = false;
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

std::tuple<bool, std::string> HarmonyProcessor::preprocessStream(std::string &chunkText,
                                                                 bool isToolResponse,
                                                                 std::string &toolResponse)
{
    // 不要清空 m_finalText，因为它需要累积流式输出
    
    // 记录处理前的长度，用于计算新增内容
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
    
    // 只返回新增的 final 通道内容给客户端
    // analysis 通道内容不应展示给用户
    std::string newContent;
    if (m_finalText.length() > prevLength) {
        newContent = m_finalText.substr(prevLength);
    }
    
    // 根据 Harmony 规范（openai-harmony.md line 288）：
    // "The model has not been trained to the same safety standards in the
    //  chain-of-thought as it has for final output. You should not show the
    //  chain-of-thought to your users, as they might contain harmful content."
    chunkText = newContent;

    // 如果检测到工具调用，返回工具调用信息
    if (m_isToolCall)
    {
        return std::make_tuple(true, m_toolCallContent);
    }

    return std::make_tuple(false, "");
}

size_t HarmonyProcessor::findTag(const std::string &tag)
{
    if (buffer.empty()) return std::string::npos;

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

    // 流式输出完整性检查 - 更新最后接收时间
    auto now = std::chrono::steady_clock::now();
    
    // 检查是否超时（只在有未完成消息时检查）
    if (currentState != State::INIT &&
        lastChunkTime_.time_since_epoch().count() > 0) {
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            now - lastChunkTime_
        ).count();
        
        if (elapsed > STREAM_TIMEOUT_SECONDS) {
            My_Log{My_Log::Level::kDebug}
                << "Stream timeout detected (" << elapsed << "s), forcing message completion" << std::endl;
            
            // 强制完成当前消息
            if (currentState == State::IN_MESSAGE && !currentMessage.empty()) {
                My_Log{My_Log::Level::kDebug}
                    << "Incomplete message: " << currentMessage << std::endl;
                
                // 根据当前通道类型处理
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
            
            // 重置状态
            ResetState();
        }
    }
    
    // 更新最后接收时间
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
                static const std::string constrainTag = "<|constrain|>";
                
                size_t msgPos = findTag(messageTag);
                size_t constrainPos = findTag(constrainTag);
                
                // 检查是否先遇到 <|constrain|> 标记
                if (constrainPos != std::string::npos &&
                    (msgPos == std::string::npos || constrainPos < msgPos))
                {
                    // 提取通道字符串（可能包含 to= 参数）
                    std::string header = buffer.substr(0, constrainPos);
                    currentChannelStr += header;
                    
                    // 解析 to= 参数
                    currentToParam = parseToParam(currentChannelStr);
                    
                    // 确定通道类型
                    currentChannel = Impl::determineChannelType(currentChannelStr);
                    
                    // 调试日志：输出解析结果
                    My_Log{My_Log::Level::kDebug} << "[Channel Parsed] channelStr: " << currentChannelStr
                             << ", toParam: " << currentToParam
                             << ", channelType: " << static_cast<int>(currentChannel) << std::endl;
                    
                    // 进入 CONSTRAIN 状态
                    pendingBuffer = pendingBuffer.substr(constrainPos + constrainTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_CONSTRAIN;
                    currentConstrain.clear();
                }
                else if (msgPos != std::string::npos)
                {
                    // 提取通道字符串（可能包含 to= 参数）
                    std::string header = buffer.substr(0, msgPos);
                    currentChannelStr += header;
                    
                    // 解析 to= 参数
                    currentToParam = parseToParam(currentChannelStr);
                    
                    // 确定通道类型
                    currentChannel = Impl::determineChannelType(currentChannelStr);
                    
                    // 调试日志：输出解析结果
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
                    // 确保保留足够长度以识别 messageTag 和 constrainTag
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
                    // 提取 constrain 类型（如 "json"）
                    currentConstrain = buffer.substr(0, msgPos);
                    
                    pendingBuffer = pendingBuffer.substr(msgPos + messageTag.length());
                    buffer = pendingBuffer;
                    currentState = State::IN_MESSAGE;
                    currentMessage.clear();
                    outputtedLength = 0;
                }
                else
                {
                    // 确保保留足够长度以识别 messageTag
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
                            // 消息完成时，处理剩余内容并构建工具调用
                            if (currentMessage.length() > outputtedLength)
                            {
                                impl_->commentaryCallback(currentMessage.substr(outputtedLength));
                                outputtedLength = currentMessage.length();
                            }
                            
                            // 检查是否是工具调用
                            if (!currentToParam.empty() && currentToParam.find("functions.") == 0)
                            {
                                std::string funcName = currentToParam.substr(10); // 去掉 "functions."

                                My_Log{My_Log::Level::kDebug} << "[Message Complete] Processing tool call for function: " << funcName << std::endl;
                                // 使用 currentMessage（当前消息内容，即纯 JSON 参数），而非 m_commentaryText
                                // m_commentaryText 累积了所有 commentary 内容（包括前言文本），会导致 JSON 解析失败
                                My_Log{My_Log::Level::kDebug} << "[Message Complete] currentMessage: " << currentMessage << std::endl;

                                m_isToolCall = true;

                                // 构建工具调用信息（包装成 <tool_call> 格式）
                                json tool_call_json;
                                tool_call_json["name"] = funcName;

                                // 使用 currentMessage 解析工具调用参数
                                // currentMessage 只包含当前消息的内容（即工具调用的 JSON 参数）
                                // 而 m_commentaryText 可能包含前言文本 + JSON，导致解析失败
                                // 先修复反斜杠（智能模式：保留已有转义序列，转义未转义的反斜杠）
                                // 背景：模型生成的路径（如 C:\Users\...\nexaai-paddleocr）中，
                                // \n 是反斜杠 + n（两个字符），但 json::parse 会将 \n 解析为换行符（0x0A），
                                // 导致路径中的 n 丢失（变成换行符）。
                                // fixBackslashes(smart_mode=true) 会将未转义的 \n 转义为 \\n，
                                // 防止 json::parse 误解析。
                                std::string fixed_msg = ResponseTools::fixBackslashes(currentMessage, true);
                                // 再进行 JSON 修复（尾随逗号、Python 字面量等），再尝试解析
                                std::string repaired_msg = ResponseTools::repairJson(fixed_msg);
                                try {
                                    json args = json::parse(repaired_msg);
                                    // 兼容模型混用格式：to=functions.exec 时消息体本应是纯参数，
                                    // 但模型有时仍生成 {"name":"exec","arguments":{...}} 包装格式。
                                    // 检测到包装格式时，提取内层 arguments 作为真正的参数，
                                    // 避免双重嵌套导致 {"name":"exec","arguments":{"name":"exec","arguments":{...}}}。
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
                                    // 如果解析失败，直接使用字符串（convertToolCallJson 会进一步处理）
                                    tool_call_json["arguments"] = currentMessage;
                                    My_Log{My_Log::Level::kDebug}
                                        << "[Message Complete] Failed to parse arguments as JSON: " << e.what() << std::endl;
                                }

                                // 包装成 <tool_call> 格式
                                m_toolCallContent = "<tool_call>" + tool_call_json.dump() + "</tool_call>";
                                m_toolCallFunctionName = funcName;

                                My_Log{My_Log::Level::kDebug} << "[Tool Call] Function: " << funcName << std::endl;
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] Arguments: " << currentMessage << std::endl;
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] Formatted content: " << m_toolCallContent << std::endl;
                            }
                            // 非标准格式：to=functions（无具体函数名），JSON 消息体为 {"name":"xxx","arguments":{...}}
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
                                        My_Log{My_Log::Level::kWarning} << "[Message Complete] Non-standard tool call: JSON body has no 'name' field, ignoring." << std::endl;
                                    }
                                } catch (const std::exception& e) {
                                    My_Log{My_Log::Level::kWarning} << "[Message Complete] Non-standard tool call: failed to parse JSON body: " << e.what() << std::endl;
                                }
                            }
                            break;
                        case ChannelType::FUNCTIONS:
                            impl_->functionsCallback(currentMessage);
                            break;
                        default:
                            break;
                    }
                    
                    // 统一停止标记处理
                    // 构建用于历史存储的完整消息
                    // 根据 Harmony 规范（openai-harmony.md line 211-217）：
                    // "`<|return|>` is a decode-time stop token only. When you add the assistant's 
                    //  generated reply to conversation history for the next turn, replace the trailing 
                    //  `<|return|>` with `<|end|>` so that stored messages are fully formed as 
                    //  `<|start|>{header}<|message|>{content}<|end|>`."
                    //
                    // 关键规则：
                    // 1. <|return|> 和 <|call|> 是解码时的停止标记（decode-time stop tokens）
                    // 2. 历史存储时必须统一替换为 <|end|>，确保消息格式完整
                    // 3. 标准格式：<|start|>{header}<|message|>{content}<|end|>
                    //
                    // 实现：无论模型输出使用的是 <|return|> 还是 <|call|>，
                    // 历史存储时统一使用 <|end|> 标记
                    std::string historyMessage = "<|start|>assistant";
                    
                    // 添加通道信息（如果有 to= 参数也包含）
                    if (!currentChannelStr.empty()) {
                        historyMessage += "<|channel|>" + currentChannelStr;
                    }
                    
                    // 添加 constrain 信息（如果有）
                    // 根据 Harmony 规范，格式为：<|constrain|>json（无空格）
                    if (!currentConstrain.empty()) {
                        historyMessage += " <|constrain|>" + currentConstrain;
                    }
                    
                    historyMessage += "<|message|>" + currentMessage;
                    
                    // 历史存储统一使用 <|end|>
                    // 无论当前 endMarker 是 <|return|> 还是 <|call|>，都替换为 <|end|>
                    historyMessage += "<|end|>";
                    
                    // 根据通道类型分别存储（用于 CoT 管理）
                    if (currentChannel == ChannelType::ANALYSIS) {
                        m_analysisMessages.push_back(historyMessage);
                    } else if (currentChannel == ChannelType::FINAL) {
                        m_finalMessages.push_back(historyMessage);
                    } else if (currentChannel == ChannelType::COMMENTARY) {
                        m_commentaryMessages.push_back(historyMessage);
                    }
                    
                    // 保存到完整消息缓冲区（向后兼容）
                    m_completeMessage += historyMessage;

                    pendingBuffer = pendingBuffer.substr(endPos + endMarker.length());
                    buffer = pendingBuffer;

                    // 不在这里调用 resetState()，而是手动重置必要的状态
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
                        // ✅ UTF-8 乱码修复：确保在字符边界上截断
                        // 步骤1：确保结束位置在 UTF-8 边界上
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
                                // 添加 COMMENTARY 通道的流式输出处理
                                // 这对于工具调用非常重要，因为工具调用通过 commentary 通道传递
                                impl_->commentaryCallback(newOutput);
                            }
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
    m_finalText.clear();
    m_completeMessage.clear();
    m_internalAnalysisBuffer.clear();
    m_commentaryText.clear();
    m_isToolCall = false;
    m_toolCallContent.clear();
    m_toolCallFunctionName.clear();
    
    // 清空消息列表（CoT 管理）
    m_analysisMessages.clear();
    m_finalMessages.clear();
    m_commentaryMessages.clear();
    
    ResetState();
}

void HarmonyProcessor::FinalizeToolCall()
{
    // 强制完成工具调用处理（用于流结束时）
    // 检查是否有未完成的工具调用
    //
    // 背景：当模型以 EOG token 结束生成（而非显式的 <|call|>/<|end|> 文本标记）时，
    // processChunk() 的状态机停留在 IN_MESSAGE 状态，currentMessage 从未被赋值，
    // m_commentaryText 也可能因 pendingBuffer 末尾保留机制而不完整或为空。
    // 此时需要直接从 pendingBuffer（outputtedLength 之后的部分）提取工具调用内容。
    
    // 记录调用时的完整状态，便于问题定位（受 debug.log_inference_stream 控制）
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

    // ── 修复1：早返回保护 ──────────────────────────────────────────────────────
    // 当 processChunk() 已在流式处理中正确完成工具调用（遇到 <|call|>/<|end|> 标记时），
    // m_isToolCall=true 且 m_toolCallContent 已被正确设置。
    // 此时不应再执行后续逻辑，否则会用 m_commentaryText（可能包含前言文本+JSON
    // 的混合内容）覆盖已经正确的 m_toolCallContent，导致 JSON 解析失败。
    //
    // 典型场景：模型先输出一条前言 commentary，再输出带 to=functions.xxx 的工具调用
    // commentary。processChunk() 处理完工具调用消息后正确设置了 m_toolCallContent，
    // 但 m_commentaryText 此时已累积了"前言文本 + JSON"，FinalizeToolCall() 若继续
    // 执行情况3，会用这段混合内容覆盖正确结果。
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
    // ─────────────────────────────────────────────────────────────────────────

    std::string funcName;
    bool isToolCall = false;
    bool isNonStandardFormat = false; // to=functions 格式，需从 JSON 消息体提取函数名
    std::string detectedFromMessage; // 情况3：保存检测到工具调用的原始消息，用于提取纯内容

    // 情况1：currentToParam 有值（状态机正在 IN_MESSAGE 中处理，EOG 提前终止）
    // 这是 EOG 场景的主要路径：模型生成完 JSON 后直接以 EOG 结束，
    // 没有输出 <|call|> 或 <|end|> 文本标记，状态机停留在 IN_MESSAGE 状态
    if (!currentToParam.empty() && currentToParam.find("functions.") == 0)
    {
        funcName = currentToParam.substr(10); // 去掉 "functions."
        isToolCall = true;
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected tool call from currentToParam: " << funcName << std::endl;
        }
    }
    // 情况1b：非标准格式 to=functions（无具体函数名），函数名在 JSON 消息体中
    else if (!currentToParam.empty() && currentToParam == "functions")
    {
        isToolCall = true;
        isNonStandardFormat = true;
        if (ResponseTools::log_inference_stream)
        {
            My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected non-standard tool call from currentToParam=functions, will extract name from JSON body" << std::endl;
        }
    }
    // 情况2：检查当前通道和消息内容
    else if (currentChannel == ChannelType::COMMENTARY && !currentMessage.empty())
    {
        // 检查 currentChannelStr 是否包含 to=functions.xxx
        size_t toPos = currentChannelStr.find("to=functions.");
        if (toPos != std::string::npos)
        {
            size_t start = toPos + 13; // "to=functions." 的长度
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
        // 情况2b：非标准格式 to=functions（无具体函数名）
        else
        {
            size_t toPos2 = currentChannelStr.find("to=functions");
            if (toPos2 != std::string::npos)
            {
                // 确认后面紧跟空格或结尾（不是 to=functions.xxx）
                size_t afterPos = toPos2 + 12; // "to=functions" 长度
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
    // 情况3：检查已累积的 commentary 内容
    else if (!m_commentaryText.empty())
    {
        // 检查 m_commentaryMessages 中最后一条消息
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
                detectedFromMessage = lastMsg; // 保存原始消息，用于后续提取纯 JSON 内容
                if (ResponseTools::log_inference_stream)
                {
                    My_Log{My_Log::Level::kInfo} << "[FinalizeToolCall] Detected tool call from m_commentaryMessages: " << funcName << std::endl;
                }
            }
            // 情况3b：非标准格式 to=functions（无具体函数名）
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
        // 确定工具调用内容，按优先级依次尝试三个来源：
        //
        // 来源1：currentMessage
        //   正常路径：processChunk() 找到结束标记时赋值（harmony.cpp:510）
        //   EOG 路径：状态机停留在 IN_MESSAGE 时此值为空
        //
        // 来源2：m_commentaryText
        //   通过 commentaryCallback 流式累积，但 pendingBuffer 末尾保留机制
        //   可能导致最后几个字节未被 flush，内容不完整甚至为空
        //
        // 来源3：pendingBuffer（EOG 兜底）
        //   EOG 提前终止时，pendingBuffer 中保存了状态机尚未处理完的原始内容
        //   从 outputtedLength 开始取（跳过已通过 commentaryCallback 输出的部分）
        //   这是修复 EOG 场景工具调用丢失问题的关键路径
        std::string toolContent;
        std::string toolContentSource;

        if (!currentMessage.empty())
        {
            toolContent = currentMessage;
            toolContentSource = "currentMessage";
        }
        else if (!detectedFromMessage.empty())
        {
            // ── 修复2：从 m_commentaryMessages 的原始消息中提取纯 JSON 内容 ──────
            // 背景：m_commentaryText 会同时累积前言文本和工具调用 JSON（两个分支都追加），
            // 当模型先输出前言再输出工具调用时，m_commentaryText = 前言 + JSON，
            // 直接用作工具调用参数会导致 JSON 解析失败。
            // 正确做法：从 m_commentaryMessages 中找到带 to=functions.xxx 的消息，
            // 提取其 <|message|>...<|end|> 之间的纯内容（即工具调用的 JSON 参数）。
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
                // 提取失败，降级使用 m_commentaryText（可能包含前言，但总比没有强）
                toolContent = m_commentaryText;
                toolContentSource = "m_commentaryText(fallback, message extraction failed)";
                My_Log{My_Log::Level::kWarning}
                    << "[FinalizeToolCall] Failed to extract content from detectedFromMessage, "
                    << "falling back to m_commentaryText. "
                    << "detectedFromMessage='" << detectedFromMessage.substr(0, 80) << "'" << std::endl;
            }
            // ─────────────────────────────────────────────────────────────────
        }
        else if (!m_commentaryText.empty())
        {
            toolContent = m_commentaryText;
            toolContentSource = "m_commentaryText";
        }
        else if (!pendingBuffer.empty())
        {
            // EOG 提前终止时，pendingBuffer 中有未处理的内容
            // 从 outputtedLength 开始取（跳过已输出的部分）
            toolContent = pendingBuffer.length() > outputtedLength
                          ? pendingBuffer.substr(outputtedLength)
                          : pendingBuffer;
            toolContentSource = "pendingBuffer(EOG fallback, offset=" + std::to_string(outputtedLength) + ")";
        }

        // 记录内容来源和内容预览（受 log_inference_stream 控制）
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

        // 非标准格式：to=functions，从 JSON 消息体中提取函数名和参数
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
                    My_Log{My_Log::Level::kWarning} << "[FinalizeToolCall] Non-standard tool call: JSON body has no 'name' field, ignoring." << std::endl;
                }
            } catch (const std::exception& e) {
                My_Log{My_Log::Level::kWarning} << "[FinalizeToolCall] Non-standard tool call: failed to parse JSON body: " << e.what() << std::endl;
            }
            return;
        }

        m_isToolCall = true;

        // 构建工具调用信息（包装成 <tool_call> 格式）
        json tool_call_json;
        tool_call_json["name"] = funcName;

        // 先修复反斜杠（智能模式：保留已有转义序列，转义未转义的反斜杠）
        // 背景：模型生成的路径（如 C:\Users\...\nexaai-paddleocr）中，
        // \n 是反斜杠 + n（两个字符），但 json::parse 会将 \n 解析为换行符（0x0A），
        // 导致路径中的 n 丢失（变成换行符）。
        // fixBackslashes(smart_mode=true) 会将未转义的 \n 转义为 \\n，
        // 防止 json::parse 误解析。
        std::string fixed_content = ResponseTools::fixBackslashes(toolContent, true);
        // 再进行 JSON 修复（尾随逗号、Python 字面量等），再尝试解析
        std::string repaired_content = ResponseTools::repairJson(fixed_content);
        try {
            json args = json::parse(repaired_content);
            // 兼容模型混用格式：to=functions.xxx 时消息体本应是纯参数，
            // 但模型有时仍生成 {"name":"xxx","arguments":{...}} 包装格式。
            // 检测到包装格式时，提取内层 arguments，避免双重嵌套。
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
            // 如果解析失败，直接使用字符串（convertToolCallJson 会进一步处理）
            tool_call_json["arguments"] = toolContent;
            My_Log{My_Log::Level::kWarning}
                << "[FinalizeToolCall] Failed to parse arguments as JSON: " << e.what()
                << " (source=" << toolContentSource << ")" << std::endl;
            My_Log{My_Log::Level::kWarning}
                << "[FinalizeToolCall] Raw content: '" << toolContent << "'" << std::endl;
        }
        
        // 包装成 <tool_call> 格式
        m_toolCallContent = "<tool_call>" + tool_call_json.dump() + "</tool_call>";
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
    // 强制 flush final 通道残留内容（用于 EOG/流结束时）
    //
    // 背景：
    // Harmony 格式中，<|return|> 是 final 通道的结束标记，同时也是 EOG token（特殊 token）。
    // 当 params_.special=false（默认值）时，common_token_to_piece() 对特殊 token 返回 ""，
    // 导致 callback 收到空字符串后走 heartbeat 分支，processChunk() 从未被调用，
    // 状态机停留在 IN_MESSAGE 状态，pendingBuffer 中积累的最后一段 final 内容无法被 flush。
    //
    // 即使方案A（params_.special=true）已修复根本原因，此方法作为防御性兜底，
    // 确保在任何情况下 final 通道内容都能完整输出。
    
    // [诊断] 记录调用时的状态，便于问题定位（受 debug.log_inference_stream 控制）
    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo}
            << "[FinalizeFinalChannel] Called: state=" << static_cast<int>(currentState)
            << ", channel=" << static_cast<int>(currentChannel)
            << ", pendingBuffer.length=" << pendingBuffer.length()
            << ", outputtedLength=" << outputtedLength
            << ", m_finalText.length=" << m_finalText.length() << std::endl;
    }
    
    // 只处理 final 通道的残留内容
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
    
    // 检查是否有未输出的内容
    // pendingBuffer 是最后一次 processChunk 调用后的缓冲区
    // outputtedLength 是相对于 pendingBuffer 的已输出偏移量
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
    
    // 获取未输出的内容（从 outputtedLength 开始）
    std::string unoutputed = remaining.substr(outputtedLength);
    
    // 安全检查：移除可能残留在末尾的 <|return|>/<|end|>/<|call|> 前缀片段
    // 背景：processChunk 的 else 分支（部分消息处理）会保留末尾可能是标签前缀的内容，
    // 防止将不完整的标签输出给客户端。在 EOG 时，这些末尾前缀永远不会被完整标签替代，
    // 应该直接输出（因为它们实际上就是普通内容，不是真正的标签）。
    //
    // 注意：只检查末尾，不检查中间位置。
    // 原因：processChunk 已经将安全的中间内容输出（outputtedLength 已更新），
    // 只有末尾的"可能前缀"才是被保留的。如果检查中间位置，会错误截断正常内容。
    static const std::vector<std::string> stop_tags = {"<|return|>", "<|end|>", "<|call|>"};
    size_t safeLength = unoutputed.length();
    for (const auto& tag : stop_tags)
    {
        // 检查 tag 的所有前缀（从 "<|" 开始，长度 >= 2）
        // 只检查字符串末尾是否以此前缀结尾
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
    
    // 确保截断在 UTF-8 字符边界上
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
    
    // 记录 flush 前的 m_finalText 长度，用于计算新增内容
    size_t prevFinalLength = m_finalText.length();
    
    // 通过 finalCallback 将内容写入 m_finalText
    impl_->finalCallback(toFlush);
    outputtedLength += safeLength;
    
    // 返回新增的 final 内容
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
    // ========== 强化 Analysis 通道内容隔离 ==========
    // 根据 Harmony 规范（openai-harmony.md line 288）：
    // "The model has not been trained to the same safety standards in the
    //  chain-of-thought as it has for final output. You should not show the
    //  chain-of-thought to your users, as they might contain harmful content."
    //
    // 实施策略：
    // 1. Analysis 内容只保存到内部缓冲区（m_internalAnalysisBuffer）
    // 2. 绝对不添加到 m_finalText（客户端输出缓冲区）
    // 3. 仅用于内部日志和 CoT 管理
    // 4. 添加生产环境的运行时验证
    // =====================================================
    
    if (!m_isAnalysis)
    {
        m_isAnalysis = true;
        My_Log{My_Log::Level::kDebug} << "[Analysis Channel - Internal Only - Not for Client]" << std::endl;
    }

    // ✅ 第一道防线：只保存到内部缓冲区
    parent_->m_internalAnalysisBuffer += content;

    // ❌ 严格禁止：绝对不要将 analysis 内容添加到 m_finalText
    // parent_->m_finalText += content;  // 禁止！这会导致安全问题

    // ========== 生产环境安全检查 ==========
    // 运行时验证：确保 analysis 内容没有泄露到客户端输出
    // 这是一个关键的安全检查，即使在生产环境也应该保留
    if (!parent_->m_finalText.empty())
    {
        // 检查 m_finalText 是否意外包含了 analysis 内容
        // 这不应该发生，如果发生了说明有 bug
        if (parent_->m_finalText.find(content) != std::string::npos)
        {
            My_Log{My_Log::Level::kError}
                << "[SECURITY] CRITICAL: Analysis content leaked to client output!" << std::endl;
            My_Log{My_Log::Level::kError}
                << "[SECURITY] This is a security violation. Analysis content contains unsafe material." << std::endl;
            My_Log{My_Log::Level::kError}
                << "[SECURITY] Leaked content length: " << content.length() << " bytes" << std::endl;
            
            // 紧急修复：从 m_finalText 中移除泄露的内容
            size_t pos = parent_->m_finalText.find(content);
            parent_->m_finalText.erase(pos, content.length());
            
            My_Log{My_Log::Level::kError}
                << "[SECURITY] Emergency fix applied: removed leaked content from output" << std::endl;
            My_Log{My_Log::Level::kError}
                << "[SECURITY] Please report this bug to the development team!" << std::endl;
        }
    }

    // 调试日志（仅在 DEBUG 模式下）
    My_Log{My_Log::Level::kDebug} << "[Analysis Content] " << content << std::endl;
}

void HarmonyProcessor::Impl::handleFinal(const std::string &content)
{
    if (!m_isFinal)
    {
        m_isFinal = true;
    }
    // 直接输出 final 通道的内容，不添加额外格式
    parent_->m_finalText += content;
}

void HarmonyProcessor::Impl::handleCommentary(const std::string &content)
{
    // Commentary 通道可能包含：
    // 1. 函数调用前言（preamble）- 应该展示给用户
    // 2. 函数调用内容（如果有 to=functions.xxx）- 不展示，转换为工具调用
    
    if (!parent_->currentToParam.empty() &&
        parent_->currentToParam.find("functions.") == 0)
    {
        // 这是一个函数调用，不展示给用户
        // 流式输出时，累积内容而不是每次都重新设置
        
        if (!m_isCommentary)
        {
            m_isCommentary = true;
        }
        
        // 累积工具调用内容
        parent_->m_commentaryText += content;
        
        // 注意：不在这里设置 m_isToolCall 和构建工具调用信息
        // 这些应该在消息完成时（遇到结束标记）或查询结束时才处理
    }
    else
    {
        // 这是前言或其他 commentary 内容
        if (!m_isCommentary)
        {
            m_isCommentary = true;
        }
        parent_->m_commentaryText += content;
        
        // 只有前言才展示给用户
        // 检测是否是前言（包含特定关键词）
        if (IsPreamble(content)) {
            parent_->m_finalText += content;
        }
    }
}

// 改进的前言检测逻辑
// 前言（Preamble）是模型在调用多个工具前生成的说明性消息
// 根据 Harmony 规范（openai-harmony.md line 440-453），前言通过 commentary 通道告知用户
static bool IsPreamble(const std::string& content) {
    // 1. 首先检查是否是 JSON 格式（工具调用参数）
    // 如果是 JSON，肯定不是前言
    if (!content.empty() && (content[0] == '{' || content[0] == '[')) {
        try {
            // 修复 C4834 警告：使用返回值
            auto parsed = json::parse(content);
            (void)parsed;  // 明确表示我们不使用这个值
            return false;  // 是有效的 JSON，不是前言
        } catch (...) {
            // 不是有效的 JSON，继续检查
        }
    }
    
    // 2. 检查前言关键词（精确的列表）
    // 这些关键词通常出现在前言中
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
        "---",  // 分隔符
        "Planning to",
        "Going to",
        "About to"
    };
    
    for (const auto& keyword : preamble_keywords) {
        if (content.find(keyword) != std::string::npos) {
            return true;
        }
    }
    
    // 3. 检查是否包含 Markdown 格式的编号列表
    // 前言经常使用编号列表描述步骤
    std::regex numbered_list_regex(R"(^\s*\d+\.\s)");
    if (std::regex_search(content, numbered_list_regex)) {
        return true;
    }
    
    // 4. 检查多行编号列表（更宽松的匹配）
    // 例如："1. xxx\n2. xxx\n3. xxx"
    std::regex multiline_list_regex(R"(\d+\.\s[^\n]+\n\s*\d+\.\s)");
    if (std::regex_search(content, multiline_list_regex)) {
        return true;
    }
    
    // 移除过于宽松的 Markdown 强调检测
    // 原因：仅凭 ** 或 __ 就判断为前言过于宽松，可能误判普通 Markdown 文本
    // 已移除该检测逻辑
    
    // 5. 默认不展示（安全优先原则）
    // 根据 Harmony 规范，analysis 通道内容未经严格安全训练
    // commentary 通道也应该谨慎处理，只展示明确的前言
    return false;
}

void HarmonyProcessor::Impl::handleFunctions(const std::string &content)
{
    // 解析函数调用内容
    // 格式：{"name": "function_name", "arguments": {...}}
    
    try
    {
        json funcCall = json::parse(content);
        
        if (funcCall.contains("name") && funcCall.contains("arguments"))
        {
            std::string funcName = funcCall["name"];
            json funcArgs = funcCall["arguments"];
            
            // 标记为工具调用
            parent_->m_isToolCall = true;
            
            // 构建工具调用信息（用于返回给客户端）
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
    // 优先检查是否是工具调用（to=functions.xxx）
    // 根据 Harmony 规范（openai-harmony.md line 374）：
    // "The recipient might be defined in the role or channel section of the header."
    // 所有工具调用都必须通过 commentary 通道处理，即使模型错误地输出了 analysis 通道
    //
    // 修复问题：当模型输出 "analysis to=functions.exec" 时，
    // 应该将其识别为工具调用（COMMENTARY），而不是 ANALYSIS
    if (channelStr.find("to=functions.") != std::string::npos)
    {
        // 这是一个工具调用，无论通道名称是什么，都应该按 COMMENTARY 处理
        return ChannelType::COMMENTARY;
    }
    
    // 检查是否包含 functions. 前缀（直接的函数调用格式）
    if (channelStr.find("functions.") != std::string::npos)
    {
        return ChannelType::FUNCTIONS;
    }
    
    // 然后检查标准通道名称
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

// ========== Harmony 格式构建功能实现 ==========

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
    
    // 如果有工具定义，添加工具部分
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
    // 历史消息统一使用 <|end|>
    // 根据 Harmony 规范（openai-harmony.md line 211-217）：
    // - <|return|> 和 <|call|> 是解码时的停止标记
    // - 历史存储时必须统一替换为 <|end|>
    // - 标准格式：<|start|>{header}<|message|>{content}<|end|>
    std::string msg = "<|start|>assistant<|channel|>" + channel + "<|message|>" + content;
    msg += "<|end|>";  // 历史消息统一使用 <|end|>
    return msg;
}

std::string HarmonyProcessor::BuildToolMessage(
    const std::string& tool_name,
    const std::string& content)
{
    // 完善工具响应格式构建
    // 1. 确保工具名包含 functions. 前缀
    std::string full_name = tool_name;
    if (full_name.find("functions.") != 0) {
        full_name = "functions." + full_name;
        My_Log{My_Log::Level::kDebug} << "[Tool Response] Added 'functions.' prefix to tool name: " << full_name << std::endl;
    }
    
    // 2. 根据 Harmony 规范（openai-harmony.md line 378-380）构建格式：
    // <|start|>functions.xxx to=assistant<|channel|>commentary<|message|>...<|end|>
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
        // 检查是否有 items 定义
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
        // 对于对象类型，返回 any（除非需要展开定义）
        return "any";
    }
    
    return "any";
}

std::string HarmonyProcessor::ConvertToolsToTypeScript(const json& tools)
{
    std::ostringstream oss;
    oss << "# Tools\n\n## functions\n\nnamespace functions {\n\n";
    
    for (const auto& tool : tools) {
        if (!tool.contains("function")) continue;
        
        const auto& func = tool["function"];
        std::string name = func.value("name", "");
        std::string description = func.value("description", "");
        
        if (name.empty()) continue;
        
        // 添加函数描述
        if (!description.empty()) {
            oss << "// " << description << "\n";
        }
        
        // 检查是否有参数
        if (func.contains("parameters") && func["parameters"].contains("properties") &&
            !func["parameters"]["properties"].empty()) {
            
            const auto& parameters = func["parameters"];
            const auto& properties = parameters["properties"];
            
            oss << "type " << name << " = (_: {\n";
            
            // 获取必需参数列表
            std::vector<std::string> required_params;
            if (parameters.contains("required") && parameters["required"].is_array()) {
                for (const auto& req : parameters["required"]) {
                    if (req.is_string()) {
                        required_params.push_back(req.get<std::string>());
                    }
                }
            }
            
            // 遍历所有参数
            for (auto it = properties.begin(); it != properties.end(); ++it) {
                std::string param_name = it.key();
                const auto& param_def = it.value();
                
                // 添加参数描述
                if (param_def.contains("description")) {
                    oss << "  // " << param_def["description"].get<std::string>() << "\n";
                }
                
                // 参数名
                oss << "  " << param_name;
                
                // 检查是否是必需参数
                bool is_required = std::find(required_params.begin(), required_params.end(), param_name)
                                   != required_params.end();
                if (!is_required) {
                    oss << "?";
                }
                
                oss << ": ";
                
                // 处理枚举类型
                if (param_def.contains("enum") && param_def["enum"].is_array()) {
                    const auto& enum_values = param_def["enum"];
                    for (size_t i = 0; i < enum_values.size(); ++i) {
                        if (i > 0) oss << " | ";
                        oss << "\"" << enum_values[i].get<std::string>() << "\"";
                    }
                    
                    // 添加 default 注释
                    if (param_def.contains("default")) {
                        if (param_def["default"].is_string()) {
                            oss << ", // default: " << param_def["default"].get<std::string>();
                        } else {
                            oss << ", // default: " << param_def["default"].dump();
                        }
                    }
                } else {
                    oss << ConvertJsonTypeToTS(param_def);
                    
                    // 添加 default 注释
                    if (param_def.contains("default")) {
                        if (param_def["default"].is_string()) {
                            oss << ", // default: " << param_def["default"].get<std::string>();
                        } else {
                            oss << ", // default: " << param_def["default"].dump();
                        }
                    }
                }
                
                oss << "\n";
            }
            
            oss << "}) => any;\n\n";
        } else {
            // 无参数函数
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
        // 工具调用：保留所有通道内容（analysis + commentary + final）
        // 按顺序拼接：analysis -> commentary -> final
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
        // 一般对话：只保留 final 通道内容
        // 丢弃 analysis 和 commentary 通道（CoT 管理规则）
        for (const auto& msg : m_finalMessages) {
            result += msg;
        }
    }
    
    return result;
}

