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
APP_MODULES := JNIGenieAPIService GenieAPIService GenieAPIClient 
APP_CPPFLAGS += -std=c++2a -O3 -Wall -frtti -fexceptions -fvisibility=hidden -DSPILLFILL -DQUALLA_ENGINE_QNN_HTP=TRUE -DQUALLA_ENGINE_QNN_CPU=TRUE -DQUALLA_ENGINE_QNN_GPU=TRUE -DFMT_HEADER_ONLY -Wno-unused-function
APP_LDFLAGS  += -lc -lm -ldl -Wl,--strip-all
