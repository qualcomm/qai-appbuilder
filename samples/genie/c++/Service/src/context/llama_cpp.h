//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_CPP_H
#define LLAMA_CPP_H

#include "core/context_base.h"

class LLAMACppBuilder : public ContextBase
{
public:
    explicit LLAMACppBuilder(const IModelConfig &info);

    ~LLAMACppBuilder() override;

    bool Query(const std::string &prompt, Callback callback) override;

    bool Stop() override;

    size_t TokenLength(const std::string &text) override;

    json HandleProfile() override;

private:
    class Impl;

    Impl *impl_;
};

#endif //LLAMA_CPP_H
