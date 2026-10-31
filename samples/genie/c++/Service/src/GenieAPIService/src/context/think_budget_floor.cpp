//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "think_budget_floor.h"

#include <algorithm>

namespace think_budget_floor
{

int32_t ApplyThinkBudgetFloor(int64_t budget_before_ctx_clamp, int64_t ctx_left,
                               int64_t think_min_tokens, bool force_min_floor)
{
    int64_t budget = std::max<int64_t>(0, std::min(budget_before_ctx_clamp, ctx_left));
    if (force_min_floor)
    {
        const int64_t floor_budget = std::min<int64_t>(think_min_tokens, kForcedMinFloorTokens);
        if (floor_budget > budget)
        {
            budget = floor_budget;
        }
    }
    return static_cast<int32_t>(budget);
}

}
