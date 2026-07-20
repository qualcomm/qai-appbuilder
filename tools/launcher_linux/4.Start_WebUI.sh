#!/bin/bash
# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------


# 接收脚本路径参数
scriptPath=$(pwd)

# 检查 Pixi 是否安装
if ! command -v pixi &> /dev/null; then
    echo "Pixi is not installed. Please run 1.Setup_QAI_AppBuilder.sh first."
    read -p "Press Enter to exit..."
    exit 1
fi

# 切换到 env 目录
cd "$scriptPath/env" || exit

# 显示菜单
echo "Please choose which WebUI to launch:"
echo "1. Start ImageRepairApp"
echo "2. Start GenieWebUI"
read -p "Enter the number (1-2) corresponding to your choice: " choice



case "$choice" in
    1)
        echo "Launching ImageRepairApp ..."
         # Add SOC ID Select Menu
        echo ""
        echo "Please choose the SOC ID:"
        echo "1. wos (default)"
        echo "2. 9075"
        echo "3. 6490"
        read -p "Enter the number (1-3) corresponding to your choice: " soc_choice
        
        # set SOC ID
        case "$soc_choice" in
            1)
                soc_id="wos"
                ;;
            2)
                soc_id="9075"
                ;;
            3)
                soc_id="8550"
                ;;
            *)
                echo "Invalid SOC ID choice. Using default (wos)."
                soc_id="wos"
                ;;
        esac
        
        echo "Starting ImageRepairApp with SOC ID: $soc_id"
        pixi run webui-imagerepair --soc_id "$soc_id"
        ;;
    2)
        echo "Launching GenieWebUI ..."
        pixi run webui-genie
        ;;
    *)
        echo "Unaccepted option. Please run the script again and choose a valid option."
        read -p "Press Enter to exit..."
        cd "$scriptPath" || exit
        exit 1
        ;;
esac

# 返回原始目录
cd "$scriptPath" || exit
