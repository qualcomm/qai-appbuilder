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

class GeneralProcessor : public ModelProcessor
{
public:
    std::tuple<bool, std::string>
    preprocessStream(std::string &chunkText, bool isToolResponse, std::string &toolResponse) override;

    void Clean() final
    {};
private:
    struct Utils;
};

#endif //GENERAL_H
