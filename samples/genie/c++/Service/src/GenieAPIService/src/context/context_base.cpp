//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "context_base.h"
#include "log.h"

bool ContextBase::Stop()
{
    My_Log("BuilderBase::Stop called\n");
    return true;
}

bool ContextBase::SetParams(const std::string max_length, const std::string temp, const std::string top_k,
                            const std::string top_p)
{
    My_Log("BuilderBase::SetParams called\n");
    return true;
}

bool ContextBase::SetStopSequence(const std::string &stop_sequences)
{
    My_Log("BuilderBase::SetStopSequence called\n");
    return true;
}

size_t ContextBase::TokenLength(const std::string &text)
{
    return text.size();
}

void ContextBase::applyLora(const std::string engineRole, const std::string loraAdapterName)
{
    My_Log("BuilderBase::applyLora called\n");
}

void ContextBase::setLoraStrength(const std::string engineRole,
                                  const std::unordered_map<std::string, float> &alphaValue)
{
    My_Log("BuilderBase::setLoraStrength called\n");
}

ContextBase::~ContextBase()
{
    My_Log("the context is be destroyed\n");
}
