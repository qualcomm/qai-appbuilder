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

    enum class MatchState {
        NORMAL,
        MATCHING_START,
        IN_TOOL_CALL,
        MATCHING_END,
        TOOL_CALL_DONE
    };

    MatchState match_state_;
    std::string match_buffer_;
    size_t match_pos_;

    void resetMatchState();
    std::tuple<bool, std::string> processNormalState(const std::string &chunkText, std::string &toolResponse);
    std::tuple<bool, std::string> processMatchingState(const std::string &chunkText, std::string &toolResponse);
};

#endif
