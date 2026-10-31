//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "context_base.h"
#include "log.h"
#include <algorithm>
#include <chrono>
#include <thread>

void ContextBase::MarkDestroying()
{
    destroying_.store(true);
    stop_epoch_.fetch_add(1);
}

bool ContextBase::WaitForIdle(int timeout_ms)
{
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (stop_in_flight_.load() > 0 || pending_queries_.load() > 0)
    {
        if (std::chrono::steady_clock::now() >= deadline)
        {
            My_Log{My_Log::Level::kWarning}
                    << "[Context] WaitForIdle timed out after " << timeout_ms
                    << "ms: stop_in_flight=" << stop_in_flight_.load()
                    << ", pending_queries=" << pending_queries_.load() << std::endl;
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return true;
}

bool ContextBase::Stop()
{
    My_Log("BuilderBase::Stop called\n");
    return true;
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

bool ContextBase::SetParamsByConfig(const json &j)
{
    std::lock_guard<std::mutex> params_lock(params_mutex_);
    std::lock_guard<std::mutex> query_lock(query_mutex_);
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

        std::string alt_key = key;
        std::replace(alt_key.begin(), alt_key.end(), '-', '_');
        const std::string &lookup_key = j.contains(key) ? key : alt_key;
        if (!j.contains(lookup_key))
        {
            continue;
        }

        const auto &j_val = j[lookup_key];
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

        if (key == "size")
        {
            int max_length = std::stoi(value);
            if (max_length)
            {
                max_length_ = max_length;
            }
            goto ahead;
        }

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
        My_Log{} << "set param successfully: "
                 << "name: " << key << ", "
                 << "value: " << value << "\n";
    }

    for (const char *name : {"temperature", "temp", "top_k", "top-k", "top_p", "top-p", "min_p", "min-p", "seed"})
    {
        std::string alt_name = name;
        std::replace(alt_name.begin(), alt_name.end(), '-', '_');
        if (j.contains(name) && !sample_config.contains(name) && !sample_config.contains(alt_name))
        {
            My_Log{My_Log::Level::kWarning} << "request sampling param ignored (not in model sampler config): "
                                            << name << "=" << j[name].dump() << "\n";
        }
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
    My_Log("the context is being destroyed\n");
}

void ContextBase::Reset()
{
    My_Log("BuilderBase::Reset called\n");
}
