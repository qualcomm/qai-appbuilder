//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "degenerate_repeat_guard.h"

#include <algorithm>
#include <cstddef>

namespace degenerate_repeat_guard
{

int FindRepeatedPeriod(const std::vector<int32_t> &ids, int max_period)
{
    if (max_period <= 0 || ids.size() < 2)
    {
        return 0;
    }

    const int upper_bound = std::min<int>(max_period, static_cast<int>(ids.size() / 2));
    const auto end = ids.end();
    for (int period = 1; period <= upper_bound; ++period)
    {
        const auto first = end - period * 2;
        const auto middle = end - period;
        if (std::equal(first, middle, middle, end))
        {
            return period;
        }
    }
    return 0;
}

StreamGuard::StreamGuard(bool enabled, int max_period)
    : enabled_(enabled && max_period > 0), max_period_(max_period > 0 ? max_period : 0)
{
}

void StreamGuard::ResetHold()
{
    holding_ = false;
    period_ = 0;
    held_ = 0;
}

Decision StreamGuard::Observe(int32_t id, bool terminal)
{
    if (!enabled_)
    {
        return Decision{Action::kEmit, 0};
    }

    ids_.push_back(id);

    if (!holding_)
    {
        const int period = FindRepeatedPeriod(ids_, max_period_);
        if (period > 0)
        {
            holding_ = true;
            period_ = period;
            held_ = 0;
        }
        return Decision{Action::kEmit, 0};
    }

    ++held_;
    if (terminal)
    {
        ResetHold();
        return Decision{Action::kFlush, 0};
    }
    if (held_ < period_)
    {
        return Decision{Action::kHold, 0};
    }

    // 进入持有态时尾部两块已逐 token 相同（FindRepeatedPeriod 的语义），此刻又攒满了第三块，
    // 故 ids_.size() >= 3 * period_，下面两个下标不会下溢。
    const std::size_t pending_start = ids_.size() - static_cast<std::size_t>(period_);
    const std::size_t previous_start = pending_start - static_cast<std::size_t>(period_);
    const bool repeated = std::equal(ids_.begin() + static_cast<std::ptrdiff_t>(previous_start),
                                     ids_.begin() + static_cast<std::ptrdiff_t>(pending_start),
                                     ids_.begin() + static_cast<std::ptrdiff_t>(pending_start),
                                     ids_.end());
    const int detected = period_;
    ResetHold();
    return repeated ? Decision{Action::kAbort, detected} : Decision{Action::kFlush, 0};
}

}
