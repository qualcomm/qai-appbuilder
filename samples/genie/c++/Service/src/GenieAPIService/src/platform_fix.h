//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PLATFORM_FIX_H
#define PLATFORM_FIX_H

// Fix for Windows SDK ARM64 compatibility issue
// _CountOneBits64 is not available on ARM64 in older Windows SDK versions
#if defined(_MSC_VER) && (defined(_M_ARM64) || defined(_M_ARM64EC))
    #include <intrin.h>
    #ifndef _CountOneBits64
        // Use __popcnt64 intrinsic for ARM64
        #define _CountOneBits64 __popcnt64
    #endif
    #ifndef _CountOneBits
        #define _CountOneBits __popcnt
    #endif
#endif

#endif // PLATFORM_FIX_H
