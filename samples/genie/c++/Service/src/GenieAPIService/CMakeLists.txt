#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

include(platform.cmake)

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

configure_file(
        ${SCRIPT_PATH}/version.rc.in
        ${CMAKE_BINARY_DIR}/version.rc
        @ONLY
)

add_executable(${CMAKE_PROJECT_NAME} ${SERVICE_SOURCES})
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${INCLUDE_PATH})
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE -DNOMINMAX -DBUILDING_LIBCURL -D_HAS_STD_BYTE=0)
target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIBS})

set(CONFIG_PATH ${CMAKE_SOURCE_DIR}/../../python/models)
add_custom_command(
        TARGET ${CMAKE_PROJECT_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E echo start to copy binray....
        COMMAND ${CMAKE_COMMAND} -E copy ${EXTERNAL_BIN} ${BUILD_PATH}
        COMMAND ${CMAKE_COMMAND} -E copy_directory ${CONFIG_PATH} ${BUILD_PATH}/config
        COMMAND ${CMAKE_COMMAND} -E echo compile successfully!
)
