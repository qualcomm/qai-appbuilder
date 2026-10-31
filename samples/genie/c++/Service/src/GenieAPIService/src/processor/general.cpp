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

    if (match_state_ == MatchState::NORMAL) {

        bool starts_with_tool = (chunkText.size() >= 5 && chunkText.compare(0, 5, "<tool") == 0);
        bool starts_with_tool_call = (chunkText.size() >= Utils::START_TAG.size() &&
                                      chunkText.compare(0, Utils::START_TAG.size(), Utils::START_TAG) == 0);
        if (starts_with_tool && !starts_with_tool_call) {
            My_Log{My_Log::Level::kWarning}
                << "[Format Parser] Detected incomplete <tool tag: " << chunkText.substr(0, std::min(chunkText.size(), size_t(15))) << "..." << std::endl;

            size_t json_start = chunkText.find('{');
            if (json_start != std::string::npos) {
                std::string fixed_chunk = Utils::START_TAG + chunkText.substr(json_start);
                My_Log{} << "[Format Parser] Auto-fixed to: <tool_call>..." << std::endl;
                chunkText = fixed_chunk;
            }
        }

    }

    for (ptrdiff_t i = 0; i < static_cast<ptrdiff_t>(chunkText.size()); ++i)
    {
        char ch = chunkText[i];

        switch (match_state_)
        {
            case MatchState::NORMAL:
            {

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

                        match_state_ = MatchState::IN_TOOL_CALL;
                        toolResponse = match_buffer_;
                        match_buffer_.clear();
                        match_pos_ = 0;
                        currentIsToolResponse = true;
                    }
                }
                else
                {

                    outputChunk += match_buffer_[0];

                    std::string reinject = match_buffer_.substr(1) + ch;
                    chunkText.replace(i, 1, reinject);

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

                        match_state_ = MatchState::TOOL_CALL_DONE;
                        match_buffer_.clear();
                        match_pos_ = 0;

                    }
                }
                else
                {

                    toolResponse += ch;
                    match_state_ = MatchState::IN_TOOL_CALL;
                    match_buffer_.clear();
                    match_pos_ = 0;

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

    if (match_state_ == MatchState::MATCHING_START)
    {

    }
    else if (match_state_ == MatchState::MATCHING_END)
    {

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
