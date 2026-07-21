@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM qai.bat - convenience wrapper for the unified "qai" command-line tool.
REM
REM After Setup.bat has installed the environment, the "qai" console
REM script lives in the ARM64 venv at
REM   %LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313\Scripts\qai.exe
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

setlocal

set "VENV=%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313"
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

"%QAI_EXE%" %*
exit /b %ERRORLEVEL%
