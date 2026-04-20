//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PLATFORM_FIX_H
#define PLATFORM_FIX_H

// Fix for ARM64 Windows SDK issue with _CountOneBits64
#if defined(_M_ARM64) || defined(_M_ARM64EC)
#include <intrin.h>

#ifndef _CountOneBits64
inline unsigned int _CountOneBits64(unsigned __int64 value) {
    return (unsigned int)__popcnt64(value);
}
#endif

#endif // _M_ARM64 || _M_ARM64EC

#endif // PLATFORM_FIX_H
