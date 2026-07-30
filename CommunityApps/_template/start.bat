@REM ---------------------------------------------------------------------
@REM Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
REM start.bat — one-click launcher for Windows on Snapdragon (WoS).
REM Creates/uses a local venv, installs deps, then runs the app.
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo Starting app...
python main.py

endlocal
pause
