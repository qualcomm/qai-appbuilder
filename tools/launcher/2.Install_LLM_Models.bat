@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
cd /d "%~dp0"
set "currentDir=%CD%"

setlocal enabledelayedexpansion
echo Starting installation...

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

echo Installing large language model...
cd env
pixi run install-model

echo(

if %ERRORLEVEL%==0 (
    echo Install successfully. Press any key to exit...
) else (
    echo Install failed, exit code: %ERRORLEVEL%
)

pause
