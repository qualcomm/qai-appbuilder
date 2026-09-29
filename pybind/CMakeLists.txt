#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(appbuilder LANGUAGES CXX)

option(APPBUILDER_ENABLE_TFLITE "Build direct TFLite + QNN delegate support" OFF)
option(APPBUILDER_ENABLE_TFLITE_CPU "Build direct TFLite CPU support" OFF)

if (APPBUILDER_ENABLE_TFLITE AND APPBUILDER_ENABLE_TFLITE_CPU)
    message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE and APPBUILDER_ENABLE_TFLITE_CPU are mutually exclusive.")
endif()

if (APPBUILDER_ENABLE_TFLITE_CPU)
    if (NOT WIN32)
        message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE_CPU is supported only on Windows.")
    endif()
    if (NOT DEFINED ENV{TFLITE_ROOT} OR "$ENV{TFLITE_ROOT}" STREQUAL "")
        message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE_CPU requires TFLITE_ROOT.")
    endif()
endif()

if (APPBUILDER_ENABLE_TFLITE)
    if (NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
        message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE is supported only on Linux ARM64.")
    endif()
    if (NOT DEFINED ENV{TFLITE_ROOT} OR "$ENV{TFLITE_ROOT}" STREQUAL "")
        message(FATAL_ERROR "APPBUILDER_ENABLE_TFLITE requires TFLITE_ROOT.")
    endif()
endif()

if (WIN32)
    LINK_DIRECTORIES(../lib/Release ../lib/RelWithDebInfo)
else()
    LINK_DIRECTORIES(../lib/ ../lib/Release)
endif()

INCLUDE_DIRECTORIES(../src)
if (APPBUILDER_ENABLE_TFLITE OR APPBUILDER_ENABLE_TFLITE_CPU)
    INCLUDE_DIRECTORIES("$ENV{TFLITE_ROOT}/include")
    if (APPBUILDER_ENABLE_TFLITE)
        INCLUDE_DIRECTORIES("$ENV{QNN_SDK_ROOT}/include")
        add_compile_definitions(APPBUILDER_ENABLE_TFLITE=1)
    else()
        add_compile_definitions(APPBUILDER_ENABLE_TFLITE_CPU=1)
    endif()
endif()
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
    set_target_properties(${PROJECT_NAME} PROPERTIES
        INSTALL_RPATH "$ORIGIN"
        BUILD_WITH_INSTALL_RPATH TRUE
    )
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
        set_target_properties(geniebuilder PROPERTIES
            INSTALL_RPATH "$ORIGIN"
            BUILD_WITH_INSTALL_RPATH TRUE
        )
    endif()

    target_include_directories(geniebuilder PUBLIC
        $ENV{QNN_SDK_ROOT}/include/Genie
        $ENV{QNN_SDK_ROOT}/include
    )
else()
    message(FATAL_ERROR "Could not find Genie library")
endif()
