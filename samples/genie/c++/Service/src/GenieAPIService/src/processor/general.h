//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GENERAL_H
#define GENERAL_H

#include "processor.h"
#include <string>

class GeneralProcessor : public ModelProcessor
{
public:
    GeneralProcessor();

    std::tuple<bool, std::string>
    preprocessStream(std::string &chunkText, bool isToolResponse, std::string &toolResponse) override;

    void Clean() final;

private:
    struct Utils;

    // State machine for detecting <tool_call> tag
    enum class MatchState {
        NORMAL,           // Normal text output
        MATCHING_START,   // Matching "<tool_call>"
        IN_TOOL_CALL,     // Inside <tool_call>...</tool_call>
        MATCHING_END,     // Matching "</tool_call>"
        TOOL_CALL_DONE    // Tool call completed, ignore subsequent content
    };

    MatchState match_state_;
    std::string match_buffer_;  // Buffer for partial tag matching
    size_t match_pos_;          // Current position in tag matching

    // Helper methods
    void resetMatchState();
    std::tuple<bool, std::string> processNormalState(const std::string &chunkText, std::string &toolResponse);
    std::tuple<bool, std::string> processMatchingState(const std::string &chunkText, std::string &toolResponse);
};

#endif //GENERAL_H
