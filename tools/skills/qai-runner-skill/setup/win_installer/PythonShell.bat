@echo off
REM =====================================================================
REM PythonShell.bat
REM Activate the x64 Python 3.10 virtual environment for AIPC
REM =====================================================================
REM
REM This script activates the x86_64 Python 3.10 virtual environment
REM created by Setup_Env.bat, allowing you to directly use
REM the python command within the venv context.
REM
REM Usage:
REM   PythonShell.bat          - Activate venv in current window
REM   PythonShell.bat --new    - Open a new cmd window with venv activated
REM =====================================================================

setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "VENV_310_DIR=%ROOT_DIR%venv\.venv_x64_310"
set "ACTIVATE_SCRIPT=%VENV_310_DIR%\Scripts\activate.bat"

REM -- Validate virtual environment exists ----------------------------------
if not exist "%VENV_310_DIR%" (
    echo.
    echo [ERROR] Virtual environment directory not found:
    echo         %VENV_310_DIR%
    echo.
    echo [INFO]  Please run Setup_Env.bat first to create the environment.
    echo.
    pause
    exit /b 1
)

if not exist "%VENV_310_DIR%\Scripts\python.exe" (
    echo.
    echo [ERROR] Python executable not found in virtual environment:
    echo         %VENV_310_DIR%\Scripts\python.exe
    echo.
    echo [INFO]  The virtual environment may be corrupted.
    echo [INFO]  Please re-run Setup_Env.bat to recreate it.
    echo.
    pause
    exit /b 1
)

if not exist "%ACTIVATE_SCRIPT%" (
    echo.
    echo [ERROR] Activation script not found:
    echo         %ACTIVATE_SCRIPT%
    echo.
    echo [INFO]  The virtual environment may be incomplete.
    echo [INFO]  Please re-run Setup_Env.bat to recreate it.
    echo.
    pause
    exit /b 1
)

REM -- Check for --new flag to open in new window ---------------------------
if /i "%~1"=="--new" (
    echo [INFO] Opening new command window with x64 Python 3.10 venv activated...
    start "AIPC - Python 3.10 x64" cmd /k "cd /d "%ROOT_DIR%" && "%ACTIVATE_SCRIPT%""
    exit /b 0
)

REM -- Activate in current window -------------------------------------------
endlocal
echo.
echo [INFO] Activating x64 Python 3.10 virtual environment...
echo [INFO] Venv: %~dp0venv\.venv_x64_310
echo [INFO] Type 'deactivate' to exit the virtual environment.
echo.

cd /d "%~dp0"
call "%~dp0venv\.venv_x64_310\Scripts\activate.bat"

REM Keep the shell open with venv activated
cmd /k
