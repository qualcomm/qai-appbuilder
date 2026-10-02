#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(libappbuilder)

option(APPBUILDER_ENABLE_TFLITE "Build direct TFLite + QNN delegate support" OFF)
option(APPBUILDER_ENABLE_TFLITE_CPU "Build direct TFLite CPU support" OFF)

if (APPBUILDER_ENABLE_TFLITE AND APPBUILDER_ENABLE_TFLITE_CPU)
  message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE and APPBUILDER_ENABLE_TFLITE_CPU are mutually exclusive.")
endif()

if (APPBUILDER_ENABLE_TFLITE_CPU)
  if (NOT WIN32)
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE_CPU is supported only on Windows.")
  endif()
  if (NOT DEFINED ENV{TFLITE_ROOT} OR "$ENV{TFLITE_ROOT}" STREQUAL "")
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE_CPU requires TFLITE_ROOT.")
  endif()
  if (NOT EXISTS "$ENV{TFLITE_ROOT}/include/tensorflow/lite/c/c_api.h")
    message(FATAL_ERROR "TFLITE_ROOT must contain include/tensorflow/lite/c/c_api.h.")
  endif()
endif()

if (APPBUILDER_ENABLE_TFLITE)
  if (NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE is supported only on Linux ARM64; current platform is ${CMAKE_SYSTEM_NAME}.")
  endif()
  if (NOT CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|AARCH64|ARM64)$")
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE is supported only on Linux ARM64; current processor is ${CMAKE_SYSTEM_PROCESSOR}.")
  endif()
  if (NOT DEFINED ENV{QNN_SDK_ROOT} OR "$ENV{QNN_SDK_ROOT}" STREQUAL "")
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE requires QNN_SDK_ROOT.")
  endif()
  if (NOT DEFINED ENV{TFLITE_ROOT} OR "$ENV{TFLITE_ROOT}" STREQUAL "")
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE requires TFLITE_ROOT with TFLite C API headers and libraries.")
  endif()
  if (NOT EXISTS "$ENV{TFLITE_ROOT}/include/tensorflow/lite/c/c_api.h")
    message(FATAL_ERROR "TFLITE_ROOT must contain include/tensorflow/lite/c/c_api.h.")
  endif()
  if (NOT EXISTS "$ENV{QNN_SDK_ROOT}/include/QNN/TFLiteDelegate/QnnTFLiteDelegate.h")
    message(FATAL_ERROR "QNN_SDK_ROOT must contain include/QNN/TFLiteDelegate/QnnTFLiteDelegate.h.")
  endif()
endif()

if (WIN32)
set(APP "libappbuilder")
else()
set(APP "appbuilder")
endif()

set(CMAKE_CXX_STANDARD 20)

set(APP_SOURCES "QnnInferenceEngine.cpp"
                "Log/Logger.cpp"
                "Log/LogUtils.cpp"
                "PAL/src/common/GetOpt.cpp"
                "PAL/src/common/StringOp.cpp"
                "Utils/DataUtil.cpp"
                "Utils/DynamicLoadUtil.cpp"
                "Utils/IOTensor.cpp"
		"Utils/QnnDlcUtils.cpp"
                "Utils/QnnAppUtils.cpp"
                "WrapperUtils/QnnWrapperUtils.cpp"
                "LibAppBuilder.cpp"
                "Lora.cpp")

if (APPBUILDER_ENABLE_TFLITE OR APPBUILDER_ENABLE_TFLITE_CPU)
  list(APPEND APP_SOURCES "TFLiteInferenceEngine.cpp")
endif()

if (WIN32)
set(APP_SOURCES_ARCH "PAL/src/windows/Common.cpp"
                "PAL/src/windows/Directory.cpp"
                "PAL/src/windows/DynamicLoading.cpp"
                "PAL/src/windows/FileOp.cpp"
                "PAL/src/windows/Path.cpp")
else()
set(APP_SOURCES_ARCH "PAL/src/linux/Directory.cpp"
                "PAL/src/linux/DynamicLoading.cpp"
                "PAL/src/linux/FileOp.cpp"
                "PAL/src/linux/Path.cpp")
endif()

ADD_LIBRARY(${APP} SHARED ${APP_SOURCES} ${APP_SOURCES_ARCH})

if (MSVC)
  target_compile_options(${APP} PRIVATE /O2 /GL /fp:fast)
  target_link_options(${APP}    PRIVATE /LTCG)
endif()

SET(LIBRARY_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../lib")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")
target_compile_definitions(${APP} PRIVATE DLL_EXPORTS)

if (WIN32)
target_link_libraries(${APP} PRIVATE Shlwapi Shell32)
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
endif()

target_include_directories(${APP} PUBLIC CachingUtil
                                         Log
                                         PAL/include
                                         Utils
                                         WrapperUtils
                                         ${CMAKE_BINARY_DIR}
                                         $ENV{QNN_SDK_ROOT}/include/QNN
                                         SVC
                                         ./)

if (APPBUILDER_ENABLE_TFLITE OR APPBUILDER_ENABLE_TFLITE_CPU)
  target_include_directories(${APP} PUBLIC "$ENV{TFLITE_ROOT}/include")
  if (APPBUILDER_ENABLE_TFLITE)
    target_compile_definitions(${APP} PUBLIC APPBUILDER_ENABLE_TFLITE=1)
    target_include_directories(${APP} PUBLIC "$ENV{QNN_SDK_ROOT}/include")
    find_library(TFLITE_C_LIBRARY NAMES tensorflowlite_c tflite_c
                 PATHS "$ENV{TFLITE_ROOT}/lib" "$ENV{TFLITE_ROOT}/lib/aarch64-oe-linux-gcc11.2"
                       "$ENV{TFLITE_ROOT}/lib/aarch64-linux-gnu"
                 NO_DEFAULT_PATH)
    if (NOT TFLITE_C_LIBRARY)
      message(FATAL_ERROR "Could not find the TFLite C API library (tensorflowlite_c or tflite_c) under TFLITE_ROOT/lib.")
    endif()
    find_library(QNN_TFLITE_DELEGATE_LIBRARY NAMES QnnTFLiteDelegate
                 PATHS "$ENV{QNN_SDK_ROOT}/lib/aarch64-oe-linux-gcc11.2"
                 NO_DEFAULT_PATH)
    if (NOT QNN_TFLITE_DELEGATE_LIBRARY)
      message(FATAL_ERROR "Could not find libQnnTFLiteDelegate.so under QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2.")
    endif()
    target_link_libraries(${APP} PRIVATE ${TFLITE_C_LIBRARY} ${QNN_TFLITE_DELEGATE_LIBRARY} dl)
  else()
    target_compile_definitions(${APP} PUBLIC APPBUILDER_ENABLE_TFLITE_CPU=1)
    find_library(TFLITE_C_LIBRARY NAMES tensorflowlite_c tflite_c
                 PATHS "$ENV{TFLITE_ROOT}/lib" "$ENV{TFLITE_ROOT}/lib/windows"
                 NO_DEFAULT_PATH)
    if (NOT TFLITE_C_LIBRARY)
      message(FATAL_ERROR "Could not find the Windows TFLite C API import library under TFLITE_ROOT/lib.")
    endif()
    target_link_libraries(${APP} PRIVATE ${TFLITE_C_LIBRARY})
  endif()
endif()

# The remote-inference service (QAIAppSvc) is now cross-platform.
add_subdirectory(SVC)

include_directories($ENV{QNN_SDK_ROOT}/include/QNN)

