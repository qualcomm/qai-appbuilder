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

option(USE_MNN "Enable the use mnn platform" OFF)

option(USE_GGUF "Enable the use gguf platform" OFF)

set(SCRIPT_PATH ${CMAKE_SOURCE_DIR}/scripts)
include(${SCRIPT_PATH}/version.cmake)

set(G_EXTERNAL_DIR ${CMAKE_SOURCE_DIR}/../External)
set(G_EXTERNAL_INCLUDE_PATH ${CMAKE_BINARY_DIR}/include)
set(G_EXTERNAL_BIN_PATH ${CMAKE_BINARY_DIR}/bin)
set(G_EXTERNAL_LIB_PATH ${CMAKE_BINARY_DIR}/lib)

set(BUILD_PATH ${CMAKE_SOURCE_DIR}/GenieService_v${QAI_APP_BUILDER_VERSION})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG ${BUILD_PATH})
file(REMOVE_RECURSE ${BUILD_PATH})

add_compile_options(
        /MD
        /O2
        /Ob2
        /utf-8
)

include_directories(${G_EXTERNAL_INCLUDE_PATH}
        ${G_EXTERNAL_DIR}/json/single_include
        ${CMAKE_CURRENT_SOURCE_DIR}/src/common
)
link_directories(${G_EXTERNAL_LIB_PATH})
add_subdirectory(src/GenieAPIService)
add_subdirectory(examples/GenieAPIClient)

add_custom_target(GenieSerivesPacking
        COMMAND ${CMAKE_COMMAND} -E copy ${BUILD_VERSION_FILE} -t ${BUILD_PATH}
)
