#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(pyqnnhelper LANGUAGES CXX)

LINK_DIRECTORIES(../lib/Release)
INCLUDE_DIRECTORIES(../LibQNNHelper/src)

SET(LIBRARY_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../lib")

add_subdirectory(pybind11)
pybind11_add_module(${PROJECT_NAME} QNNHelper.cpp)

target_link_libraries(${PROJECT_NAME} PUBLIC libqnnhelper)
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
