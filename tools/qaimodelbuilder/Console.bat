@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM QAIModelBuilder - Open an activated environment console
REM
REM Double-click (or run) this to drop into the project's isolated Python
REM 3.13 environment as an interactive shell, so you can install extra
REM packages or run ad-hoc commands without typing the long venv path:
REM   pip install <package>          (standard)
REM   uv pip install <package>       (faster, uses uv)
REM   qai --help                     (the unified CLI is on PATH here too)
REM To permanently record new runtime dependencies, add them to
REM pyproject.toml [project].dependencies (the single source of truth since
REM requirements.txt was removed).

REM NOTE: Do NOT use "chcp 65001" here - it causes cmd.exe to silently drop leading
REM       characters from command output on some Windows versions (known OS bug).

echo.
echo  +--------------------------------------------------+
echo  ^|   QAI ModelBuilder  -  Environment Console       ^|
echo  +--------------------------------------------------+
echo.
echo  [INFO] Entering isolated Python 3.13 environment...
echo  [INFO] Install packages:  pip install ^<package^>
echo  [INFO]                or: uv pip install ^<package^>
echo  [INFO] Run the CLI:       qai --help
echo  [INFO] Exit console:      exit
echo.

set "ROOT_DIR=%~dp0"
setlocal EnableDelayedExpansion
REM --- Host architecture selection (three-tier) -----------------------------
REM Priority: 1) --arch <value> CLI flag  2) data\config\host_arch file
REM           3) Auto-detect via %PROCESSOR_ARCHITECTURE% / PROCESSOR_ARCHITEW6432
set "FORCED_ARCH="
set "_NEXT_IS_ARCH="
for %%A in (%*) do (
    if defined _NEXT_IS_ARCH (
        set "FORCED_ARCH=%%~A"
        set "_NEXT_IS_ARCH="
    ) else if /i "%%~A"=="--arch" (
        set "_NEXT_IS_ARCH=1"
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
set "VENV_DIR=%LOCALAPPDATA%\QAIModelBuilder\envs\%VENV_DIR_NAME%"
set "ACTIVATE=%VENV_DIR%\Scripts\activate.bat"

REM Route Python bytecode caches out of the source tree into data\caches\pycache
REM (keeps the source tree clean; data\ is the per-user runtime root and is
REM git-ignored). Exported into the interactive shell so ad-hoc python / pytest /
REM ruff / mypy runs inside Console.bat also honour it. %~dp0 has a trailing
REM backslash so no extra separator is needed.
set "PYTHONPYCACHEPREFIX=%~dp0data\caches\pycache"

if not exist "%ACTIVATE%" goto :no_venv

REM Add tools\ to PATH so uv is available inside the shell.
set "PATH=%ROOT_DIR%tools;%PATH%"

REM PortableGit PATH injection (parity with Start.bat / qai.bat) so commands
REM that shell out to git -- e.g. `pip install git+https://...`, editable
REM installs of git checkouts, or `qai` subcommands -- find git even on a
REM machine without a system-wide git.
set "PORTABLE_GIT_DIR=%LOCALAPPDATA%\QAIModelBuilder\git"
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" (
    set "PATH=%PORTABLE_GIT_DIR%\cmd;%PORTABLE_GIT_DIR%\usr\bin;%PATH%"
) else if exist "%PORTABLE_GIT_DIR%\bin\git.exe" (
    set "PATH=%PORTABLE_GIT_DIR%\bin;%PORTABLE_GIT_DIR%\usr\bin;%PATH%"
)

cmd /k call "%ACTIVATE%"
exit /b 0

:no_venv
echo [ERROR] .venv not found at %LOCALAPPDATA%\QAIModelBuilder\envs. Please run Setup.bat first.
pause
exit /b 1
