#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(libappbuilder)

if (WIN32)
set(APP "libappbuilder")
else()
set(APP "appbuilder")
endif()

set(APP_SOURCES "QnnSampleApp.cpp"
                "main.cpp"
                "Log/Logger.cpp"
                "Log/LogUtils.cpp"
                "PAL/src/common/GetOpt.cpp"
                "PAL/src/common/StringOp.cpp"
                "Utils/DataUtil.cpp"
                "Utils/DynamicLoadUtil.cpp"
                "Utils/IOTensor.cpp"
		"Utils/QnnDlcUtils.cpp"
                "Utils/QnnSampleAppUtils.cpp"
                "WrapperUtils/QnnWrapperUtils.cpp"
                "LibAppBuilder.cpp"
                "Lora.cpp")

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
target_compile_features(${APP} PRIVATE cxx_std_20)

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
if (WIN32)
add_subdirectory(SVC)
endif()

include_directories($ENV{QNN_SDK_ROOT}/include/QNN)

