#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

project(GenieAPIClient)

include(ExternalProject)

# The vendored curl source (External/curl) ships no prebuilt library; build a
# minimal static libcurl (HTTP-only, no TLS/LDAP/SSH2) from it here, since this
# client only ever talks plain HTTP to the local service.
set(CURL_INSTALL_DIR ${CMAKE_BINARY_DIR}/curl-install)
ExternalProject_Add(Libcurl
        SOURCE_DIR ${G_EXTERNAL_DIR}/curl
        BINARY_DIR ${CMAKE_BINARY_DIR}/curl-build
        CMAKE_ARGS
        -DCMAKE_INSTALL_PREFIX=${CURL_INSTALL_DIR}
        -DCMAKE_BUILD_TYPE=Release
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DBUILD_SHARED_LIBS=OFF
        -DBUILD_CURL_EXE=OFF
        -DBUILD_TESTING=OFF
        -DCURL_ENABLE_SSL=OFF
        -DCURL_DISABLE_LDAP=ON
        -DCURL_USE_LIBPSL=OFF
        -DCURL_USE_LIBSSH2=OFF
        BUILD_IN_SOURCE OFF
)

add_executable(${PROJECT_NAME}
        GenieAPIClient.cpp
        ${CMAKE_SOURCE_DIR}/src/common/utils.cpp
)
add_dependencies(${PROJECT_NAME} Libcurl)
target_include_directories(${PROJECT_NAME} PRIVATE ${EXTERNAL_HEADER_PATH} ${CURL_INSTALL_DIR}/include)
target_link_directories(${PROJECT_NAME} PRIVATE ${CURL_INSTALL_DIR}/lib)

if (MSVC)
    target_compile_definitions(${PROJECT_NAME} PRIVATE -DCURL_STATICLIB -DNOMINMAX)
    target_link_libraries(${PROJECT_NAME} PRIVATE libcurl Iphlpapi ws2_32 Wldap32 Crypt32)
elseif (UNIX)
    target_compile_definitions(${PROJECT_NAME} PRIVATE -DCURL_STATICLIB)
    target_link_libraries(${PROJECT_NAME} PRIVATE curl pthread dl)
endif ()
