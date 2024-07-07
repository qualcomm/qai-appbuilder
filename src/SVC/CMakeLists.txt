#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(QAIAppSvc)

set(APP "QAIAppSvc")
set(APP_SOURCES "main.cpp")

LINK_DIRECTORIES(../../lib/Release ../../lib/RelWithDebInfo)

add_executable(${APP} ${APP_SOURCES})

target_link_libraries(${PROJECT_NAME} PUBLIC libappbuilder)
SET(EXECUTABLE_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../../lib")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")
target_link_libraries(${APP} PRIVATE Shlwapi Shell32)
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
target_include_directories(${APP} PUBLIC CachingUtil
                                         ../
                                         ./)
                                         