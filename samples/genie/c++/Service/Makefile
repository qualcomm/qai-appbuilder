#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# specify compiler
export CXX := clang++-14
export PATH := $(ANDROID_NDK_ROOT)/toolchains/llvm/prebuilt/linux-x86_64/bin:$(PATH)

.PHONY: all android
.DEFAULT: android

all: android

android:
	@echo "-------------------- Building GenieAPIServer for android -------------------- "
	@$(ANDROID_NDK_ROOT)/ndk-build.cmd APP_ALLOW_MISSING_DEPS=true APP_ABI="arm64-v8a" NDK_PROJECT_PATH=./ NDK_APPLICATION_MK=make/Application.mk APP_BUILD_SCRIPT=make/Android.mk || (echo "-------------------- server android build failed --------------------"; exit 1; )
	@echo "-------------------- GenieAPIServer for android build succeeded -------------------- "
