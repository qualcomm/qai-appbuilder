//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "general.h"

#include <nlohmann/json.hpp>
#include "utils.h"
#include "log.h"

using json = nlohmann::ordered_json;

struct GeneralProcessor::Utils
{
    static inline const std::string START_TAG = "<tool_call>";
    static inline const std::string END_TAG = "</tool_call>";
};

GeneralProcessor::GeneralProcessor()
        : match_state_(MatchState::NORMAL),
          match_pos_(0)
{
}

void GeneralProcessor::Clean()
{
    resetMatchState();
}

void GeneralProcessor::resetMatchState()
{
    match_state_ = MatchState::NORMAL;
    match_buffer_.clear();
    match_pos_ = 0;
}

std::tuple<bool, std::string> GeneralProcessor::preprocessStream(std::string &chunkText,
                                                                 bool /* isToolResponse */,
                                                                 std::string &toolResponse)
{
    bool currentIsToolResponse = false;
    std::string outputChunk;

    // [新增] 检测残缺的 <tool 标签，以及裸 JSON 工具调用（无 <tool_call> 标签）
    if (match_state_ == MatchState::NORMAL) {
        // ── 情况1：以 "<tool" 开头但不是完整的 "<tool_call>" 前缀 ──
        // 使用 substr 比较而非 find，避免误匹配 "<tool_call_something>" 等变体：
        //   - "<tool_call>" 开头：正常标签，不需要修复
        //   - "<tool_call" 开头（但后面不是 ">"）：可能是截断的 "<tool_call>"，尝试修复
        //   - "<tool_xxx>" 开头：非标准标签，尝试修复
        bool starts_with_tool = (chunkText.size() >= 5 && chunkText.compare(0, 5, "<tool") == 0);
        bool starts_with_tool_call = (chunkText.size() >= Utils::START_TAG.size() &&
                                      chunkText.compare(0, Utils::START_TAG.size(), Utils::START_TAG) == 0);
        if (starts_with_tool && !starts_with_tool_call) {
            My_Log{My_Log::Level::kWarning}
                << "[Format Parser] Detected incomplete <tool tag: " << chunkText.substr(0, std::min(chunkText.size(), size_t(15))) << "..." << std::endl;
            
            // 尝试修复：如果后续有 JSON '{'，补全为 <tool_call>
            size_t json_start = chunkText.find('{');
            if (json_start != std::string::npos) {
                std::string fixed_chunk = Utils::START_TAG + chunkText.substr(json_start);
                My_Log{} << "[Format Parser] Auto-fixed to: <tool_call>..." << std::endl;
                chunkText = fixed_chunk;
            }
        }
        // 注：原"情况2"（per-chunk 裸 JSON 检测：要求单个 chunk 同时以 '{' 开头且含 "name"）
        // 已删除——token 逐个到达时 '{' 与 "name" 几乎不会落在同一个 chunk 里，实际上是
        // 死代码，且一旦误触发会在半条消息上做不完整的标签包装。裸 JSON（无 <tool_call>
        // 标签）检测已改为生成结束后对完整可见文本做一次 ResponseTools::DetectBareToolCall()
        // （response_dispatcher.cpp finalize_generation_result，即"Layer -1"），配合流式
        // hold-back 机制防止畸形内容提前泄漏给客户端，见 response_tools.md。
    }

    for (ptrdiff_t i = 0; i < static_cast<ptrdiff_t>(chunkText.size()); ++i)
    {
        char ch = chunkText[i];

        switch (match_state_)
        {
            case MatchState::NORMAL:
            {
                // Check if we're starting to match "<tool_call>"
                if (ch == Utils::START_TAG[0])
                {
                    match_state_ = MatchState::MATCHING_START;
                    match_buffer_ = ch;
                    match_pos_ = 1;
                }
                else
                {
                    outputChunk += ch;
                }
                break;
            }

            case MatchState::MATCHING_START:
            {
                if (ch == Utils::START_TAG[match_pos_])
                {
                    match_buffer_ += ch;
                    match_pos_++;

                    if (match_pos_ == Utils::START_TAG.size())
                    {
                        // Successfully matched "<tool_call>"
                        match_state_ = MatchState::IN_TOOL_CALL;
                        toolResponse = match_buffer_;  // Start collecting tool call content
                        match_buffer_.clear();
                        match_pos_ = 0;
                        currentIsToolResponse = true;
                    }
                }
                else
                {
                    // Mismatch - this was not a tool call tag.
                    //
                    // 修复：不直接输出整个 match_buffer_，而是只输出第一个字符 '<'，
                    // 然后将 match_buffer_[1..] + ch 重新插入 chunkText 当前位置，
                    // 让外层 for 循环从 NORMAL 状态重新处理这些字符。
                    //
                    // 背景：当 token 边界恰好落在 '<tool_call>' 内部时（例如
                    // chunk1="正文<tool"，chunk2="_call>..."），chunk2 第一个字符
                    // 触发 mismatch，旧逻辑会把整个 match_buffer_（如 "<tool"）直接
                    // 输出，导致 '<tool' 等前缀泄漏到客户端 UI。
                    //
                    // 新逻辑：
                    //   1. 输出 match_buffer_[0]（即 '<'）
                    //      ── '<' 确认不是 <tool_call> 的完整开头（因为后续字符不匹配），
                    //         但它可能是普通文本中的 '<'，应该正常输出。
                    //   2. 将 match_buffer_[1..] + ch 插回 chunkText[i] 处，回退循环索引
                    //   3. 外层 for 循环从 NORMAL 状态重新处理 match_buffer_[1..] + ch
                    //      ── 这些字符可能包含新的 '<tool_call>' 开头，需要重新匹配
                    //
                    // 关键：match_buffer_[1..] 是 START_TAG 的子串（如 "tool"），
                    // 不包含 '<'，不会触发新的 MATCHING_START，直接输出为普通文本。
                    // 只有 ch 可能是 '<'，会触发新的 MATCHING_START 继续尝试匹配。
                    //
                    // 这样可以正确处理任意 token 分割场景，且不会产生无限循环。

                    // 步骤1：输出 match_buffer_[0]（即 '<'）
                    outputChunk += match_buffer_[0];

                    // 步骤2：将 match_buffer_[1..] + ch 插回 chunkText[i] 处，
                    //        重置状态机为 NORMAL，回退循环索引让外层循环重新处理
                    std::string reinject = match_buffer_.substr(1) + ch;
                    chunkText.replace(i, 1, reinject);  // 替换 chunkText[i] 为 reinject
                    // 回退 i，使外层 for 循环从 reinject[0] 开始重新处理
                    // （for 循环末尾会执行 ++i，所以这里设为 i-1）
                    i = i - 1;

                    match_state_ = MatchState::NORMAL;
                    match_buffer_.clear();
                    match_pos_ = 0;
                }
                break;
            }

            case MatchState::IN_TOOL_CALL:
            {
                toolResponse += ch;
                currentIsToolResponse = true;

                // Check if we're starting to match "</tool_call>"
                if (ch == Utils::END_TAG[0])
                {
                    match_state_ = MatchState::MATCHING_END;
                    match_buffer_ = ch;
                    match_pos_ = 1;
                }
                break;
            }

            case MatchState::MATCHING_END:
            {
                currentIsToolResponse = true;

                if (ch == Utils::END_TAG[match_pos_])
                {
                    toolResponse += ch;
                    match_buffer_ += ch;
                    match_pos_++;

                    if (match_pos_ == Utils::END_TAG.size())
                    {
                        // Successfully matched "</tool_call>"
                        // Switch to TOOL_CALL_DONE state to ignore subsequent content
                        match_state_ = MatchState::TOOL_CALL_DONE;
                        match_buffer_.clear();
                        match_pos_ = 0;

                        // Keep currentIsToolResponse = true to signal completion
                    }
                }
                else
                {
                    // Mismatch - add buffered content and current char to toolResponse
                    // Continue collecting tool call content
                    toolResponse += ch;
                    match_state_ = MatchState::IN_TOOL_CALL;
                    match_buffer_.clear();
                    match_pos_ = 0;

                    // Re-check current character in case it starts end tag
                    if (ch == Utils::END_TAG[0])
                    {
                        match_state_ = MatchState::MATCHING_END;
                        match_buffer_ = ch;
                        match_pos_ = 1;
                    }
                }
                break;
            }

            case MatchState::TOOL_CALL_DONE:
            {
                currentIsToolResponse = true;
                break;
            }
        }
    }

    // Handle partial matches at end of chunk
    if (match_state_ == MatchState::MATCHING_START)
    {
        // We're in the middle of matching "<tool_call>" but chunk ended
        // Keep the buffer for next chunk, don't output yet
        // Return empty output to avoid premature display
    }
    else if (match_state_ == MatchState::MATCHING_END)
    {
        // We're in the middle of matching "</tool_call>" but chunk ended
        // This is still part of tool call, keep currentIsToolResponse = true
        currentIsToolResponse = true;
    }
    else if (match_state_ == MatchState::IN_TOOL_CALL)
    {
        currentIsToolResponse = true;
    }
    else if (match_state_ == MatchState::TOOL_CALL_DONE)
    {
        currentIsToolResponse = true;
    }

    return std::make_tuple(currentIsToolResponse, outputChunk);
}
