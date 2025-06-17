#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\

cmake_minimum_required(VERSION 3.4...3.18)
set(CMAKE_CXX_STANDARD 20)

################### Build Service ###################
project(GenieAPIService)

set(APP "GenieAPIService")
set(APP_SOURCES "GenieAPIService.cpp"
                "GenieBuilder.cpp")

if (MSVC)
    add_compile_options(/utf-8)
endif()

if (WIN32)
    LINK_DIRECTORIES($ENV{QNN_SDK_ROOT}lib\\aarch64-windows-msvc)
    message(STATUS "$ENV{QNN_SDK_ROOT}lib\\aarch64-windows-msvc") 
endif()

add_executable(${APP} ${APP_SOURCES})

SET(EXECUTABLE_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/libs")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")
target_link_libraries(${APP} PRIVATE Shlwapi Shell32)

set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")

target_include_directories(${APP} PUBLIC ../External/cpp-httplib
                                         ../External/json/include
                                         ../External/fmt/include
                                         ../External/DuoTools/DuoEngine  ## for using 'windows_getopt.h' in this project.
                                         ./)

target_link_libraries(${APP} PUBLIC Genie)
target_include_directories(${APP} PUBLIC $ENV{QNN_SDK_ROOT}/include/Genie)


################### Build client ###################
project(GenieAPIClient)

set(APP "GenieAPIClient")
set(APP_SOURCES "GenieAPIClient.cpp")

LINK_DIRECTORIES(../External/curl/build/lib/Release/)

add_executable(${APP} ${APP_SOURCES})

SET(EXECUTABLE_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/libs")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")
target_link_libraries(${APP} Shlwapi Shell32)

set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")

target_include_directories(${APP} PUBLIC ../External/cpp-httplib
                                         ../External/json/include
                                         ../External/fmt/include
                                         ../External/curl/include
                                         ../External/DuoTools/DuoEngine  ## for using 'windows_getopt.h' in this project.
                                         ./)

target_link_libraries(${APP} libcurl_imp)
