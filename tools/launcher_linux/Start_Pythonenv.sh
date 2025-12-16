
#!/bin/bash

# 切换到 env 目录
cd env

# pixi update (如果需要可以取消注释)
# pixi update

# 启动 pixi shell
pixi shell

# 等待用户按回车
read -p "Press Enter to continue..."
