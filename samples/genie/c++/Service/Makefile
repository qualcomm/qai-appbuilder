#=============================================================================
#
#  Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
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
	@echo "-------------------- GenieAPIServer android build succeeded -------------------- "

check_ndk:
ifeq ($(ANDROID_NDK_ROOT),)
	$(error ERROR: ANDROID_NDK_ROOT not set, skipping compilation for Android platform(s).)
endif

