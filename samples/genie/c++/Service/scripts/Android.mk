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
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/cli11/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/libsamplerate/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/LibrosaCpp
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../../External/dr_libs
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
LOCAL_SRC_FILES := ../build-android/libappbuilder.so
include $(PREBUILT_SHARED_LIBRARY)
#========================== Define libsamplerate.so variables =============================================
include $(CLEAR_VARS)
LOCAL_MODULE := libsamplerate
LOCAL_SRC_FILES := ../build-android/libsamplerate.so
include $(PREBUILT_SHARED_LIBRARY)
#========================== Define Service Lib variables =============================================
# Android native builds are QNN-only; do not add MNN or GGUF sources here.
SERVICE_SRC_FILES :=            ../src/GenieAPIService/src/chat_history/chat_history.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/chat_request_handler.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/prompt_optimizer.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/message_pre_filter.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/prompt_preparation_service.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/summary_cache.cpp \
                                    ../src/GenieAPIService/src/chat_request_handler/long_text_summarizer.cpp \
									../src/GenieAPIService/src/context/context_base.cpp \
                                    ../src/GenieAPIService/src/context/qnn/genie.cpp \
                                    ../src/GenieAPIService/src/context/qnn/genie_interface.cpp \
                                    ../src/GenieAPIService/src/context/qnn/phi4mm/phi4mm.cpp \
                                    ../src/GenieAPIService/src/context/qnn/qwen2_5/qwen_2_5.cpp \
                                    ../src/GenieAPIService/src/context/qnn/qwen2_5_omini/qwen_2_5_omini.cpp \
                                    ../src/GenieAPIService/src/model/model_manager.cpp \
                                    ../src/GenieAPIService/src/port_available.cpp \
                                    ../src/GenieAPIService/src/processor/harmony.cpp \
                                    ../src/GenieAPIService/src/processor/general.cpp \
                                    ../src/GenieAPIService/src/response/response_tools.cpp \
                                    ../src/GenieAPIService/src/response/response_dispatcher.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_routing.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_session.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_history.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_cloud.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_steps.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_incremental.cpp \
                                    ../src/GenieAPIService/src/gateway/gateway/gateway_overflow.cpp \
                                    ../src/GenieAPIService/src/gateway/security/content_security_inspector.cpp \
                                    ../src/GenieAPIService/src/gateway/security/desensitizer.cpp \
                                    ../src/GenieAPIService/src/gateway/security/task_complexity_evaluator.cpp \
                                    ../src/GenieAPIService/src/gateway/routing/model_router.cpp \
                                    ../src/GenieAPIService/src/gateway/cloud/cloud_model_client.cpp \
                                    ../src/GenieAPIService/src/gateway/audit/audit_logger.cpp \
                                    ../src/common/utils.cpp \
                                    ../src/GenieAPIService/src/GenieAPIService.cpp

include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_MODULE                   := GenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libappbuilder libGenie libsamplerate
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := $(SERVICE_SRC_FILES)

include $(BUILD_SHARED_LIBRARY)
#========================== Define Service Lib variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_MODULE                   := JNIGenieAPIService
LOCAL_SHARED_LIBRARIES 		   := libappbuilder libGenie libsamplerate
LOCAL_LDLIBS                   := -llog
LOCAL_SRC_FILES                := $(SERVICE_SRC_FILES)
include $(BUILD_SHARED_LIBRARY)
