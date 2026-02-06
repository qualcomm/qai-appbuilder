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
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/cli11/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/stb
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/../../../../src
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../
PACKAGE_C_INCLUDES += -I ${QNN_SDK_ROOT}include/Genie/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/common

#========================== Define libGenie.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE := libGenie
LOCAL_SRC_FILES := ${QNN_SDK_ROOT}lib/aarch64-android/libGenie.so
include $(PREBUILT_SHARED_LIBRARY)
#========================== Define libappbuilder.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE := libappbuilder
LOCAL_SRC_FILES := ../libappbuilder.so
include $(PREBUILT_SHARED_LIBRARY)
# #========================== Define libcurl.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE    := libcurl
LOCAL_SRC_FILES := ../libcurl.so
include $(PREBUILT_SHARED_LIBRARY)
#========================== Define Service Lib variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_MODULE                   := GenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libappbuilder libGenie
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                :=   ../src/GenieAPIService/src/chat_history/chat_history.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/chat_request_handler.cpp \
									../src/GenieAPIService/src/context/context_base.cpp \
                                    ../src/GenieAPIService/src/context/qnn/genie.cpp \
                                    ../src/GenieAPIService/src/context/qnn/genie_interface.cpp \
                                    ../src/GenieAPIService/src/context/qnn/phi4mm/phi4mm.cpp \
                                    ../src/GenieAPIService/src/context/qnn/qwen2_5/qwen_2_5.cpp \
                                    ../src/GenieAPIService/src/context/qnn/qwen2_5_omini/qwen_2_5_omini.cpp \
                                    ../src/GenieAPIService/src/model/model_manager.cpp \
                                    ../src/GenieAPIService/src/processor/harmony.cpp \
                                    ../src/GenieAPIService/src/processor/general.cpp \
                                    ../src/GenieAPIService/src/response/response_tools.cpp \
                                    ../src/GenieAPIService/src/response/response_dispatcher.cpp \
                                    ../src/common/utils.cpp \
                                    ../src/GenieAPIService/src/GenieAPIService.cpp

include $(BUILD_SHARED_LIBRARY)
#========================== Define Service Lib variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_MODULE                   := JNIGenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libappbuilder libGenie
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                :=   ../src/GenieAPIService/src/chat_history/chat_history.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/chat_request_handler.cpp \
									../src/GenieAPIService/src/context/context_base.cpp \
                                    ../src/GenieAPIService/src/context/qnn/genie.cpp \
                                    ../src/GenieAPIService/src/context/qnn/genie_interface.cpp \
                                    ../src/GenieAPIService/src/context/qnn/phi4mm/phi4mm.cpp \
                                    ../src/GenieAPIService/src/context/qnn/qwen2_5/qwen_2_5.cpp \
                                    ../src/GenieAPIService/src/context/qnn/qwen2_5_omini/qwen_2_5_omini.cpp \
                                    ../src/GenieAPIService/src/model/model_manager.cpp \
                                    ../src/GenieAPIService/src/processor/harmony.cpp \
                                    ../src/GenieAPIService/src/processor/general.cpp \
                                    ../src/GenieAPIService/src/response/response_tools.cpp \
                                    ../src/GenieAPIService/src/response/response_dispatcher.cpp \
                                    ../src/common/utils.cpp \
                                    ../src/GenieAPIService/src/GenieAPIService.cpp
include $(BUILD_SHARED_LIBRARY)
#========================== Define client app variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_MODULE                   := GenieAPIClient
LOCAL_SHARED_LIBRARIES 		   := libcurl
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := ../examples/GenieAPIClient/GenieAPIClient.cpp
include $(BUILD_EXECUTABLE)
