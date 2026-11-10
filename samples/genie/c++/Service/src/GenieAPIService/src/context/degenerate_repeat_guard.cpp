//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "degenerate_repeat_guard.h"

#include <algorithm>

namespace degenerate_repeat_guard
{

StreamGuard::StreamGuard(bool enabled, int max_period, std::size_t min_span_bytes)
    : enabled_(enabled && max_period > 0 && min_span_bytes > 0),
      max_period_(enabled_ ? max_period : 0),
      min_span_bytes_(min_span_bytes)
{
    if (enabled_)
    {
        const std::size_t ring = static_cast<std::size_t>(max_period_) + 1;
        ids_.assign(ring, 0);
        cumulative_bytes_.assign(ring, 0);
        run_.assign(ring, 0);
    }
}

Decision StreamGuard::Observe(int32_t id, std::size_t piece_bytes)
{
    Decision decision;
    if (!enabled_)
    {
        return decision;
    }

    const std::size_t ring = ids_.size();
    const std::size_t index = observed_;
    const std::size_t slot = index % ring;

    for (std::size_t period = 1; period <= static_cast<std::size_t>(max_period_); ++period)
    {
        const bool same = index >= period && ids_[(index - period) % ring] == id;
        run_[period] = same ? run_[period] + 1 : 0;
    }

    total_bytes_ += piece_bytes;
    ids_[slot] = id;
    cumulative_bytes_[slot] = total_bytes_;
    ++observed_;

    for (std::size_t period = 1; period <= static_cast<std::size_t>(max_period_); ++period)
    {
        const std::size_t repeats = (run_[period] + period) / period;
        if (repeats < static_cast<std::size_t>(kMinRepeats))
        {
            continue;
        }
        const uint64_t before_unit = index >= period ? cumulative_bytes_[(index - period) % ring] : 0;
        const uint64_t unit_bytes = total_bytes_ - before_unit;
        const uint64_t span_bytes = unit_bytes * repeats;
        const uint64_t required = period == 1 ? std::max<std::size_t>(1, min_span_bytes_ / 2) : min_span_bytes_;
        if (span_bytes >= required)
        {
            decision.abort = true;
            decision.period = static_cast<int>(period);
            decision.span_bytes = static_cast<std::size_t>(span_bytes);
            return decision;
        }
    }
    return decision;
}

}
