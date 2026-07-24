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
        src/chat_request_handler/prompt_optimizer.cpp
        src/chat_request_handler/message_pre_filter.cpp
        src/chat_request_handler/prompt_preparation_service.cpp
        src/chat_request_handler/summary_cache.cpp
        src/chat_request_handler/long_text_summarizer.cpp
        src/chat_history/chat_history.cpp
        src/model/model_manager.cpp
        src/context/qnn/genie.cpp
        src/context/qnn/genie_interface.cpp
        src/context/qnn/phi4mm/phi4mm.cpp
        src/context/qnn/qwen2_5/qwen_2_5.cpp
        src/context/qnn/qwen2_5_omini/qwen_2_5_omini.cpp
        src/context/qnn/qwen3_vl/qwen_3_vl.cpp
        src/context/context_base.cpp
        src/GenieAPIService.cpp
        src/port_available.cpp
        src/processor/harmony.cpp
        src/processor/general.cpp
        src/response/response_tools.cpp
        src/response/response_dispatcher.cpp
        ${CMAKE_SOURCE_DIR}/src/common/utils.cpp
        src/gateway/gateway/gateway.cpp
        src/gateway/gateway/gateway_routing.cpp
        src/gateway/gateway/gateway_session.cpp
        src/gateway/gateway/gateway_history.cpp
        src/gateway/gateway/gateway_cloud.cpp
        src/gateway/gateway/gateway_steps.cpp
        src/gateway/gateway/gateway_incremental.cpp
        src/gateway/gateway/gateway_overflow.cpp
        src/gateway/security/content_security_inspector.cpp
        src/gateway/security/desensitizer.cpp
        src/gateway/security/task_complexity_evaluator.cpp
        src/gateway/routing/model_router.cpp
        src/gateway/cloud/cloud_model_client.cpp
        src/gateway/audit/audit_logger.cpp
)

add_compile_options(
        -DQAI_APP_BUILDER_MAJOR_VERSION=${QAI_APP_BUILDER_MAJOR_VERSION}
        -DQAI_APP_BUILDER_MINOR_VERSION=${QAI_APP_BUILDER_MINOR_VERSION}
        -DQAI_APP_BUILDER_PATCH_VERSION=${QAI_APP_BUILDER_PATCH_VERSION}
)

if (MSVC AND USE_MNN)
    add_definitions(-DUSE_MNN)
    list(APPEND SERVICE_SOURCES
            src/context/mnn.cpp
    )
endif ()

if (MSVC AND USE_GGUF)
    add_definitions(-DUSE_GGUF)
    list(APPEND SERVICE_SOURCES
            src/context/llama_cpp.cpp
    )
endif ()

if (MSVC)
    configure_file(
            ${SCRIPT_PATH}/version.rc.in
            ${CMAKE_BINARY_DIR}/version.rc
            @ONLY
    )
endif ()

if (MSVC)
    add_executable(${CMAKE_PROJECT_NAME} ${SERVICE_SOURCES})
elseif (UNIX)
    add_executable(${CMAKE_PROJECT_NAME} ${SERVICE_SOURCES})
endif ()

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${INCLUDE_PATH})
if (MSVC)
    target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
            NOMINMAX
            _HAS_STD_BYTE=0
            _USE_MATH_DEFINES
    )
endif ()
target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE ${EXTERNAL_LIBS})
add_dependencies(${CMAKE_PROJECT_NAME} Libsamplerate)
add_dependencies(${CMAKE_PROJECT_NAME} Libappbuilder)
if (UNIX)
    # cpp-httplib in TLS mode pulls in <mbedtls/*.h>, so the mbedTLS headers must
    # exist (i.e. the ExternalProject download step must have run) before any TU
    # is compiled.
    add_dependencies(${CMAKE_PROJECT_NAME} Libmbedtls)
endif ()
if (MSVC AND USE_MNN)
    add_dependencies(${CMAKE_PROJECT_NAME} Libmnn)
endif ()
if (MSVC AND USE_GGUF)
    add_dependencies(${CMAKE_PROJECT_NAME} Libllama.cpp)
    add_dependencies(${CMAKE_PROJECT_NAME} AdrenoOpenCLKernels)
endif ()

if (UNIX)
    set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN"
    )
endif ()

if (MSVC)
    target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE /Zi)
    target_link_options(${CMAKE_PROJECT_NAME} PRIVATE /DEBUG)
endif ()

if (BUILD_AS_DLL)
    # BUILD_AS_DLL=ON 把 exe 排除出默认 ALL 目标，只单独构建 dll。
    set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES EXCLUDE_FROM_ALL TRUE)
endif ()

set(LIB_NAME GenieAPILibrary)
list(APPEND DLL_SOURCES
        ${SERVICE_SOURCES}
        src/GenieAPILibrary.cpp
)

add_library(${LIB_NAME} SHARED ${DLL_SOURCES})
target_compile_definitions(${LIB_NAME} PRIVATE GENIEAPISERVICE_EXPORTS)
if (MSVC)
    target_compile_definitions(${LIB_NAME} PRIVATE
            NOMINMAX
            GENIEAPI_EXPORTS
            _HAS_STD_BYTE=0
            _USE_MATH_DEFINES
    )
endif ()

target_include_directories(${LIB_NAME} PRIVATE ${INCLUDE_PATH})
target_link_directories(${LIB_NAME} PRIVATE ${EXTERNAL_LIB_PATH})
target_link_libraries(${LIB_NAME} PRIVATE ${EXTERNAL_LIBS})
add_dependencies(${LIB_NAME} Libsamplerate)
add_dependencies(${LIB_NAME} Libappbuilder)
if (UNIX)
    add_dependencies(${LIB_NAME} Libmbedtls)
endif ()
if (MSVC AND USE_MNN)
    add_dependencies(${LIB_NAME} Libmnn)
endif ()
if (MSVC AND USE_GGUF)
    add_dependencies(${LIB_NAME} Libllama.cpp)
    add_dependencies(${LIB_NAME} AdrenoOpenCLKernels)
endif ()

if (UNIX)
    set_target_properties(${LIB_NAME} PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN"
    )
endif ()

if (MSVC)
    target_compile_options(${LIB_NAME} PRIVATE /Zi)
    target_link_options(${LIB_NAME} PRIVATE /DEBUG)
endif ()

add_custom_command(
        TARGET ${LIB_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory ${BUILD_PATH}/include
        COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_SOURCE_DIR}/src/GenieAPILibrary.h ${BUILD_PATH}/include
)
if (BUILD_AS_DLL)
    # 保证只构建 ${LIB_NAME}（不连带构建 ${CMAKE_PROJECT_NAME}）时，EXTERNAL_BIN 里的后端
    # dll（包括升级第三方依赖时新增的条目，如 llama-common.dll）也能同步拷贝到 BUILD_PATH，
    # 不再依赖额外构建一次 exe target 才能补齐。BUILD_AS_DLL=OFF 时 exe 与 dll 同时构建，
    # 这份拷贝已由 ${CMAKE_PROJECT_NAME} 的 POST_BUILD 覆盖，此处按需省略以避免两个 target
    # 并行拷贝同一份文件产生写入竞争。
    add_custom_command(
            TARGET ${LIB_NAME} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${EXTERNAL_BIN} ${BUILD_PATH}
    )
endif ()

set(CONFIG_PATH ${CMAKE_SOURCE_DIR}/../../python/models)
if (MSVC)
    add_custom_command(
            TARGET ${CMAKE_PROJECT_NAME} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${EXTERNAL_BIN} ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_directory ${CONFIG_PATH} ${BUILD_PATH}/config
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                ${CMAKE_CURRENT_SOURCE_DIR}/../config/service_config.json
                ${CMAKE_CURRENT_SOURCE_DIR}/../config/sensitive_keywords.json
                ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                ${CMAKE_CURRENT_SOURCE_DIR}/../../test/test_service.py
                ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${BUILD_VERSION_FILE} ${BUILD_PATH}
    )
elseif (UNIX)
    add_custom_command(
            TARGET ${CMAKE_PROJECT_NAME} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${EXTERNAL_BIN} ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_directory ${CONFIG_PATH} ${BUILD_PATH}/config
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                ${CMAKE_CURRENT_SOURCE_DIR}/../config/service_config.json
                ${CMAKE_CURRENT_SOURCE_DIR}/../config/sensitive_keywords.json
                ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                ${CMAKE_CURRENT_SOURCE_DIR}/../../test/test_service.py
                ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${BUILD_VERSION_FILE} ${BUILD_PATH}
    )
endif ()
if (BUILD_AS_DLL)
    # BUILD_AS_DLL=ON 时 exe 不构建，同样的打包也得挂到 ${LIB_NAME} 上，才能得到完整产物目录。
    # BUILD_AS_DLL=OFF 时省略此块，避免与上面 ${CMAKE_PROJECT_NAME} 的 POST_BUILD 并行拷贝
    # 同一份文件产生写入竞争。
    add_custom_command(
            TARGET ${LIB_NAME} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_directory ${CONFIG_PATH} ${BUILD_PATH}/config
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                ${CMAKE_CURRENT_SOURCE_DIR}/../config/service_config.json
                ${CMAKE_CURRENT_SOURCE_DIR}/../config/sensitive_keywords.json
                ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                ${CMAKE_CURRENT_SOURCE_DIR}/../../test/test_service.py
                ${BUILD_PATH}
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${BUILD_VERSION_FILE} ${BUILD_PATH}
    )
endif ()
