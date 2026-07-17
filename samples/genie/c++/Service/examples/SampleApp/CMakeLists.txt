#==============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#==============================================================================


project(SampleApp)

add_executable(SampleApp
        SampleApp.cpp
        ${CMAKE_SOURCE_DIR}/src/common/utils.cpp
)

target_include_directories(SampleApp PRIVATE
        ${BUILD_PATH}/include
        ${CMAKE_SOURCE_DIR}/src/common
        ${CMAKE_SOURCE_DIR}/src/GenieAPIService
)

target_link_libraries(SampleApp PRIVATE
    GenieAPILibrary
)

if (UNIX)
    set_target_properties(SampleApp PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN"
    )
endif ()

