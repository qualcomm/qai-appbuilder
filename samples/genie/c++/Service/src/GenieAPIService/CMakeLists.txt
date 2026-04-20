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
    # Build as DLL
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
    endif ()
    
    target_include_directories(${LIB_NAME} PRIVATE ${INCLUDE_PATH})
    target_link_directories(${LIB_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
    target_link_libraries(${LIB_NAME} PRIVATE ${EXTERNAL_LIBS})
    
    # Add platform-specific fixes for ARM64
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "ARM64" OR CMAKE_GENERATOR_PLATFORM MATCHES "ARM64")
        target_compile_options(${LIB_NAME} PRIVATE /FI"${CMAKE_CURRENT_SOURCE_DIR}/src/platform_fix.h")
    endif()
    
    
else()
    # Build as EXE (original behavior)
    if (MSVC)
        add_executable(${CMAKE_PROJECT_NAME} ${SERVICE_SOURCES})
    elseif (ANDROID)
        add_library(${CMAKE_PROJECT_NAME} SHARED ${SERVICE_SOURCES})
    endif ()
    
    target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${INCLUDE_PATH})
    target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
            -DNOMINMAX
            -D_HAS_STD_BYTE=0
            -D_USE_MATH_DEFINES
    )
    target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
    target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIBS})
    
    # Add platform-specific fixes for ARM64
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "ARM64" OR CMAKE_GENERATOR_PLATFORM MATCHES "ARM64")
        target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE /FI"${CMAKE_CURRENT_SOURCE_DIR}/src/platform_fix.h")
    endif()
endif()

set(CONFIG_PATH ${CMAKE_SOURCE_DIR}/../../python/models)
add_custom_command(
        TARGET ${LIB_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E echo start to copy binray....
        COMMAND ${CMAKE_COMMAND} -E copy ${EXTERNAL_BIN} ${BUILD_PATH}
        COMMAND ${CMAKE_COMMAND} -E copy_directory ${CONFIG_PATH} ${BUILD_PATH}/config
        COMMAND ${CMAKE_COMMAND} -E copy ${BUILD_VERSION_FILE} -t ${BUILD_PATH}
        COMMAND ${CMAKE_COMMAND} -E echo compile successfully!
)

message(${CMAKE_CURRENT_SOURCE_DIR}/src/GenieAPIService/src/GenieAPILibrary.h)
if (BUILD_AS_DLL)
        add_custom_command(
                TARGET ${LIB_NAME} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E make_directory ${BUILD_PATH}/include
                COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_SOURCE_DIR}/src/GenieAPILibrary.h -t ${BUILD_PATH}/include
        )
endif()


