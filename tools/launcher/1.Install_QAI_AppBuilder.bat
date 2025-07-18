@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
cd /d "%~dp0"
set "currentDir=%CD%"

if exist "tools" (
    echo The tools directory already exists
) else (
    echo Creating tools directory...
    mkdir tools\Git
)

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

set "downloadUrl=https://github.com/minnyres/aria2-windows-arm64/releases/download/v1.37.0/aria2_1.37.0_arm64.zip"
set "downloadPath=%currentDir%\tools\aria2c.zip"
set "extractPath=%currentDir%\tools\aria2c"

if exist "tools\pixi\pixi.exe" (
    echo pixi exist
) else (
    echo Installing aria2c

    powershell -Command "Invoke-WebRequest -Uri %downloadUrl% -OutFile '%downloadPath%'"

    if exist "%downloadPath%" (
        mkdir "%extractPath%"
        powershell -Command "Expand-Archive -Path '%downloadPath%' -DestinationPath '%extractPath%' -Force"
        if %errorlevel% equ 0 (
            echo Extract to %extractPath%
        ) else (
            echo Extraction failed.
        )
    ) else (
        echo Download failed.
    )

    echo Installing pixi

    aria2c --console-log-level=error -x 16 -s 16 -o "pixi.zip" -d "%currentDir%\tools" "https://github.com/prefix-dev/pixi/releases/download/v0.49.0/pixi-aarch64-pc-windows-msvc.zip"
    powershell -Command "Expand-Archive -Path '%currentDir%\tools\pixi.zip' -DestinationPath '%currentDir%\tools\pixi' -Force"
)

echo Installing tools...
cd env
pixi run install-tools
cd ..

:Install_QAI_AppBuilder
powershell -ExecutionPolicy Bypass -File "%currentDir%\utils\Install_QAI_AppBuilder.ps1" "%currentDir%"
