@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
cd /d "%~dp0"
set "currentDir=%CD%"

setlocal enabledelayedexpansion
echo Start install langflow...

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

if exist "tools" (
    echo tools directory already exists
) else (
    echo Creating tools directory...
    mkdir tools
)

:Install Visual Studio
powershell -ExecutionPolicy Bypass -File "%currentDir%\utils\Install_Visual_Studio.ps1" "%currentDir%"

cd env
@REM pixi update
pixi run "langflow -v"

if %ERRORLEVEL% equ 0 (
    echo [%date% %time%] Langflow has been installed
    echo Langflow has been installed
) else (
    echo [%date% %time%] Langflow is not installed
    pixi install --tls-no-verify --frozen
    pixi run install-langflow
)
pause
