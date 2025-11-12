#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.22.1)

project("superresolution")

include_directories(
        ${CMAKE_SOURCE_DIR}/External/QAIAppBuilder/include
)

add_library(${CMAKE_PROJECT_NAME} SHARED native-lib.cpp)

# add_executable(${CMAKE_PROJECT_NAME} native-lib.cpp) # Build command line executable binary for debugging.

add_library(qaiappbuilder SHARED IMPORTED)
set_target_properties(
        qaiappbuilder
        PROPERTIES IMPORTED_LOCATION
        ${CMAKE_SOURCE_DIR}/External/QAIAppBuilder/libappbuilder.so
)

target_link_libraries(${CMAKE_PROJECT_NAME}
        # List libraries link to the target library
        android
        qaiappbuilder
        log)
