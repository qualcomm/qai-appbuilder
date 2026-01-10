//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _BUILDER_BASE_H
#define _BUILDER_BASE_H

#include "../model/model_manager.h"

class ContextBase
{
public:
    using Callback = std::function<bool(std::string &)>;

    explicit ContextBase(const IModelConfig &info) : model_config_{info}
    {};

    virtual ~ContextBase();

    virtual bool Query(const std::string &prompt, const Callback callback) = 0;

    virtual bool Stop();

    virtual bool SetParams(const std::string max_length, const std::string temp,
                           const std::string top_k, const std::string top_p);

    virtual json HandleProfile() = 0;

    virtual bool SetStopSequence(const std::string &stop_sequences);

    virtual size_t TokenLength(const std::string &text);

    virtual void applyLora(const std::string engineRole, const std::string loraAdapterName);

    virtual void setLoraStrength(const std::string engineRole,
                                 const std::unordered_map<std::string, float> &alphaValue);

protected:
    const IModelConfig& model_config_;
};

#endif