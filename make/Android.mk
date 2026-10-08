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
LOCAL_C_INCLUDES               += -I $(LOCAL_PATH)/../src/SVC
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
LOCAL_LDLIBS                   := -lGLESv2 -lEGL -llog -landroid
include $(BUILD_SHARED_LIBRARY)

#====================== Define QAIAppSvc (remote-inference service) Executable ===================================
# Cross-platform service process: launched by libappbuilder to run a model in a
# separate process. Communicates over an AF_UNIX socketpair and shares tensor
# memory via ASharedMemory (passed as an fd through SCM_RIGHTS).
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_C_INCLUDES               += -I $(LOCAL_PATH)/../src/SVC
LOCAL_CPP_FEATURES             += exceptions
LOCAL_CPPFLAGS                 += -fexceptions
LOCAL_MODULE                   := QAIAppSvc
LOCAL_SRC_FILES                := $(subst make/,,$(LOCAL_PATH)/../src/SVC/main.cpp)
LOCAL_SHARED_LIBRARIES         := appbuilder
LOCAL_LDLIBS                   := -landroid -llog
include $(BUILD_EXECUTABLE)
