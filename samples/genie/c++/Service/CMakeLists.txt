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
if (NOT (${CMAKE_SYSTEM_NAME} STREQUAL "Windows"
        OR ${CMAKE_SYSTEM_NAME} STREQUAL "Linux"
        OR ${CMAKE_SYSTEM_NAME} STREQUAL "Android"))
    message(FATAL_ERROR "current os is: " ${CMAKE_SYSTEM_NAME} " only Windows / Linux / Android are supported")
endif ()

option(USE_MNN "Enable the use mnn platform" OFF)

option(USE_GGUF "Enable the use gguf platform" OFF)

option(BUILD_AS_DLL "Build as DLL library instead of executable" OFF)

if (NOT DEFINED QNN_STUB_VERSION)
    set(QNN_STUB_VERSION "v73")
endif ()

set(SCRIPT_PATH ${CMAKE_SOURCE_DIR}/scripts)
include(${SCRIPT_PATH}/version.cmake)

set(G_EXTERNAL_DIR ${CMAKE_SOURCE_DIR}/../External)
set(G_EXTERNAL_INCLUDE_PATH ${CMAKE_BINARY_DIR}/include)
set(G_EXTERNAL_BIN_PATH ${CMAKE_BINARY_DIR}/bin)
set(G_EXTERNAL_LIB_PATH ${CMAKE_BINARY_DIR}/lib)

# Output directory naming.
# Windows / Android keep their historical name "GenieService_v<APPVER>" so any
# existing deployment scripts continue to work.
# On Linux the name includes the QNN SDK version and optionally "_gguf" if the
# GGUF backend is enabled: "GenieService_v<APPVER>_qnn<SDKVER>[_gguf]"
if (UNIX AND NOT ANDROID)
    # Extract the QAIRT SDK version from QNN_SDK_ROOT path,
    # e.g. ".../qairt/2.45.40.260406" -> "2.45.40".
    set(_qnn_sdk_ver "unknown")
    string(REGEX MATCH "[0-9]+\\.[0-9]+\\.[0-9]+" _qnn_match "$ENV{QNN_SDK_ROOT}")
    if (NOT "${_qnn_match}" STREQUAL "")
        set(_qnn_sdk_ver "${_qnn_match}")
    endif ()
    set(_suffix "")
    if (USE_GGUF)
        set(_suffix "_gguf")
    endif ()
    set(BUILD_PATH ${CMAKE_SOURCE_DIR}/GenieService_v${QAI_APP_BUILDER_VERSION}_qnn${_qnn_sdk_ver}${_suffix})
else ()
    set(BUILD_PATH ${CMAKE_SOURCE_DIR}/GenieService_v${QAI_APP_BUILDER_VERSION})
endif ()

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG ${BUILD_PATH})

file(REMOVE_RECURSE ${BUILD_PATH})

if (MSVC)
    add_compile_options(
            /MD
            /O2
            /Ob2
            /utf-8
            /MP
            /FS
            /Zc:referenceBinding
            /UNDEBUG
    )
elseif (UNIX AND NOT ANDROID)
    # Native Linux only. Note that the Android build is driven by ndk-build
    # using scripts/Application.mk, not by this CMakeLists, so its options are
    # untouched. This branch deliberately uses "UNIX AND NOT ANDROID" rather
    # than "UNIX" to make that intent explicit.
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
    if(BUILD_AS_DLL)
        # GenieAPILibrary.lib is routed to ${BUILD_PATH}/lib via a per-target
        # ARCHIVE_OUTPUT_DIRECTORY property in src/GenieAPIService/CMakeLists.txt
        # (a global CMAKE_ARCHIVE_OUTPUT_DIRECTORY set here would be too late,
        # since src/GenieAPIService was already added above).
        add_subdirectory(examples/SampleApp)
    else()
        add_subdirectory(examples/GenieAPIClient)
        add_subdirectory(examples/tools)
    endif()
elseif (UNIX AND NOT ANDROID)
    # Linux: examples/GenieAPIClient depends on libcurl which in turn pulls in
    # OpenSSL, libpsl, etc. Building those is out of scope for the core service
    # and is gated behind BUILD_LINUX_CLIENT=ON. tools/ uses Win32-only audio
    # APIs and is intentionally skipped.
    option(BUILD_LINUX_CLIENT "Build GenieAPIClient sample on Linux" OFF)
    if (BUILD_LINUX_CLIENT AND NOT BUILD_AS_DLL)
        add_subdirectory(examples/GenieAPIClient)
    endif ()
endif ()

# always
#add_custom_target(GenieSerivesPacking ALL
#        COMMAND ${CMAKE_COMMAND} -E copy ${BUILD_VERSION_FILE} -t ${BUILD_PATH}
#)
