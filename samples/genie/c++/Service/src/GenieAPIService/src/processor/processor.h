//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_PROCESSING_H
#define MODEL_PROCESSING_H

#include <string>
class ModelProcessor
{
public:
    virtual ~ModelProcessor() = default;

    virtual void Clean() = 0;

    virtual std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                           bool isToolResponse,
                                                           std::string &toolResponse) = 0;

    std::string start_tag_;
};

#endif //MODEL_PROCESSING_H
