#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(libappbuilder)

set(APP "libappbuilder")
set(APP_SOURCES "QnnSampleApp.cpp"
                "main.cpp"
                "Log/Logger.cpp"
                "Log/LogUtils.cpp"
                "PAL/src/windows/Common.cpp"
                "PAL/src/windows/Directory.cpp"
                "PAL/src/windows/DynamicLoading.cpp"
                "PAL/src/windows/FileOp.cpp"
                "PAL/src/windows/Path.cpp"
                "PAL/src/common/GetOpt.cpp"
                "PAL/src/common/StringOp.cpp"
                "Utils/DataUtil.cpp"
                "Utils/DynamicLoadUtil.cpp"
                "Utils/IOTensor.cpp"
                "Utils/QnnSampleAppUtils.cpp"
                "WrapperUtils/QnnWrapperUtils.cpp"
                "LibAppBuilder.cpp")

ADD_LIBRARY(${APP} SHARED ${APP_SOURCES})

SET(LIBRARY_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../lib")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")
target_link_libraries(${APP} PRIVATE Shlwapi Shell32)
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
target_include_directories(${APP} PUBLIC CachingUtil
                                           Log
                                           PAL/include
                                           Utils
                                           WrapperUtils
                                           ${CMAKE_BINARY_DIR}
                                           $ENV{QNN_SDK_ROOT}/include/QNN
                                           SVC
                                           ./)

add_subdirectory(SVC)
