#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

include(dependents.cmake)

list(APPEND INCLUDE_PATH
        ${EXTERNAL_HEADER_PATH}
        ${CMAKE_CURRENT_SOURCE_DIR}/src/utils
)

list(APPEND SERVICE_SOURCES
        src/chat_request_handler/chat_request_handler.cpp
        src/chat_history/chat_history.cpp
        src/model/model_manager.cpp
        src/context/qnn/genie.cpp
        src/context/qnn/genie_interface.cpp
        src/context/qnn/phi4mm/phi4mm.cpp
        src/context/qnn/qwen2_5/qwen_2_5.cpp
        src/context/qnn/qwen2_5_omini/qwen_2_5_omini.cpp
        src/context/context_base.cpp
        src/GenieAPIService.cpp
        src/processor/harmony.cpp
        src/processor/general.cpp
        src/response/response_tools.cpp
        src/response/response_dispatcher.cpp
        ${CMAKE_SOURCE_DIR}/src/common/utils.cpp
)


add_compile_options(
        -DQAI_APP_BUILDER_MAJOR_VERSION=${QAI_APP_BUILDER_MAJOR_VERSION}
        -DQAI_APP_BUILDER_MINOR_VERSION=${QAI_APP_BUILDER_MINOR_VERSION}
        -DQAI_APP_BUILDER_PATCH_VERSION=${QAI_APP_BUILDER_PATCH_VERSION}
)

if (USE_MNN)
    add_definitions(-DUSE_MNN)
    list(APPEND SERVICE_SOURCES
            src/context/mnn.cpp
    )
endif ()

if (USE_GGUF)
    add_definitions(-DUSE_GGUF)
    list(APPEND SERVICE_SOURCES
            src/context/llama_cpp.cpp
    )
endif ()

# DLL wrapper sources
list(APPEND DLL_SOURCES
        ${SERVICE_SOURCES}
        src/GenieAPILibrary.cpp
)

configure_file(
        ${SCRIPT_PATH}/version.rc.in
        ${CMAKE_BINARY_DIR}/version.rc
        @ONLY
)

set(LIB_NAME ${CMAKE_PROJECT_NAME})

if (BUILD_AS_DLL)
    set(LIB_NAME GenieAPILibrary)
    # Build as DLL/SO
    if (MSVC)
        add_library(${LIB_NAME} SHARED ${DLL_SOURCES})
        target_compile_definitions(${LIB_NAME} PRIVATE
                -DGENIEAPISERVICE_EXPORTS
                -DNOMINMAX
                -D_HAS_STD_BYTE=0
                -D_USE_MATH_DEFINES
        )
        # Generate import library and set output name to GenieAPILibrary

    elseif (ANDROID)
        add_library(${LIB_NAME} SHARED ${DLL_SOURCES})
        target_compile_definitions(${LIB_NAME} PRIVATE
                -DGENIEAPISERVICE_EXPORTS
                -DNOMINMAX
                -D_HAS_STD_BYTE=0
                -D_USE_MATH_DEFINES
        )
    elseif (UNIX AND NOT ANDROID)
        # Native Linux only — Android still goes through the elseif(ANDROID)
        # branch above.
        add_library(${LIB_NAME} SHARED ${DLL_SOURCES})
        target_compile_definitions(${LIB_NAME} PRIVATE
                -DGENIEAPISERVICE_EXPORTS
        )
    endif ()

    target_include_directories(${LIB_NAME} PRIVATE ${INCLUDE_PATH})
    target_link_directories(${LIB_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
    target_link_libraries(${LIB_NAME} PRIVATE ${EXTERNAL_LIBS})

    # Add MSVC ARM64 specific platform fixes (only for MSVC ARM64 builds)
    if(MSVC AND (CMAKE_SYSTEM_PROCESSOR MATCHES "ARM64" OR CMAKE_GENERATOR_PLATFORM MATCHES "ARM64"))
        target_compile_options(${LIB_NAME} PRIVATE /FI"${CMAKE_CURRENT_SOURCE_DIR}/src/platform_fix.h")
    endif()


else()
    # Build as EXE / SO (original behavior)
    if (MSVC)
        add_executable(${CMAKE_PROJECT_NAME} ${SERVICE_SOURCES})
    elseif (ANDROID)
        add_library(${CMAKE_PROJECT_NAME} SHARED ${SERVICE_SOURCES})
    elseif (UNIX AND NOT ANDROID)
        # Native Linux: build as a standalone executable.
        add_executable(${CMAKE_PROJECT_NAME} ${SERVICE_SOURCES})
    endif ()

    target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${INCLUDE_PATH})
    if (MSVC)
        target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
                -DNOMINMAX
                -D_HAS_STD_BYTE=0
                -D_USE_MATH_DEFINES
        )
    endif ()
    target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
    target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIBS})

    # Set rpath so the executable can locate its sibling .so files in the same dir
    if (UNIX AND NOT ANDROID)
        set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES
                BUILD_WITH_INSTALL_RPATH TRUE
                INSTALL_RPATH "$ORIGIN"
        )
    endif ()

    # Ensure Libllama.cpp ExternalProject finishes before we link (Linux only)
    if (USE_GGUF AND UNIX AND NOT ANDROID)
        add_dependencies(${CMAKE_PROJECT_NAME} Libllama.cpp)
    endif ()

    # Add MSVC ARM64 specific platform fixes (only for MSVC ARM64 builds)
    if(MSVC AND (CMAKE_SYSTEM_PROCESSOR MATCHES "ARM64" OR CMAKE_GENERATOR_PLATFORM MATCHES "ARM64"))
        target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE /FI"${CMAKE_CURRENT_SOURCE_DIR}/src/platform_fix.h")
    endif()
endif()

set(CONFIG_PATH ${CMAKE_SOURCE_DIR}/../../python/models)

if (UNIX AND NOT ANDROID)
    # -------------------------------------------------------------------------
    # Linux: minimal output — only the files the user actually needs to
    # deploy (target board already has QAIRT SDK installed).
    # -------------------------------------------------------------------------
    add_custom_command(
            TARGET ${LIB_NAME} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E echo "[post-build] copying GenieAPIService + libappbuilder.so ..."
            COMMAND ${CMAKE_COMMAND} -E make_directory ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy ${LIBAPPBUILDER_ROOT}/lib/libappbuilder.so ${BUILD_PATH}/
            COMMAND ${CMAKE_COMMAND} -E echo "[post-build] done. Output: ${BUILD_PATH}"
    )
    if (USE_GGUF)
        # Copy only the SONAME versioned libraries (lib*.so.0) that are needed
        # at runtime.  The unversioned lib*.so symlinks are only required at
        # link-time and are intentionally omitted to keep the output clean.
        # cp -L dereferences symlinks so we always get real files in the output.
        add_custom_command(
                TARGET ${LIB_NAME} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E echo "[post-build] copying llama.cpp shared libraries ..."
                COMMAND sh -c "cp -L ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/lib*.so.0 ${BUILD_PATH}/"
        )
    endif ()
else ()
    # -------------------------------------------------------------------------
    # Windows / Android: full package (SDK libs + config templates) for
    # standalone deployment where the target may not have SDK installed.
    # -------------------------------------------------------------------------
    add_custom_command(
            TARGET ${LIB_NAME} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E echo start to copy binray....
            COMMAND ${CMAKE_COMMAND} -E copy ${EXTERNAL_BIN} ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_directory ${CONFIG_PATH} ${BUILD_PATH}/config
            COMMAND ${CMAKE_COMMAND} -E copy ${BUILD_VERSION_FILE} -t ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E echo compile successfully!
    )
endif ()

message(${CMAKE_CURRENT_SOURCE_DIR}/src/GenieAPIService/src/GenieAPILibrary.h)
if (BUILD_AS_DLL)
        add_custom_command(
                TARGET ${LIB_NAME} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E make_directory ${BUILD_PATH}/include
                COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_SOURCE_DIR}/src/GenieAPILibrary.h -t ${BUILD_PATH}/include
        )
endif()
