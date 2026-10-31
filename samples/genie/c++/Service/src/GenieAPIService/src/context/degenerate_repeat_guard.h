//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef DEGENERATE_REPEAT_GUARD_H
#define DEGENERATE_REPEAT_GUARD_H

#include <cstdint>
#include <vector>

namespace degenerate_repeat_guard
{

int FindRepeatedPeriod(const std::vector<int32_t> &ids, int max_period);

// 观测一个 token 之后调用方应采取的动作。
enum class Action
{
    kEmit,   // 立即放行本 token（此时持有队列必然为空）
    kHold,   // 把本 token 压入持有队列，先不输出，等攒满一个周期再比对
    kFlush,  // 把本 token 压入持有队列后整队放行（第三块与前一块不同，不是退化循环）
    kAbort,  // 第三块与前一块逐 token 相同：确认退化循环，丢弃整队并停止生成
};

struct Decision
{
    Action action = Action::kEmit;
    int period = 0;  // 仅 kAbort 有意义：确认的重复周期
};

// 生成期退化复读守卫的状态机本体。刻意只依赖 token id，不依赖 llama 句柄/KV 位置。
class StreamGuard
{
public:
    StreamGuard(bool enabled, int max_period);

    // terminal 表示「本 token 之后生成必定结束」（EOG，或确实设了正数 n_predict 且已用尽）。
    // 为真时持有队列必须整队放行，不能再等第三块，否则这些已采样的 token 会被静默丢弃。
    Decision Observe(int32_t id, bool terminal);

    bool holding() const { return holding_; }
    int period() const { return period_; }
    int held() const { return held_; }

private:
    void ResetHold();

    bool enabled_ = false;
    int max_period_ = 0;
    std::vector<int32_t> ids_;
    bool holding_ = false;
    int period_ = 0;
    int held_ = 0;
};

}

#endif
