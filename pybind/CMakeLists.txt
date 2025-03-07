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
    LINK_DIRECTORIES(../lib/)
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


# Just compile GenieBuilder for WoS.
if (WIN32)
    # GenieBuilder pyd
    pybind11_add_module(geniebuilder GenieBuilder.cpp)
    target_link_libraries(geniebuilder PUBLIC Genie)
    target_include_directories(geniebuilder PUBLIC $ENV{QNN_SDK_ROOT}/include/Genie)
endif()
