#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

project(GenieAPIClient)

add_executable(${PROJECT_NAME}
        GenieAPIClient.cpp
        ${CMAKE_SOURCE_DIR}/src/common/utils.cpp
)
target_include_directories(${PROJECT_NAME} PRIVATE ${EXTERNAL_HEADER_PATH} ${G_EXTERNAL_DIR}/curl/include)

if (MSVC)
    target_compile_definitions(${PROJECT_NAME} PRIVATE -DCURL_STATICLIB -DNOMINMAX)
    target_link_directories(${PROJECT_NAME} PRIVATE ${G_EXTERNAL_DIR}/curl/lib/Release)
    target_link_libraries(${PROJECT_NAME} PRIVATE libcurl Iphlpapi ws2_32 Wldap32 Crypt32)
elseif (UNIX)
    target_compile_definitions(${PROJECT_NAME} PRIVATE -DCURL_STATICLIB)
    # On Linux libcurl static archive is named libcurl.a; we also need the system
    # libs that curl historically depends on (ssl/crypto are provided by openssl
    # in CMAKE_BINARY_DIR/lib if the bundled curl is configured with TLS).
    target_link_libraries(${PROJECT_NAME} PRIVATE curl pthread dl)
endif ()
