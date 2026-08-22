@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM qai.bat - convenience wrapper for the unified "qai" command-line tool.
REM
REM The runtime venv supplies third-party dependencies. This wrapper loads the
REM current installation's apps/ and src/qai/ through PYTHONPATH, so separate
REM installations never depend on the venv's editable-project state.
REM
REM   qai --help
REM   qai config provider list
REM   qai app whisper-base --audio clip.wav
REM   qai build
REM
REM All arguments are forwarded verbatim. PortableGit is added to PATH when
REM present so CLI commands that shell out to git behave like Start.bat.

setlocal EnableDelayedExpansion
set "ROOT_DIR=%~dp0"

REM --- Host architecture selection (three-tier) -----------------------------
REM Priority: 1) --arch <value> CLI flag  2) data\config\host_arch file
REM           3) Auto-detect via %PROCESSOR_ARCHITECTURE% / PROCESSOR_ARCHITEW6432
REM PASS_ARGS is the arg list with any --arch <value> pair removed so it can
REM be forwarded to apps.cli without leaking the launcher-only flag.
set "FORCED_ARCH="
set "PASS_ARGS="
set "_NEXT_IS_ARCH="
for %%A in (%*) do (
    if defined _NEXT_IS_ARCH (
        set "FORCED_ARCH=%%~A"
        set "_NEXT_IS_ARCH="
    ) else if /i "%%~A"=="--arch" (
        set "_NEXT_IS_ARCH=1"
    ) else (
        set "PASS_ARGS=!PASS_ARGS! %%A"
    )
)
set "HOST_ARCH="
if defined FORCED_ARCH (
    if /i "!FORCED_ARCH!"=="x64"   set "HOST_ARCH=x64"
    if /i "!FORCED_ARCH!"=="arm64" set "HOST_ARCH=arm64"
)
if not defined HOST_ARCH (
    if exist "%~dp0data\config\host_arch" (
        set "_FILE_ARCH="
        for /f "usebackq tokens=1 delims= " %%B in ("%~dp0data\config\host_arch") do (
            if not defined _FILE_ARCH set "_FILE_ARCH=%%B"
        )
        if /i "!_FILE_ARCH!"=="x64"   set "HOST_ARCH=x64"
        if /i "!_FILE_ARCH!"=="arm64" set "HOST_ARCH=arm64"
    )
)
if not defined HOST_ARCH (
    set "HOST_ARCH=arm64"
    if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" set "HOST_ARCH=x64"
    if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "HOST_ARCH=x64"
)
set "VENV_DIR_NAME=.venv_arm64_313"
if /i "%HOST_ARCH%"=="x64" set "VENV_DIR_NAME=.venv_x64_313"
set "VENV=%LOCALAPPDATA%\QAIModelBuilder\envs\%VENV_DIR_NAME%"
set "PYTHON=%VENV%\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Python runtime is not installed yet.
    echo [ERROR] Expected: %PYTHON%
    echo [ERROR] Run Setup.bat first to create the environment.
    exit /b 1
)

REM PortableGit PATH injection (parity with Start.bat) so `qai` subcommands
REM that invoke git find it even on a machine without a system git.
set "PORTABLE_GIT_DIR=%LOCALAPPDATA%\QAIModelBuilder\git"
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" (
    set "PATH=%PORTABLE_GIT_DIR%\cmd;%PORTABLE_GIT_DIR%\usr\bin;%PATH%"
) else if exist "%PORTABLE_GIT_DIR%\bin\git.exe" (
    set "PATH=%PORTABLE_GIT_DIR%\bin;%PORTABLE_GIT_DIR%\usr\bin;%PATH%"
)

set "PYTHONPATH=%ROOT_DIR%src;%ROOT_DIR%"
cd /d "%ROOT_DIR%"
echo [INFO] CLI source root: %ROOT_DIR%
"%PYTHON%" -c "import qai; print('[INFO] CLI qai source: ' + qai.__file__)"
"%PYTHON%" -m apps.cli %PASS_ARGS%
exit /b %ERRORLEVEL%
