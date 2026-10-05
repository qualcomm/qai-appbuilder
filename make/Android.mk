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
PACKAGE_C_INCLUDES += -I $(QNN_SDK_ROOT)/include/QNN
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/CachingUtil
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/Log
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/PAL/include
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/Utils
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../src/WrapperUtils
PACKAGE_C_INCLUDES += -I $(LOCAL_PATH)/../include/flatbuffers

# Optional ExecuTorch native integration. EXECUTORCH_ROOT must point to an
# Android arm64-v8a ExecuTorch build; the host/Linux SDK is not compatible.
ifneq ($(EXECUTORCH_ROOT),)
PACKAGE_C_INCLUDES += -I $(EXECUTORCH_ROOT)
PACKAGE_C_INCLUDES += -I $(EXECUTORCH_ROOT)/..
PACKAGE_C_INCLUDES += -I $(EXECUTORCH_ROOT)/runtime/core/portable_type/c10
endif

EXECUTORCH_LIB_DIR ?= $(EXECUTORCH_ROOT)/lib
EXECUTORCH_BUILD_ROOT ?= $(EXECUTORCH_ROOT)/android-build
EXECUTORCH_TORCH_INCLUDE_ROOT ?=

ifneq ($(EXECUTORCH_ROOT),)
PACKAGE_C_INCLUDES += -I $(EXECUTORCH_BUILD_ROOT)/include
PACKAGE_C_INCLUDES += -I $(EXECUTORCH_BUILD_ROOT)/schema/include
endif

ifneq ($(EXECUTORCH_TORCH_INCLUDE_ROOT),)
PACKAGE_C_INCLUDES += -I $(EXECUTORCH_TORCH_INCLUDE_ROOT)
endif

ifneq ($(EXECUTORCH_ROOT),)
EXECUTORCH_LINK_LIBS := \
    -Wl,--start-group \
    -Wl,--no-as-needed \
    -Wl,--whole-archive \
    -lportable_ops_lib \
    -Wl,--no-whole-archive \
    -Wl,--as-needed \
    -lquantized_ops_lib \
    -lportable_kernels \
    -lquantized_kernels \
    -lkernels_util_all_deps \
    -lextension_module_static \
    -lextension_tensor \
    -lextension_data_loader \
    -lextension_flat_tensor \
    -lextension_named_data_map \
    -lextension_evalue_util \
    -lextension_threadpool \
    -lpthreadpool \
    -lcpuinfo \
    -lextension_runner_util \
    -lexecutorch_core
ifneq ($(EXECUTORCH_ENABLE_XNNPACK),)
EXECUTORCH_LINK_LIBS += -Wl,--no-as-needed -Wl,--whole-archive -lexecutorch_backend_xnnpack -Wl,--no-whole-archive -Wl,--as-needed
endif
ifneq ($(EXECUTORCH_ENABLE_QNN),)
EXECUTORCH_LINK_LIBS += -Wl,--no-as-needed -Wl,--whole-archive -lqnn_executorch_backend -Wl,--no-whole-archive -Wl,--as-needed
endif
EXECUTORCH_LINK_LIBS += -Wl,--end-group
endif

#========================== Define OpPackage Library Build Variables =============================================
include $(CLEAR_VARS)
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES)
LOCAL_C_INCLUDES               += -I $(LOCAL_PATH)/../src/SVC
LOCAL_CPP_FEATURES             += exceptions
LOCAL_CPPFLAGS                 += -fexceptions
MY_SRC_FILES                   := $(filter-out $(LOCAL_PATH)/../src/TFLiteInferenceEngine.cpp $(LOCAL_PATH)/../src/ExecuTorchInferenceEngine.cpp,$(wildcard $(LOCAL_PATH)/../src/*.cpp))
ifneq ($(EXECUTORCH_ROOT),)
MY_SRC_FILES                   += $(LOCAL_PATH)/../src/ExecuTorchInferenceEngine.cpp
LOCAL_C_INCLUDES               += -I $(EXECUTORCH_ROOT)
LOCAL_CPPFLAGS                 += -DAPPBUILDER_ENABLE_EXECUTORCH=1
endif
LOCAL_C_INCLUDES               := $(PACKAGE_C_INCLUDES) $(LOCAL_C_INCLUDES)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/Log/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/PAL/src/linux/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/PAL/src/common/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/Utils/*.cpp)
MY_SRC_FILES                   += $(wildcard $(LOCAL_PATH)/../src/WrapperUtils/*.cpp)
LOCAL_MODULE                   := appbuilder
LOCAL_SRC_FILES                := $(patsubst $(LOCAL_PATH)/%,%,$(MY_SRC_FILES))
LOCAL_LDLIBS                   += -lGLESv2 -lEGL -llog -landroid

ifneq ($(EXECUTORCH_ROOT),)
LOCAL_LDLIBS                   += -L$(EXECUTORCH_LIB_DIR) $(EXECUTORCH_LINK_LIBS) -ldl
ifneq ($(EXECUTORCH_ENABLE_QNN),)
LOCAL_CPPFLAGS                 += -DAPPBUILDER_ENABLE_EXECUTORCH_QNN=1
endif
endif
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
LOCAL_SRC_FILES                := ../src/SVC/main.cpp
LOCAL_SHARED_LIBRARIES         := appbuilder
LOCAL_LDLIBS                   := -landroid -llog
include $(BUILD_EXECUTABLE)
