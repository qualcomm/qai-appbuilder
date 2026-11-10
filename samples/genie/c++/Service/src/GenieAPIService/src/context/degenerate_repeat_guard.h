//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef DEGENERATE_REPEAT_GUARD_H
#define DEGENERATE_REPEAT_GUARD_H

#include <cstddef>
#include <cstdint>
#include <vector>

namespace degenerate_repeat_guard
{

constexpr int kMinRepeats = 3;

struct Decision
{
    bool abort = false;
    int period = 0;
    std::size_t span_bytes = 0;
};

class StreamGuard
{
public:
    StreamGuard(bool enabled, int max_period, std::size_t min_span_bytes);

    Decision Observe(int32_t id, std::size_t piece_bytes);

private:
    bool enabled_ = false;
    int max_period_ = 0;
    std::size_t min_span_bytes_ = 0;
    std::size_t observed_ = 0;
    uint64_t total_bytes_ = 0;
    std::vector<int32_t> ids_;
    std::vector<uint64_t> cumulative_bytes_;
    std::vector<std::size_t> run_;
};

}

#endif
