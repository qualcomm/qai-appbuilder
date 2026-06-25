#!/bin/bash
# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------


# 切换到当前脚本所在目录
currentDir=$(pwd)
cd "$currentDir" || exit

echo "Starting installation..."

echo "Installing large language model..."
cd env || exit

# 执行 pixi 命令
if [ -f "$currentDir/qai-appbuilder/samples/genie/python/models/IBM-Granite-v3.1-8B/weight_sharing_model_4_of_5.serialized.bin" ]; then
    echo "LLM Models already exists, skipping download..."
else
    echo "Download LLM Models..."
    pixi run install-model
fi

status=$?

echo

if [ $status -eq 0 ]; then
    echo "Install successfully. Press Enter to exit..."
else
    echo "Install failed, exit code: $status"
fi

# 返回原始目录
cd "$currentDir" || exit

