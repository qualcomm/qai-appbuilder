
#!/bin/bash

# 获取当前目录
currentDir=$(pwd)

echo "Start install langflow..."

# 切换到 env 目录
cd env || exit

# 检查 langflow 是否已安装
pixi run "langflow -v"
if [ $? -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Langflow has been installed"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') Langflow is not installed"
    pixi install --tls-no-verify --frozen
    pixi run install-langflow
fi

# 暂停（Linux 下用 read 替代 pause）
read -p "Press Enter to continue..."
