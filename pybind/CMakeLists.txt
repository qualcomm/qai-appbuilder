#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(appbuilder LANGUAGES CXX)

if (WIN32)
    LINK_DIRECTORIES(../lib/Release ../lib/RelWithDebInfo)
else()
    LINK_DIRECTORIES(../lib/ ../lib/Release)
endif()

INCLUDE_DIRECTORIES(../src)
SET(LIBRARY_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../lib")

add_subdirectory(pybind11)

# AppBuilder pyd
pybind11_add_module(${PROJECT_NAME} AppBuilder.cpp)

if (WIN32)
    set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
    set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
    target_link_libraries(${PROJECT_NAME} PUBLIC libappbuilder)
else()
    target_link_libraries(${PROJECT_NAME} PUBLIC appbuilder.so)
endif()


# GenieBuilder pyd
if (WIN32)
    find_library(GENIE_LIBRARY NAMES Genie PATHS ../lib/Release)
else()
    find_library(GENIE_LIBRARY NAMES libGenie.so PATHS ../lib/Release)
endif()

if(GENIE_LIBRARY)
    pybind11_add_module(geniebuilder GenieBuilder.cpp)

    if (WIN32)
        set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
        set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
        target_link_libraries(geniebuilder PUBLIC Genie)
    else()
        target_link_libraries(geniebuilder PUBLIC libGenie.so)
    endif()

    target_include_directories(geniebuilder PUBLIC $ENV{QNN_SDK_ROOT}/include/Genie)
else()
    message(STATUS "Could not find Genie library")
endif()
