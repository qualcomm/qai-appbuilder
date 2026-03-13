@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
cd /d "%~dp0"
set "currentDir=%CD%"

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

cd env
@REM pixi update
pixi run webui-stable-diffusion

pause
