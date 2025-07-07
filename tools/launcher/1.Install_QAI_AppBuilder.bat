@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
set "currentDir=%CD%"

if exist "tools" (
    echo tools directory already exists
) else (
    echo Creating tools directory...
    mkdir tools\Git
)

:CheckVC
REM Install Visual C++ Redistributable
echo Checking Visual C++ Redistributable...
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" >nul 2>&1
if %errorlevel%==0 (
    echo Visual C++ Redistributable is already installed. Skipping installation.
    goto InstallQAI
)
echo Installing Visual C++ Redistributable...
powershell -Command "Invoke-WebRequest -Uri https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile vc_redist.x64.exe"
start /wait vc_redist.x64.exe /quiet /norestart
del vc_redist.x64.exe

:InstallQAI
powershell -ExecutionPolicy Bypass -File "%currentDir%\utils\Install_QAI_AppBuilder.ps1" "%currentDir%"
