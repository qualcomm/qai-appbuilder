#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# define package name
PACKAGE_NAME := AppBuilder

# define library prerequisites list
sample_app := src
make_dir := make
EXE_SOURCES = $(sample_app)

# specify compiler
export CXX := clang++-14

.PHONY: android

android:
	@echo "-------------------- Building QAI AppBuilder for android -------------------- "
	$(ANDROID_NDK_ROOT)/ndk-build.cmd APP_ALLOW_MISSING_DEPS=true APP_ABI="arm64-v8a" NDK_PROJECT_PATH=./ NDK_APPLICATION_MK=$(make_dir)/Application.mk APP_BUILD_SCRIPT=$(make_dir)/Android.mk
	@echo "-------------------- QAI AppBuilder for android build succeeded -------------------- "
