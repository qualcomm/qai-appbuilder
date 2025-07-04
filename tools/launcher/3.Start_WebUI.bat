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

pwsh -ExecutionPolicy Bypass -File "%currentDir%\utils\Start_WebUI.ps1" "%currentDir%"
