@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
set "currentDir=%CD%"

powershell -ExecutionPolicy Bypass -File "%currentDir%\utils\Start_WebUI.ps1" "%currentDir%"

if %ERRORLEVEL%==0 (
    echo Success.
) else (
    echo Failed: %ERRORLEVEL%
)

pause
