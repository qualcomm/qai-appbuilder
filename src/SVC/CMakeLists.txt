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

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

LINK_DIRECTORIES(../../lib/Release ../../lib/RelWithDebInfo ../../lib)

add_executable(${APP} ${APP_SOURCES})

SET(EXECUTABLE_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../../lib")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")

target_include_directories(${APP} PUBLIC CachingUtil
                                         ../
                                         ./)

if (WIN32)
    # On Windows the import library is 'libappbuilder'.
    target_link_libraries(${PROJECT_NAME} PUBLIC libappbuilder)
    target_link_libraries(${APP} PRIVATE Shlwapi Shell32)
    set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
    set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
else()
    # On POSIX the shared library is 'appbuilder'.
    target_link_libraries(${APP} PRIVATE appbuilder pthread)
    if (NOT ANDROID)
        # shm_open / clock primitives live in librt on glibc.
        target_link_libraries(${APP} PRIVATE rt)
    endif()
    if (ANDROID)
        # ASharedMemory_create + __android_log_print.
        target_link_libraries(${APP} PRIVATE android log)
    endif()
endif()
