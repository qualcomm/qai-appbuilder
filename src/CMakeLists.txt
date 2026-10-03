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
option(APPBUILDER_ENABLE_EXECUTORCH "Build direct ExecuTorch support" OFF)
option(APPBUILDER_ENABLE_EXECUTORCH_QNN "Enable the ExecuTorch Qualcomm QNN backend" OFF)

if (APPBUILDER_ENABLE_EXECUTORCH_QNN AND NOT APPBUILDER_ENABLE_EXECUTORCH)
  message(FATAL_ERROR "APPBUILDER_ENABLE_EXECUTORCH_QNN requires APPBUILDER_ENABLE_EXECUTORCH=ON.")
endif()

if (APPBUILDER_ENABLE_EXECUTORCH)
  if (NOT DEFINED ENV{EXECUTORCH_ROOT} OR "$ENV{EXECUTORCH_ROOT}" STREQUAL "")
    message(FATAL_ERROR "APPBUILDER_ENABLE_EXECUTORCH requires EXECUTORCH_ROOT.")
  endif()
  if (NOT EXISTS "$ENV{EXECUTORCH_ROOT}/extension/module/module.h")
    message(FATAL_ERROR "EXECUTORCH_ROOT must contain extension/module/module.h.")
  endif()
  if (APPBUILDER_ENABLE_EXECUTORCH_QNN)
    if (NOT DEFINED ENV{QNN_SDK_ROOT} OR "$ENV{QNN_SDK_ROOT}" STREQUAL "")
      message(FATAL_ERROR "APPBUILDER_ENABLE_EXECUTORCH_QNN requires QNN_SDK_ROOT.")
    endif()
    if (NOT EXISTS "$ENV{EXECUTORCH_ROOT}/backends/qualcomm/runtime/QnnExecuTorch.h")
      message(FATAL_ERROR "EXECUTORCH_ROOT must contain the Qualcomm ExecuTorch backend headers.")
    endif()
    if (NOT EXISTS "$ENV{QNN_SDK_ROOT}/include/QNN/QnnTypes.h")
      message(FATAL_ERROR "QNN_SDK_ROOT must contain include/QNN/QnnTypes.h.")
    endif()
  endif()
endif()

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

if (APPBUILDER_ENABLE_EXECUTORCH)
  list(APPEND APP_SOURCES "ExecuTorchInferenceEngine.cpp")
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

if (APPBUILDER_ENABLE_EXECUTORCH)
  target_compile_definitions(${APP} PUBLIC APPBUILDER_ENABLE_EXECUTORCH=1)
  get_filename_component(EXECUTORCH_PARENT_INCLUDE_ROOT "$ENV{EXECUTORCH_ROOT}" DIRECTORY)
  target_include_directories(${APP} PUBLIC
                             "$ENV{EXECUTORCH_ROOT}"
                             "${EXECUTORCH_PARENT_INCLUDE_ROOT}")
  if (DEFINED ENV{EXECUTORCH_TORCH_INCLUDE_ROOT} AND
      NOT "$ENV{EXECUTORCH_TORCH_INCLUDE_ROOT}" STREQUAL "")
    target_include_directories(${APP} PUBLIC "$ENV{EXECUTORCH_TORCH_INCLUDE_ROOT}")
  endif()
  find_library(EXECUTORCH_MODULE_LIBRARY NAMES extension_module_static extension_module
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_TENSOR_LIBRARY NAMES extension_tensor_static extension_tensor
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_DATA_LOADER_LIBRARY NAMES extension_data_loader_static extension_data_loader
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_FLAT_TENSOR_LIBRARY NAMES extension_flat_tensor_static extension_flat_tensor
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_NAMED_DATA_LIBRARY NAMES extension_named_data_map_static extension_named_data_map
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_EVALUE_LIBRARY NAMES extension_evalue_util_static extension_evalue_util
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_THREADPOOL_LIBRARY NAMES extension_threadpool_static extension_threadpool
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_RUNNER_LIBRARY NAMES extension_runner_util_static extension_runner_util
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_CORE_LIBRARY NAMES executorch_core executorch executorch_shared
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_XNNPACK_LIBRARY NAMES executorch_backend_xnnpack xnnpack_backend
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  # Portable ExecuTorch programs need the generated operator-registration
  # archive.  Its registration constructor is otherwise discarded by the
  # linker, so it is linked with --whole-archive below.
  find_library(EXECUTORCH_PORTABLE_OPS_LIBRARY NAMES portable_ops_lib
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_PORTABLE_KERNELS_LIBRARY NAMES portable_kernels
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  find_library(EXECUTORCH_KERNELS_UTIL_LIBRARY NAMES kernels_util_all_deps
               PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
               NO_DEFAULT_PATH)
  if (NOT EXECUTORCH_MODULE_LIBRARY OR NOT EXECUTORCH_CORE_LIBRARY OR
      NOT EXECUTORCH_PORTABLE_OPS_LIBRARY OR NOT EXECUTORCH_PORTABLE_KERNELS_LIBRARY)
    message(FATAL_ERROR "EXECUTORCH_ROOT must provide extension_module(_static), executorch_core, portable_ops_lib, and portable_kernels under lib or build/lib.")
  endif()
  set(EXECUTORCH_LIBRARIES
    -Wl,--start-group
    -Wl,--whole-archive
    ${EXECUTORCH_PORTABLE_OPS_LIBRARY}
    -Wl,--no-whole-archive
    ${EXECUTORCH_PORTABLE_KERNELS_LIBRARY}
    ${EXECUTORCH_KERNELS_UTIL_LIBRARY}
    ${EXECUTORCH_MODULE_LIBRARY}
    ${EXECUTORCH_TENSOR_LIBRARY}
    ${EXECUTORCH_DATA_LOADER_LIBRARY}
    ${EXECUTORCH_FLAT_TENSOR_LIBRARY}
    ${EXECUTORCH_NAMED_DATA_LIBRARY}
    ${EXECUTORCH_EVALUE_LIBRARY}
    ${EXECUTORCH_THREADPOOL_LIBRARY}
    ${EXECUTORCH_RUNNER_LIBRARY}
    ${EXECUTORCH_CORE_LIBRARY}
  )
  if (EXECUTORCH_XNNPACK_LIBRARY)
    list(APPEND EXECUTORCH_LIBRARIES ${EXECUTORCH_XNNPACK_LIBRARY})
  endif()
  list(APPEND EXECUTORCH_LIBRARIES -Wl,--end-group ${CMAKE_DL_LIBS})
  target_link_libraries(${APP} PRIVATE ${EXECUTORCH_LIBRARIES})

  set_target_properties(${APP} PROPERTIES BUILD_RPATH "$ENV{EXECUTORCH_ROOT}/lib")
  if (APPBUILDER_ENABLE_EXECUTORCH_QNN)
    target_compile_definitions(${APP} PUBLIC APPBUILDER_ENABLE_EXECUTORCH_QNN=1)
    find_library(EXECUTORCH_QNN_LIBRARY NAMES executorch_backend_qnn qnn_executorch_backend
                 PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
                 NO_DEFAULT_PATH)
    if (NOT EXECUTORCH_QNN_LIBRARY)
      message(FATAL_ERROR "EXECUTORCH_ROOT must provide the Qualcomm ExecuTorch backend library.")
    endif()
    target_link_libraries(${APP} PRIVATE ${EXECUTORCH_QNN_LIBRARY})
  endif()
endif()

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

