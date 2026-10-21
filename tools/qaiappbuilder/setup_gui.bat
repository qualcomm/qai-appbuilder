@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM setup_gui.bat - opens a small setup window (scripts\setup\setup_gui.ps1) to
REM pick the optional components / proxy / mirror for setup_env.bat and
REM build_frontend.bat, then runs them and shows the log.
REM
REM Usage: double-click, or  setup_gui.bat [--console]
REM   --console   keep the PowerShell console visible (for troubleshooting)

setlocal
cd /d "%~dp0"

REM Pre-flight: WinForms must be loadable (fails under WDAC constrained language
REM mode). Checked here, visibly, so a failure is not swallowed by a hidden window.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { exit 3 }; Add-Type -AssemblyName System.Windows.Forms" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The setup window cannot start here ^(PowerShell / WinForms unavailable,
    echo [ERROR] e.g. a WDAC / AppLocker constrained-language policy^).
    echo [ERROR] Run the installer from the command line instead:
    echo [ERROR]     setup_env.bat --help
    pause
    exit /b 1
)

if /i "%~1"=="--console" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0scripts\setup\setup_gui.ps1"
    exit /b %errorlevel%
)
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0scripts\setup\setup_gui.ps1"
exit /b 0
