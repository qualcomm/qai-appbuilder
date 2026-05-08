#!/bin/bash
# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------


# 切换到 env 目录
cd env

# pixi update (如果需要可以取消注释)
# pixi update

# 启动 pixi shell
pixi shell

# 等待用户按回车
read -p "Press Enter to continue..."
