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

#include "../model/model_instance_config.h"
#include <mutex>
#include <functional>

class ContextBase
{
public:
    using Callback = std::function<bool(std::string &)>;

    // Prefill 阶段心跳回调：在 prefill（prompt 处理）期间定期调用，
    // 向客户端发送保活消息，防止代理因长时间无数据而超时断开。
    // 返回 true  = 连接正常，继续 prefill；
    // 返回 false = 连接已断开，应立即中止 prefill 并返回 false。
    using PrefillHeartbeatCallback = std::function<bool()>;

    explicit ContextBase(const ModelInstanceConfig &config) : model_config_{config} {};

    virtual ~ContextBase();

    virtual bool Query(const ModelInput &, const Callback &,
                       PrefillHeartbeatCallback prefill_heartbeat = nullptr) = 0;

    virtual bool Stop();

    bool SetParamsByConfig(const json &j);

    virtual int SetParams(const std::string &key, const std::string &value);

    virtual json HandleProfile() = 0;

    virtual bool SetStopSequence(const std::string &stop_sequences);

    virtual size_t TokenLength(const std::string &text);

    virtual void Reset();

    // 检查模型是否正在执行推理（query_mutex_ 是否被锁定）
    // 用于诊断安全检查与主推理之间的锁竞争
    // 注意：此方法仅用于诊断，不保证原子性（检查后状态可能改变）
    bool is_query_busy() const {
        if (query_mutex_.try_lock()) {
            query_mutex_.unlock();
            return false;
        }
        return true;
    }

    virtual void applyLora(const std::string &engineRole, const std::string &loraAdapterName);

    virtual void setLoraStrength(const std::string &engineRole,
                                 const std::unordered_map<std::string, float> &alphaValue);

    // Fix 5: returns true if the last generation was stopped because the output token
    // limit was reached (finish_reason = "length"). Base class returns false by default;
    // GenieContext overrides this with the actual flag.
    virtual bool was_stopped_by_output_limit() const { return false; }

protected:
    virtual int ApplyParams();

    const ModelInstanceConfig &model_config_;

    int max_length_{};

    // 序列化 Query() 调用的互斥锁。
    // 各后端（GGUF/llama.cpp、MNN、QNN 等）的模型上下文均不是线程安全的，
    // 同一模型实例同一时刻只能有一个 Query() 调用在执行。
    // 安全检查、复杂度评估和正常推理都通过同一个模型句柄调用 Query()，
    // 必须通过此锁保证互斥。
    //
    // 注意：每个 ContextBase 实例（即每个已加载的模型实例）拥有独立的
    // query_mutex_，不同模型实例之间的锁互不干扰，可以并发推理。
    // 模型实例的底层硬件类型（CPU/GPU/NPU）不影响此锁的独立性。
    mutable std::mutex query_mutex_;
};

#endif
