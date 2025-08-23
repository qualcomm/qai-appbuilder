# ==============================================================================
#
#  Copyright (c) 2020, 2022-2024 Qualcomm Technologies, Inc.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
#
# ===============================================================

LOCAL_PATH := $(call my-dir)
SUPPORTED_TARGET_ABI := arm64-v8a

#============================ Define Common Variables ===============================================================
# Include paths
PACKAGE_C_INCLUDES += -I ${QNN_SDK_ROOT}include/QNN
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/CachingUtil
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/Log
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/PAL/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/Utils
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/WrapperUtils
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../include/flatbuffers

#========================== Define OpPackage Library Build Variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_CPP_FEATURES             += exceptions
LOCAL_CPPFLAGS                 += -fexceptions
MY_SRC_FILES                   := $(wildcard $(LOCAL_PATH)/../src/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/Log/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/PAL/src/linux/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/PAL/src/common/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/Utils/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/WrapperUtils/*.cpp)
LOCAL_MODULE                   := appbuilder
LOCAL_SRC_FILES                := $(subst make/,,$(MY_SRC_FILES))
LOCAL_LDLIBS                   := -lGLESv2 -lEGL -llog
include $(BUILD_SHARED_LIBRARY)
