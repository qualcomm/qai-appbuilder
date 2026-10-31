//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef THINK_BUDGET_FLOOR_H
#define THINK_BUDGET_FLOOR_H

#include <cstdint>

namespace think_budget_floor
{

constexpr int64_t kForcedMinFloorTokens = 96;

int32_t ApplyThinkBudgetFloor(int64_t budget_before_ctx_clamp, int64_t ctx_left,
                               int64_t think_min_tokens, bool force_min_floor);

}

#endif
