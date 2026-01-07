#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

project(GenieAPIClient)

ExternalProject_Add(Libcurl
        SOURCE_DIR ${G_EXTERNAL_DIR}/curl
        CMAKE_ARGS
        -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
        -DBUILD_SHARED_LIBS=OFF
        -DBUILD_CURL_EXE=OFF
        -DBUILD_TESTING=OFF
        -DCURL_USE_LIBPSL=OFF
        -DENABLE_UNICODE=ON
        BUILD_IN_SOURCE ON
)

add_executable(${PROJECT_NAME}
        GenieAPIClient.cpp
        ${CMAKE_SOURCE_DIR}/src/common/utils.cpp
)
target_include_directories(${PROJECT_NAME} PRIVATE ${EXTERNAL_HEADER_PATH})
target_compile_definitions(${PROJECT_NAME} PRIVATE -DCURL_STATICLIB -DNOMINMAX)
target_link_libraries(${PROJECT_NAME} PRIVATE libcurl Iphlpapi ws2_32 Wldap32)
