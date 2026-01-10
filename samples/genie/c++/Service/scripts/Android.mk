#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

LOCAL_PATH := $(call my-dir)
SUPPORTED_TARGET_ABI := arm64-v8a

define all-c-files-under
$(call all-named-files-under,*.c,$(1))
endef
#============================ Define Common Variables ===============================================================
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/cpp-httplib/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/json/include/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/curl/include/curl/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/curl/include/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/curl/lib/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/CLI11/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../
PACKAGE_C_INCLUDES += -I ${QNN_SDK_ROOT}include/Genie/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/utils
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/common

#========================== Define libGenie.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE := libGenie
LOCAL_SRC_FILES := ${QNN_SDK_ROOT}lib/aarch64-android/libGenie.so
include $(PREBUILT_SHARED_LIBRARY)
# #========================== Define libcurl.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE    := libcurl
LOCAL_SRC_FILES := ../../External/curl/packages/Android/libs/arm64-v8a/libcurl.so
include $(PREBUILT_SHARED_LIBRARY)
#========================== Define Service Lib variables =============================================
include $(CLEAR_VARS)
CPPFLAGS   += -DQUALLA_MAJOR_VERSION=2 -DQUALLA_MINOR_VERSION=0 -DQUALLA_PATCH_VERSION=0
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)

LOCAL_MODULE                   := GenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libGenie
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                :=   ../src/GenieAPIService/context/context_base.cpp \
                                    ../src/GenieAPIService/context/genie.cpp \
                                    ../src/GenieAPIService/chat_request_handler/chat_request_handler.cpp \
                                    ../src/GenieAPIService/GenieAPIService.cpp \
                                    ../src/GenieAPIService/model/model_manager.cpp \
                                    ../src/GenieAPIService/utils/utils.cpp \
                                    ../src/GenieAPIService/processor/harmony.cpp \
                                    ../src/GenieAPIService/processor/general.cpp \
                                    ../src/GenieAPIService/response/response_tools.cpp \
                                    ../src/GenieAPIService/response/response_dispatcher.cpp
include $(BUILD_SHARED_LIBRARY)
#========================== Define Service Lib variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)

LOCAL_MODULE                   := JNIGenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libGenie
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                :=   ../src/GenieAPIService/context/context_base.cpp \
                                    ../src/GenieAPIService/context/genie.cpp \
                                    ../src/GenieAPIService/chat_request_handler/chat_request_handler.cpp \
                                    ../src/GenieAPIService/GenieAPIService.cpp \
                                    ../src/GenieAPIService/model/model_manager.cpp \
                                    ../src/GenieAPIService/utils/utils.cpp \
                                    ../src/GenieAPIService/processor/harmony.cpp \
                                    ../src/GenieAPIService/processor/general.cpp \
                                    ../src/GenieAPIService/response/response_tools.cpp \
                                    ../src/GenieAPIService/response/response_dispatcher.cpp
include $(BUILD_SHARED_LIBRARY)
#========================== Define client app variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_MODULE                   := GenieAPIClient
#LOCAL_SHARED_LIBRARIES 		   := ../External/curl/packages/Android/libs/arm64-v8a/libcurl.so
LOCAL_SHARED_LIBRARIES 		   := libcurl
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := ../src/GenieAPIClient.cpp
include $(BUILD_EXECUTABLE)
