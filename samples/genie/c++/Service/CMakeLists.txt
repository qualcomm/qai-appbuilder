#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

CMAKE_MINIMUM_REQUIRED(VERSION 3.4...3.18)
set(CMAKE_CXX_STANDARD 17)

project(GenieAPIService)
if (ANDROID OR "${CMAKE_SYSTEM_NAME}" STREQUAL "Android")
    message(FATAL_ERROR "Android is not supported by this CMake project. Use samples/genie/c++/build_android.bat with ndk-build instead.")
elseif (NOT ("${CMAKE_SYSTEM_NAME}" STREQUAL "Windows"
        OR "${CMAKE_SYSTEM_NAME}" STREQUAL "Linux"))
    message(FATAL_ERROR "current os is: " ${CMAKE_SYSTEM_NAME} " only Windows / Linux are supported")
endif ()

if (MSVC)
    option(USE_MNN "Enable the use mnn platform" OFF)
    option(USE_GGUF "Enable the use gguf platform" OFF)
else ()
    if (DEFINED USE_MNN AND USE_MNN)
        message(FATAL_ERROR "USE_MNN is supported only on Windows/MSVC; Linux builds are QNN-only.")
    endif ()
    if (DEFINED USE_GGUF AND USE_GGUF)
        message(FATAL_ERROR "USE_GGUF is supported only on Windows/MSVC; Linux builds are QNN-only.")
    endif ()
    set(USE_MNN OFF)
    set(USE_GGUF OFF)
endif ()

option(BUILD_AS_DLL "Build only the GenieAPILibrary DLL target, excluding the exe and example targets (default OFF builds everything)" OFF)

set(SCRIPT_PATH ${CMAKE_SOURCE_DIR}/scripts)
include(${SCRIPT_PATH}/version.cmake)

set(G_EXTERNAL_DIR ${CMAKE_SOURCE_DIR}/../External)
set(G_EXTERNAL_INCLUDE_PATH ${CMAKE_BINARY_DIR}/include)
set(G_EXTERNAL_BIN_PATH ${CMAKE_BINARY_DIR}/bin)
set(G_EXTERNAL_LIB_PATH ${CMAKE_BINARY_DIR}/lib)

# Fixed, version-free, platform-specific output directory name; switching QNN SDK
# versions and rebuilding will overwrite the previous artifacts (accepted trade-off).
if (MSVC)
    set(BUILD_PATH ${CMAKE_BINARY_DIR}/GenieService-win-arm64)
else ()
    set(BUILD_PATH ${CMAKE_BINARY_DIR}/GenieService-linux-arm64)
endif ()
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG ${BUILD_PATH})
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH}/lib)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_DEBUG ${BUILD_PATH}/lib)
# Linux 上共享库(.so)属于 LIBRARY 产物类型(与 Windows 的 dll 走 RUNTIME 不同)，需单独指定，
# 否则 GenieAPILibrary.so 不会落到 BUILD_PATH。
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH})
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY_DEBUG ${BUILD_PATH})

if (MSVC)
    add_compile_options(
            /MD
            /O2
            /Ob2
            /utf-8
            /MP
            /FS
            /Zc:referenceBinding
    )
elseif (UNIX)
    add_compile_options(
            -O2
            -fPIC
            -Wno-deprecated-declarations
            -Wno-attributes
    )
    link_libraries(pthread dl)
endif ()

include_directories(${G_EXTERNAL_INCLUDE_PATH}
        ${G_EXTERNAL_DIR}/json/single_include
        ${CMAKE_CURRENT_SOURCE_DIR}/src/common
)
link_directories(${G_EXTERNAL_LIB_PATH})
add_subdirectory(src/GenieAPIService)

if (MSVC)
    if (NOT BUILD_AS_DLL)
        add_subdirectory(examples/GenieAPIClient)
        add_subdirectory(examples/tools)
        add_subdirectory(examples/SampleApp)
    endif ()
elseif (UNIX)
    option(BUILD_LINUX_CLIENT "Build GenieAPIClient sample on Linux" OFF)
    if (BUILD_LINUX_CLIENT AND NOT BUILD_AS_DLL)
        add_subdirectory(examples/GenieAPIClient)
    endif ()
    if (NOT BUILD_AS_DLL)
        add_subdirectory(examples/SampleApp)
    endif ()
endif ()
