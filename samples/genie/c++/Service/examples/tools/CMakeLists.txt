#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

project(Tools)

add_executable(decode
        base64_decode.cpp
)

add_executable(encode
        base64_encode.cpp
)

add_executable(wav
        wav_recorder.cpp
)

set_target_properties(decode PROPERTIES RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH}/tools)
set_target_properties(encode PROPERTIES RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH}/tools)
set_target_properties(wav PROPERTIES RUNTIME_OUTPUT_DIRECTORY_RELEASE ${BUILD_PATH}/tools)
