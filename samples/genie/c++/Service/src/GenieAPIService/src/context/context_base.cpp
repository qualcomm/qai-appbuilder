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

bool ContextBase::SetParamsByConfig(const json &j)
{
    if (j.empty())
    {
        return true;
    }

    bool is_configured = false;
    int status;
    auto &sample_config = model_config_.sampler();
    for (auto it = sample_config.begin(); it != sample_config.end(); ++it)
    {
        const std::string &key = it.key();
        if (!j.contains(key))
        {
            continue;
        }

        const auto &j_val = j[key];
        auto &c_val = it.value();
        if (j_val == c_val)
        {
            continue;
        }

        c_val = j_val;
        std::string value;
        if (j_val.is_string())
        {
            value = j_val.get_ref<const std::string &>();
        }
        else if (j_val.is_number())
        {
            value = j_val.dump();
        }
        else
        {
            My_Log{My_Log::Level::kError} << "param is invalid : "
                                          << "name: " << key << ", "
                                          << "value: " << value << " ";
            continue;
        }

//        if (key == "size")
//        {
//            int max_length = std::stoi(value);
//            if (max_length)
//            {
//                max_length_ = max_length;
//            }
//            goto ahead;
//        }

        if ((status = SetParams(key, value)))
        {
            My_Log{} << "set param failed: "
                     << "name: " << key << ", "
                     << "value: " << value << ", "
                     << "status: " << status << "\n";
            continue;
        }
        else
        {
            is_configured = true;
        }

        ahead:
        My_Log{} << "set param sueccessfully: "
                 << "name: " << key << ", "
                 << "value: " << value << "\n";
    }

    if (!is_configured)
    {
        return true;
    }

    if ((status = ApplyParams()))
    {
        My_Log{My_Log::Level::kError} << "apply sampler config failed: "
                                      << "status: " << status << "\n";
        return false;
    }
    else
    {
        return true;
    }
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

void ContextBase::applyLora(const std::string &engineRole, const std::string &loraAdapterName)
{
    My_Log("BuilderBase::applyLora called\n");
}

void ContextBase::setLoraStrength(const std::string &engineRole,
                                  const std::unordered_map<std::string, float> &alphaValue)
{
    My_Log("BuilderBase::setLoraStrength called\n");
}

ContextBase::~ContextBase()
{
    My_Log("the context is be destroyed\n");
}

int ContextBase::SetParams(const std::string &key, const std::string &value)
{
    My_Log("BuilderBase::SetParams called\n");
    return 0;
}

int ContextBase::ApplyParams()
{
    My_Log("BuilderBase::ApplyParams called\n");
    return 0;
}

