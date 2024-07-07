//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#define DEBUG_ON 0

#if DEBUG_ON
#define DEBUG_MSG(...)            \
  {                               \
    fprintf(stderr, __VA_ARGS__); \
    fprintf(stderr, "\n");        \
  }
#else
#define DEBUG_MSG(...)
#endif
