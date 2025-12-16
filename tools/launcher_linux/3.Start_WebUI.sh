
#!/bin/bash

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

# export QNN_SDK_ROOT=/home/ubuntu/code/2.38.0.250901
# export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
# export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v73/unsigned

case "$choice" in
    1)
        echo "Launching ImageRepairApp ..."
        pixi run webui-imagerepair
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
