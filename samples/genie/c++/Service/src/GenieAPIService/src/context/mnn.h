//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _MNNBUILDER_H
#define _MNNBUILDER_H

#include "context_base.h"

class MNNContext : public ContextBase
{
public:
    explicit MNNContext(const ModelInstanceConfig &config);

    ~MNNContext() override;

    bool Query(const ModelInput &, const Callback& callback,
               PrefillHeartbeatCallback prefill_heartbeat = nullptr) override;

    bool Stop() override;

    json HandleProfile() override;

    // Fix: 实现 TokenLength，使 Fix 1 的 available_output 计算对 MNN 后端使用真实 token 数
    // 基类默认实现返回 text.size()（字符数），对中文等多字节字符不准确
    size_t TokenLength(const std::string &text) override;

    // Fix: 报告是否因输出 token 限制而停止生成
    bool was_stopped_by_output_limit() const override { return stopped_by_output_limit_; }

    // 加载前内存预检查：估算模型目录下权重文件所需内存，与系统当前可用物理内存比较。
    // 用于在 MNNContext 构造（进而触发 Llm::createLLM/load()）之前拦截明显会导致
    // 原生硬崩溃（OOM）的超大模型加载请求。
    static uint64_t EstimateMnnMemoryRequirement(const std::string &model_path);

    static uint64_t GetAvailablePhysicalMemoryBytes();

private:
    class Impl;
    Impl* impl_;
    bool m_stop = false;
    bool stopped_by_output_limit_ = false;  // Fix: 是否因输出 token 限制而停止
};

#endif
