//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "general.h"

#include <nlohmann/json.hpp>
#include "core/utils.h"

using json = nlohmann::ordered_json;

struct GeneralProcessor::Utils
{
    static inline const std::string FN_NAME = "<tool_call>";
    static inline const char FN_FLAG = '<';
};

std::tuple<bool, std::string> GeneralProcessor::preprocessStream(std::string &chunkText,
                                                                 bool isToolResponse,
                                                                 std::string &toolResponse)
{
    bool currentIsToolResponse = isToolResponse;
    std::string keepChunk;

    if (currentIsToolResponse)
    {
        toolResponse += chunkText;
    }
    else if (str_contains(chunkText, std::string{Utils::FN_FLAG}))
    {
        std::string result;
        size_t pos = chunkText.find(Utils::FN_FLAG);
        if (pos != std::string::npos)
        {
            result = chunkText.substr(pos);
            keepChunk = chunkText.substr(0, pos);
        }

        currentIsToolResponse = true;
        toolResponse += result;
    }

    if (!str_contains(toolResponse, Utils::FN_NAME))
    {
        currentIsToolResponse = false;
        keepChunk = toolResponse;  // Since it's not tools call, add the content in toolResponse buffer to keepChunk and print it.
        toolResponse.clear();
    }

    return std::make_tuple(currentIsToolResponse, keepChunk);
}