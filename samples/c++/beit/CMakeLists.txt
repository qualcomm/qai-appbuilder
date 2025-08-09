#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.10)
project(beit)

set(CMAKE_CXX_STANDARD 17)
#set(CMAKE_TOOLCHAIN_FILE "C:/vcpkg/scripts/buildsystems/vcpkg.cmake" CACHE STRING "")
#set(CMAKE_PREFIX_PATH "C:/audio/torch/libtorch-win-shared-with-deps-2.7.1+cpu/libtorch/lib")
#set(OpenCV_DIR "C:/path/to/opencv/build_msvc")
#set(Xtensor_DIR "C:/vcpkg/installed/arm64-windows/share/xtensor")
#set(Xtl_DIR "C:/vcpkg/installed/arm64-windows/share/xtl")
set(APPBUILDER_DIR "${CMAKE_SOURCE_DIR}/3rd/QAI_AppBuilder-win_arm64-QNN2.34.0-Release")
set(APPBUILDER_DLL "${APPBUILDER_DIR}/libappbuilder.dll")

set(TARGET_OUTPUT_DIR "${CMAKE_RUNTIME_OUTPUT_DIRECTORY}/${CMAKE_BUILD_TYPE}")

find_package(OpenCV REQUIRED COMPONENTS core imgproc highgui)
find_package(xtensor REQUIRED)
find_package(xtl REQUIRED)
#find_package(Torch REQUIRED)

if(OpenCV_FOUND)
  message(STATUS "OpenCV library status:")
  message(STATUS "    version: ${OpenCV_VERSION}")
  message(STATUS "    libraries: ${OpenCV_LIBS}")
  message(STATUS "    include path: ${OpenCV_INCLUDE_DIRS}")
  message(STATUS "    lib path: ${OpenCV_LIBRARY_DIRS}")
else()
  message(FATAL_ERROR "Could not find OpenCV")
endif()
foreach(list ${OpenCV_LIBS})
    message(${list})
endforeach(list)

if(xtensor_FOUND)
  message(STATUS "xtensor library status:")
  message(STATUS "    version: ${xtensor_VERSION}")
  message(STATUS "    libraries: ${xtensor_LIBRARY}")
  message(STATUS "    include path: ${xtensor_INCLUDE_DIRS}")
  message(STATUS "    lib path: ${xtensor_LIBRARY_DIRS}")
else()
  message(FATAL_ERROR "Could not find xtensor")
endif()
foreach(list ${xtensor_LIBS})
    message(${list})
endforeach(list)

if(xtl_FOUND)
  message(STATUS "xtl library status:")
  message(STATUS "    version: ${xtl_VERSION}")
  message(STATUS "    libraries: ${xtl_LIBRARY}")
  message(STATUS "    include path: ${xtl_INCLUDE_DIRS}")
  message(STATUS "    lib path: ${xtl_LIBRARY_DIRS}")
else()
  message(FATAL_ERROR "Could not find xtl")
endif()
foreach(list ${xtl_LIBS})
    message(${list})
endforeach(list)

link_directories(${APPBUILDER_DIR})
include_directories(${APPBUILDER_DIR}/include)

add_executable(beit beit.cpp) 
  
target_link_libraries(beit PRIVATE xtensor ${OpenCV_LIBS} xtl libappbuilder)


add_custom_command(TARGET beit POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
    ${APPBUILDER_DLL}
    $<TARGET_FILE_DIR:beit>
)

#target_link_libraries(beit opencv_core xtensor)
#target_link_libraries(torch)

                   