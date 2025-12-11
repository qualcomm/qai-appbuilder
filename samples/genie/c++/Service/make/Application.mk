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
APP_CPPFLAGS += -std=c++17 -g -O2 -Wall -frtti -fexceptions -fvisibility=hidden -DSPILLFILL -DQUALLA_ENGINE_QNN_HTP=TRUE -DQUALLA_ENGINE_QNN_CPU=TRUE -DQUALLA_ENGINE_QNN_GPU=TRUE -DFMT_HEADER_ONLY -D QAI_APP_BUILDER_MAJOR_VERSION=2 -D QAI_APP_BUILDER_MINOR_VERSION=1 -D QAI_APP_BUILDER_PATCH_VERSION=2 -Wno-unused-functio
APP_LDFLAGS  += -lc -lm -ldl -Wl,--strip-all 
