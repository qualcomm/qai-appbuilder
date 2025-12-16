
#!/bin/bash

scriptPath=$(pwd)

# 检查并停止 langflow 进程
langflow_pid=$(pgrep -f "langflow")
if [ -n "$langflow_pid" ]; then
    echo "Stopping LangFlow process..."
    kill -9 "$langflow_pid"
fi

# 检查 GenieAPIService 是否运行
genie_pid=$(pgrep -f "Genie")
if [ -n "$genie_pid" ]; then
    echo "GenieAPIService is ready now."
else
    echo "Cannot find GenieAPIService. Please run [4.Start_GenieAPIService.sh] before launching LangFlow!"
    read -p "Press Enter to exit..."
    exit 1
fi

# 后台检测服务是否启动并打开浏览器
(
    maxAttempts=60
    attempt=0
    interval=2
    urlOpened=false

    while [ $attempt -lt $maxAttempts ]; do
        if nc -z 127.0.0.1 8979 2>/dev/null; then
            if [ "$urlOpened" = false ]; then
                echo "LangFlow is ready now. Opening the browser..."
                xdg-open "http://127.0.0.1:8979" >/dev/null 2>&1
                urlOpened=true
                exit 0
            fi
        else
            attempt=$((attempt + 1))
            echo "Waiting for the service to start... Attempt $attempt of $maxAttempts"
            sleep $interval
        fi
    done

    if [ $attempt -eq $maxAttempts ]; then
        echo "Failed to detect the service readiness after $maxAttempts attempts."
    fi
) &

# 检查 Pixi 是否安装
if ! command -v pixi &> /dev/null; then
    echo "Pixi is not installed. Please install Pixi first."
    exit 1
else
    echo "Pixi is already installed."
fi

# 切换到 env 目录并启动 LangFlow
cd "$scriptPath/env" || exit
pixi run langflow run --host 0.0.0.0 --port 8979
cd "$scriptPath" || exit
