#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(appbuilder LANGUAGES CXX)

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
    LINK_DIRECTORIES(../lib/Release ../lib/RelWithDebInfo)
else()
    LINK_DIRECTORIES(../lib/ ../lib/Release)
endif()

INCLUDE_DIRECTORIES(../src)
if (APPBUILDER_ENABLE_EXECUTORCH)
    get_filename_component(EXECUTORCH_PARENT_INCLUDE_ROOT "$ENV{EXECUTORCH_ROOT}" DIRECTORY)
    INCLUDE_DIRECTORIES(
        "$ENV{EXECUTORCH_ROOT}"
        "${EXECUTORCH_PARENT_INCLUDE_ROOT}"
    )
    if (DEFINED ENV{EXECUTORCH_TORCH_INCLUDE_ROOT} AND
        NOT "$ENV{EXECUTORCH_TORCH_INCLUDE_ROOT}" STREQUAL "")
        INCLUDE_DIRECTORIES("$ENV{EXECUTORCH_TORCH_INCLUDE_ROOT}")
    endif()
    add_compile_definitions(APPBUILDER_ENABLE_EXECUTORCH=1)
    if (APPBUILDER_ENABLE_EXECUTORCH_QNN)
        add_compile_definitions(APPBUILDER_ENABLE_EXECUTORCH_QNN=1)
    endif()

    # Keep the Python extension's SDK validation in sync with the native
    # library build.  The native target links the portable operator
    # registration archive with --whole-archive; fail here too when a
    # packaging build points at an incomplete SDK.
    find_library(EXECUTORCH_MODULE_LIBRARY NAMES extension_module_static extension_module
                 PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
                 NO_DEFAULT_PATH)
    find_library(EXECUTORCH_CORE_LIBRARY NAMES executorch_core executorch executorch_shared
                 PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
                 NO_DEFAULT_PATH)
    find_library(EXECUTORCH_PORTABLE_OPS_LIBRARY NAMES portable_ops_lib
                 PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
                 NO_DEFAULT_PATH)
    find_library(EXECUTORCH_PORTABLE_KERNELS_LIBRARY NAMES portable_kernels
                 PATHS "$ENV{EXECUTORCH_ROOT}/lib" "$ENV{EXECUTORCH_ROOT}/build/lib"
                 NO_DEFAULT_PATH)
    if (NOT EXECUTORCH_MODULE_LIBRARY OR NOT EXECUTORCH_CORE_LIBRARY OR
        NOT EXECUTORCH_PORTABLE_OPS_LIBRARY OR NOT EXECUTORCH_PORTABLE_KERNELS_LIBRARY)
        message(FATAL_ERROR "EXECUTORCH_ROOT must provide extension_module(_static), executorch_core, portable_ops_lib, and portable_kernels under lib or build/lib.")
    endif()
endif()
if (APPBUILDER_ENABLE_TFLITE OR APPBUILDER_ENABLE_TFLITE_CPU)
    INCLUDE_DIRECTORIES("$ENV{TFLITE_ROOT}/include")
    if (APPBUILDER_ENABLE_TFLITE)
        INCLUDE_DIRECTORIES("$ENV{QNN_SDK_ROOT}/include")
        add_compile_definitions(APPBUILDER_ENABLE_TFLITE=1)
    else()
        add_compile_definitions(APPBUILDER_ENABLE_TFLITE_CPU=1)
    endif()
endif()
SET(LIBRARY_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../lib")

add_subdirectory(pybind11)

# AppBuilder pyd
pybind11_add_module(${PROJECT_NAME} AppBuilder.cpp)

if (WIN32)
    set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
    set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
    target_link_libraries(${PROJECT_NAME} PUBLIC libappbuilder)
else()
    target_link_libraries(${PROJECT_NAME} PUBLIC appbuilder.so)
    set_target_properties(${PROJECT_NAME} PROPERTIES
        INSTALL_RPATH "$ORIGIN"
        BUILD_WITH_INSTALL_RPATH TRUE
    )
endif()


# GenieBuilder pyd
if (WIN32)
    find_library(GENIE_LIBRARY NAMES Genie PATHS ../lib/Release)
else()
    find_library(GENIE_LIBRARY NAMES libGenie.so PATHS ../lib/Release)
endif()

if(GENIE_LIBRARY)
    pybind11_add_module(geniebuilder GenieBuilder.cpp)

    if (WIN32)
        set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
        set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
        target_link_libraries(geniebuilder PUBLIC Genie)
    else()
        target_link_libraries(geniebuilder PUBLIC libGenie.so)
        set_target_properties(geniebuilder PROPERTIES
            INSTALL_RPATH "$ORIGIN"
            BUILD_WITH_INSTALL_RPATH TRUE
        )
    endif()

    target_include_directories(geniebuilder PUBLIC
        $ENV{QNN_SDK_ROOT}/include/Genie
        $ENV{QNN_SDK_ROOT}/include
    )
else()
    message(FATAL_ERROR "Could not find Genie library")
endif()
