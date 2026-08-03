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
    # Mirrors platform.cmake's LIBAPPBUILDER_ROOT/lib (qai-appbuilder repo root) and
    # QNN_LIB_PATH; those variables live in the sibling src/GenieAPIService subdirectory
    # scope and aren't visible here, so the equivalent paths are spelled out directly.
    target_link_directories(SampleApp PRIVATE
            ${CMAKE_SOURCE_DIR}/../../../../lib
            $ENV{QNN_SDK_ROOT}/lib/aarch64-oe-linux-gcc11.2
    )
    # GenieAPILibrary links Genie/appbuilder PRIVATE, so that requirement doesn't
    # propagate to SampleApp; this executable's own link step must resolve them
    # directly too, same as GenieAPIService's target_link_libraries in its CMakeLists.
    target_link_libraries(SampleApp PRIVATE Genie appbuilder)
    set_target_properties(SampleApp PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN"
    )
endif ()

