#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

LOCAL_PATH := $(call my-dir)
SUPPORTED_TARGET_ABI := arm64-v8a

#============================ Define Common Variables ===============================================================
# Include paths
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/cpp-httplib
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/json/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/fmt/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/..
PACKAGE_C_INCLUDES += -I ${QNN_SDK_ROOT}include/Genie


#========================== Define libGenie.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE := libGenie
LOCAL_SRC_FILES := ${QNN_SDK_ROOT}lib/aarch64-android/libGenie.so
include $(PREBUILT_SHARED_LIBRARY)


#========================== Define Service app variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)

LOCAL_MODULE                   := GenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libGenie
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := ../GenieAPIService.cpp \
                                  ../GenieBuilder.cpp
include $(BUILD_EXECUTABLE)

#========================== Define Service Lib variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)

LOCAL_MODULE                   := JNIGenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libGenie
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := ../GenieAPIService.cpp \
                                  ../GenieBuilder.cpp
include $(BUILD_SHARED_LIBRARY)

#========================== Define client app variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)

LOCAL_MODULE                   := GenieAPIClient
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := ../GenieAPIClient.cpp

include $(BUILD_EXECUTABLE)
