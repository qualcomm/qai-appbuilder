#==============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#==============================================================================


project(SampleApp)


add_executable(SampleApp SampleApp.cpp)

# SampleApp needs GenieAPILibrary.h. Pull it straight from the GenieAPIService
# source tree (where it actually lives) rather than relying on the DLL target's
# POST_BUILD copy into ${BUILD_PATH}/include, which only happens after the DLL
# is built and races with this target's compile.
target_include_directories(SampleApp PRIVATE
    ${CMAKE_SOURCE_DIR}/src/GenieAPIService/src
    ${BUILD_PATH}/include
)

# Ensure the GenieAPILibrary DLL (and its import lib) is built before SampleApp,
# so ${BUILD_PATH}/lib/GenieAPILibrary.lib exists at link time.
add_dependencies(SampleApp GenieAPILibrary)

if(WIN32)
    target_link_libraries(SampleApp PRIVATE
        ${BUILD_PATH}/lib/GenieAPILibrary.lib
    )
endif()

