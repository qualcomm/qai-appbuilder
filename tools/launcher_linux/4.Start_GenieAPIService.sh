scriptPath=$(pwd)

# 设置环境变量
# export QNN_SDK_ROOT=/home/ubuntu/code/2.38.0.250901
# export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
# export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v73/unsigned
# 检查 GenieAPIService 是否运行
genie_pid=$(pgrep -f "Genie")
if [ -n "$genie_pid" ]; then
    echo "GenieAPIService is ready now."
else
    echo "run GenieAPIService"
    cd "$scriptPath/env" || exit
    pixi run start-GenieAPIService
    cd "$scriptPath" || exit
    read -p "Press Enter to exit..."
    exit 1
fi