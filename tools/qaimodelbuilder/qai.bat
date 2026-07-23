@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM qai.bat - convenience wrapper for the unified "qai" command-line tool.
REM
REM After Setup.bat has installed the environment, the "qai" console
REM script lives in the host-arch runtime venv at
REM   %LOCALAPPDATA%\QAIModelBuilder\envs\<.venv_arm64_313|.venv_x64_313>\Scripts\qai.exe
REM
REM This wrapper lets you run it from the project directory WITHOUT activating
REM the venv or typing the full path:
REM
REM   qai --help
REM   qai config provider list
REM   qai app whisper-base --audio clip.wav
REM   qai build
REM
REM All arguments are forwarded verbatim to qai.exe. The wrapper also injects
REM PortableGit onto PATH (when present) so CLI commands that shell out to git
REM behave the same as Start.bat. Exit code is propagated from qai.exe.

setlocal EnableDelayedExpansion

REM --- Host architecture selection (three-tier) -----------------------------
REM Priority: 1) --arch <value> CLI flag  2) data\config\host_arch file
REM           3) Auto-detect via %PROCESSOR_ARCHITECTURE% / PROCESSOR_ARCHITEW6432
REM PASS_ARGS is the arg list with any --arch <value> pair removed so it can
REM be forwarded to qai.exe without leaking the flag.
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
set "QAI_EXE=%VENV%\Scripts\qai.exe"

if not exist "%QAI_EXE%" (
    echo [ERROR] qai is not installed yet.
    echo [ERROR] Expected: %QAI_EXE%
    echo [ERROR] Run Setup.bat first to create the environment and install
    echo [ERROR] the "qai" command-line tool.
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

"%QAI_EXE%" %PASS_ARGS%
exit /b %ERRORLEVEL%
