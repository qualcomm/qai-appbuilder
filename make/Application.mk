#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

APP_ABI      := arm64-v8a
APP_STL      := c++_static
APP_PLATFORM := android-21
APP_CPPFLAGS += -std=c++17 -O3 -fexceptions -Wall -Werror -fvisibility=hidden -DDLL_EXPORTS -DLIBAPPBUILDER_API="__attribute__((visibility(\"default\")))"
APP_LDFLAGS  += -lc -lm -ldl
MakefileAPP_STL := c++_shared

