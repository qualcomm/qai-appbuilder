@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM clean_env.bat - remove what setup_env.bat installed.
REM
REM Default (safe): the venv for the selected arch, install-time downloads
REM   (data\downloads), the downloaded helper tools (data\bin\uv|aria2c|7zr),
REM   data\config\host_arch and the fetched vendor bundle (vendor\whl|bin|...).
REM NOT touched by default: data\ user data (qai.db, logs, weights, config),
REM   uv-managed Python builds, the x64 converter venv, QAIRT SDK, PortableGit.
REM
REM Options:
REM   --arch arm64^|x64   which venv to remove (default: host_arch file, else detect)
REM   --purge-data       also delete the WHOLE data\ directory (user data!)
REM   --purge-python     also remove uv-managed cpython-3.13 for this arch
REM                      (with --all-envs: 3.13 arm64+x64 and the 3.10 x64 converter)
REM                      ^(shared by other QAI installs on this machine^)
REM   --all-envs         remove every venv under %LOCALAPPDATA%\QAIModelBuilder\envs
REM   --dry-run          only print what would be removed
REM   -y, --yes          do not ask for confirmation
REM   --no-pause

setlocal EnableExtensions
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

set "FORCED_ARCH="
set "PURGE_DATA=0"
set "PURGE_PY=0"
set "ALL_ENVS=0"
set "DRY=0"
set "YES=0"
set "NO_PAUSE=0"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--help"         goto :help
if /i "%~1"=="-h"             goto :help
if /i "%~1"=="/?"             goto :help
if /i "%~1"=="--purge-data"   ( set "PURGE_DATA=1" & shift & goto :parse )
if /i "%~1"=="--purge-python" ( set "PURGE_PY=1"   & shift & goto :parse )
if /i "%~1"=="--all-envs"     ( set "ALL_ENVS=1"   & shift & goto :parse )
if /i "%~1"=="--dry-run"      ( set "DRY=1"        & shift & goto :parse )
if /i "%~1"=="--yes"          ( set "YES=1"        & shift & goto :parse )
if /i "%~1"=="-y"             ( set "YES=1"        & shift & goto :parse )
if /i "%~1"=="--no-pause"     ( set "NO_PAUSE=1"   & shift & goto :parse )
if /i "%~1"=="--arch" (
    if /i "%~2"=="arm64" ( set "FORCED_ARCH=arm64" & shift & shift & goto :parse )
    if /i "%~2"=="x64"   ( set "FORCED_ARCH=x64"   & shift & shift & goto :parse )
    echo [ERROR] --arch needs arm64 or x64
    exit /b 2
)
echo [ERROR] Unknown option: %~1   ^(see --help^)
exit /b 2
:parsed

set "HOST_ARCH="
if defined FORCED_ARCH set "HOST_ARCH=%FORCED_ARCH%"
if not defined HOST_ARCH if exist "data\config\host_arch" (
    for /f "usebackq tokens=* delims=" %%L in ("data\config\host_arch") do if not defined HOST_ARCH set "HOST_ARCH=%%L"
)
if /i not "%HOST_ARCH%"=="arm64" if /i not "%HOST_ARCH%"=="x64" (
    set "HOST_ARCH=arm64"
    if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" set "HOST_ARCH=x64"
    if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "HOST_ARCH=x64"
)
set "VENV_DIR_NAME=.venv_arm64_313"
set "PY_SPEC=cpython-3.13-windows-aarch64"
if /i "%HOST_ARCH%"=="x64" ( set "VENV_DIR_NAME=.venv_x64_313" & set "PY_SPEC=cpython-3.13-windows-x86_64" )
set "ENVS_DIR=%LOCALAPPDATA%\QAIModelBuilder\envs"
set "VENV_DIR=%ENVS_DIR%\%VENV_DIR_NAME%"

echo.
echo  QAI ModelBuilder - clean_env   ^(arch=%HOST_ARCH%^)
echo  ------------------------------------------------------------
echo  will remove:
if "%ALL_ENVS%"=="1" ( echo    - %ENVS_DIR%  ^(all venvs^) ) else ( echo    - %VENV_DIR% )
echo    - data\downloads  data\bin\uv  data\bin\aria2c  data\bin\7zr  data\config\host_arch
echo    - vendor\whl  vendor\bin  vendor\tiktoken  vendor\nltk_data  vendor\g2p_data  vendor\openblas
if "%PURGE_PY%"=="1" if "%ALL_ENVS%"=="0" echo    - uv-managed %PY_SPEC%
if "%PURGE_PY%"=="1" if "%ALL_ENVS%"=="1" echo    - uv-managed cpython-3.13 ^(arm64 + x64^) and cpython-3.10 x64
if "%PURGE_DATA%"=="1" echo    - data\   ^(ALL user data: qai.db, logs, weights, config^)
if "%DRY%"=="1" echo  ^(dry run - nothing is deleted^)
echo.

if "%DRY%"=="1" goto :confirmed
if "%YES%"=="1" goto :confirmed
set "_ANS="
set /p "_ANS=Proceed? [y/N] "
if /i not "%_ANS%"=="y" ( echo Aborted. & endlocal & exit /b 1 )
:confirmed

REM ---- stop processes that run from the venv (they lock files) --------------
if "%DRY%"=="0" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = if ('%ALL_ENVS%' -eq '1') { '%ENVS_DIR%' } else { '%VENV_DIR%' }; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($p, 'OrdinalIgnoreCase') } | ForEach-Object { Write-Host ('[INFO] stopping pid ' + $_.ProcessId + ' ' + $_.Name); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)

set "_FAIL=0"
if "%ALL_ENVS%"=="1" ( call :rm_dir "%ENVS_DIR%" ) else ( call :rm_dir "%VENV_DIR%" )

REM --purge-python must run BEFORE data\bin\uv is removed (it needs uv.exe).
if "%PURGE_PY%"=="1" call :purge_python
call :rm_dir "data\downloads"
call :rm_dir "data\bin\uv"
call :rm_dir "data\bin\aria2c"
call :rm_dir "data\bin\7zr"
call :rm_file "data\config\host_arch"
call :rm_dir "vendor\whl"
call :rm_dir "vendor\bin"
call :rm_dir "vendor\tiktoken"
call :rm_dir "vendor\nltk_data"
call :rm_dir "vendor\g2p_data"
call :rm_dir "vendor\openblas"

if "%PURGE_DATA%"=="1" call :rm_dir "data"

echo.
if "%_FAIL%"=="1" (
    echo [WARN] some paths could not be removed ^(files in use?^). Close Start.bat / Console.bat and re-run.
) else (
    echo [OK]   clean finished.
)
if "%NO_PAUSE%"=="0" if "%YES%"=="0" pause
endlocal & exit /b %_FAIL%

REM :purge_python - remove uv-managed interpreters. Default: this arch's 3.13.
REM With --all-envs also every interpreter setup scripts install (3.13 both
REM archs + the x64 3.10 converter). Uses uv when available, else deletes the
REM matching directories under %APPDATA%\uv\python directly.
:purge_python
set "_SPECS=%PY_SPEC%"
set "_GLOBS=cpython-3.13*-windows-aarch64-none"
if /i "%HOST_ARCH%"=="x64" set "_GLOBS=cpython-3.13*-windows-x86_64-none"
if "%ALL_ENVS%"=="1" (
    set "_SPECS=cpython-3.13-windows-aarch64 cpython-3.13-windows-x86_64 cpython-3.10-windows-x86_64"
    set "_GLOBS=cpython-3.13*-windows-aarch64-none cpython-3.13*-windows-x86_64-none cpython-3.10*-windows-x86_64-none"
)
set "_UV="
if exist "data\bin\uv\uv.exe" set "_UV=data\bin\uv\uv.exe"
if not defined _UV for /f "delims=" %%U in ('where uv 2^>nul') do if not defined _UV set "_UV=%%U"
if "%DRY%"=="1" (
    echo [DRY ] uv python uninstall %_SPECS%
    exit /b 0
)
if defined _UV (
    echo [INFO] %_UV% python uninstall %_SPECS%
    "%_UV%" python uninstall %_SPECS%
)
set "_PYROOT=%APPDATA%\uv\python"
if defined UV_PYTHON_INSTALL_DIR set "_PYROOT=%UV_PYTHON_INSTALL_DIR%"
REM Fallback / leftovers (minor-version links, no uv available): delete dirs directly.
for %%G in (%_GLOBS%) do (
    for /d %%D in ("%_PYROOT%\%%G") do call :rm_dir "%%~fD"
)
exit /b 0

:rm_dir
if not exist "%~1" exit /b 0
if "%DRY%"=="1" ( echo [DRY ] rmdir %~1 & exit /b 0 )
rmdir /s /q "%~1" 2>nul
if exist "%~1" ( echo [FAIL] could not remove %~1 & set "_FAIL=1" ) else ( echo [DEL ] %~1 )
exit /b 0

:rm_file
if not exist "%~1" exit /b 0
if "%DRY%"=="1" ( echo [DRY ] del %~1 & exit /b 0 )
del /f /q "%~1" 2>nul
if exist "%~1" ( echo [FAIL] could not remove %~1 & set "_FAIL=1" ) else ( echo [DEL ] %~1 )
exit /b 0

:help
echo Usage: clean_env.bat [--arch arm64^|x64] [--purge-data] [--purge-python] [--all-envs] [--dry-run] [-y] [--no-pause]
echo See the header of this file for details.
exit /b 0
