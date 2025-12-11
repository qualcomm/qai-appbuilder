#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

CMAKE_MINIMUM_REQUIRED(VERSION 3.4...3.18)
set(CMAKE_CXX_STANDARD 17)

################### Build Service ###################
project(GenieAPIService)

option(USE_MNN "Enable the use mnn platform" OFF)

option(USE_GGUF "Enable the use gguf platform" OFF)

set(SCRIPT_PATH ${CMAKE_SOURCE_DIR}/scripts)
include(${SCRIPT_PATH}/version.cmake)
include(${SCRIPT_PATH}/platform.cmake)

set(BUILD_PATH ${CMAKE_SOURCE_DIR}/GenieSerivce_v${QAI_APP_BUILDER_VERSION})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG ${BUILD_PATH})
set(APP "GenieAPIService")

file(REMOVE_RECURSE ${BUILD_PATH})

list(APPEND SOURCE_HEADER
        ${EXTERNAL_HEADER_PATH}
        ${CMAKE_CURRENT_SOURCE_DIR}/src/core/include
)

list(APPEND SERVICE_SOURCES
        src/core/context_base.cpp
        src/core/chat_history.cpp
        src/core/utils.cpp
        src/core/model_manager.cpp
        src/context/genie.cpp
        src/GenieAPIService.cpp
        src/processor/harmony.cpp
        src/processor/general.cpp
        src/core/response/response_tools.cpp
        src/core/response/response_dispatcher.cpp
)

add_compile_options(
        -DQAI_APP_BUILDER_MAJOR_VERSION=${QAI_APP_BUILDER_MAJOR_VERSION}
        -DQAI_APP_BUILDER_MINOR_VERSION=${QAI_APP_BUILDER_MINOR_VERSION}
        -DQAI_APP_BUILDER_PATCH_VERSION=${QAI_APP_BUILDER_PATCH_VERSION}
        /MD
        /O2
        /Ob2
        /utf-8
)

if (USE_MNN)
    add_definitions(-DUSE_MNN)
    list(APPEND SERVICE_SOURCES
            src/context/mnn.cpp
    )
endif ()

if (USE_GGUF)
    add_definitions(-DUSE_GGUF)
    list(APPEND SERVICE_SOURCES
            src/context/llama_cpp.cpp
    )
endif ()

configure_file(
        ${SCRIPT_PATH}/version.rc.in
        ${CMAKE_BINARY_DIR}/version.rc
        @ONLY
)

include_directories(${SOURCE_HEADER})
add_executable(${APP} ${SERVICE_SOURCES})
target_compile_definitions(${APP} PUBLIC -DNOMINMAX -DBUILDING_LIBCURL -D_HAS_STD_BYTE=0)
target_link_libraries(${APP} PRIVATE
        Shlwapi
        Shell32
        Wldap32.lib
        Iphlpapi.lib
        ${EXTERNAL_LIBS}
)

################### Build Client ###################
set(CLIENT_APP "GenieAPIClient")
add_executable(${CLIENT_APP} src/GenieAPIClient.cpp)
target_compile_definitions(${CLIENT_APP} PUBLIC -DNOMINMAX -DCURL_STATICLIB)
target_link_libraries(${CLIENT_APP} PRIVATE advapi32 Wldap32 Iphlpapi ws2_32 Shlwapi Shell32 libcurl)

################### Generate Release ###################
add_custom_command(
        TARGET ${APP} PRE_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy ${BUILD_VERSION_FILE} -t ${BUILD_PATH}
)

foreach (FILE IN LISTS EXTERNAL_BIN)
    add_custom_command(
            TARGET ${APP} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E echo copy ${FILE} to ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy ${FILE} -t ${BUILD_PATH}
    )
endforeach ()