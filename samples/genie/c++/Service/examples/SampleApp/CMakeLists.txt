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
    # Mirrors platform.cmake's LIBAPPBUILDER_ROOT/lib (qai-appbuilder repo root); that
    # variable lives in the sibling src/GenieAPIService subdirectory scope and isn't
    # visible here, so the equivalent relative path is spelled out directly.
    target_link_directories(SampleApp PRIVATE ${CMAKE_SOURCE_DIR}/../../../../lib)
    set_target_properties(SampleApp PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN"
    )
endif ()

