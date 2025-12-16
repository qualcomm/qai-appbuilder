
#!/bin/bash

# 接收脚本路径参数
scriptPath=$(pwd)

set -e

# 获取当前脚本所在的目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"


source "${SCRIPT_DIR}/check_env.sh"

check_qnn_sdk_root

echo "QNN SDK check passed. Continuing with installation..."


# 检查 pixi 是否安装
if ! command -v pixi &> /dev/null; then
    echo "Installing Pixi..."
    # 使用官方安装脚本
    curl -fsSL https://pixi.sh/install.sh | bash
    # 刷新环境变量
    source ~/.bashrc
else
    echo "Pixi is already installed."
fi

echo Installing tools...
cd env
pixi run install-tools
cd ..


# 检查 pixi 是否安装
if command -v pixi &> /dev/null; then
    cd "$scriptPath/env" || exit
    
    if [ -f "$scriptPath/ai-engine-direct-helper/dist/qai_appbuilder-2.38.0-cp312-cp312-linux_aarch64.whl" ]; then
        echo "Wheel file already exists, skipping build..."
    else
        echo "Building QAI AppBuilder..."
        pixi run build-qai-appbuilder
    fi

    pixi run install-qai-appbuilder
    cd "$scriptPath" || exit
else
    echo "pixi is not installed. Please install pixi first."
    exit 1
fi

# 检查执行结果
if [ $? -ne 0 ]; then
    echo "Installation failed. Please try again."
    read -p "Press Enter to exit..."
    cd "$scriptPath" || exit
    exit 1
fi

echo "Install successfully."
read -p "Press Enter to exit..."
cd "$scriptPath" || exit