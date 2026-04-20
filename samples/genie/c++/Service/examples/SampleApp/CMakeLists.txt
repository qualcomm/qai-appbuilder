#==============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#==============================================================================


project(SampleApp)


add_executable(SampleApp SampleApp.cpp)

target_include_directories(SampleApp PRIVATE 
    ${BUILD_PATH}/include
)

if(WIN32)
    target_link_libraries(SampleApp PRIVATE 
        ${BUILD_PATH}/lib/GenieAPILibrary.lib
    )
endif()

