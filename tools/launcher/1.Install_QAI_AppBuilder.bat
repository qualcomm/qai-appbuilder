@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
set "currentDir=%CD%"

@REM where pwsh >nul 2>&1
@REM if errorlevel 1 (
@REM 	echo Error: 'pwsh' (PowerShell Core) is not installed or not in PATH.
@REM 	exit /b 1
@REM )

REM Check if PowerShell is installed

pwsh --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    echo PowerShell is already installed. Skipping installation.
) ELSE (
    echo PowerShell is not installed. Installing PowerShell...
    winget install --id Microsoft.PowerShell
)

set PATH=%PATH%;"C:\Program Files\PowerShell\7\"

:CheckVC
REM Step 3: Install Visual C++ Redistributable
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
pwsh -ExecutionPolicy Bypass -File "%currentDir%\utils\Install_QAI_AppBuilder.ps1" "%currentDir%"
