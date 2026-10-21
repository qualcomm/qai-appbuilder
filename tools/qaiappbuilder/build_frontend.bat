@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM build_frontend.bat - self-contained WebUI build: bootstraps Node.js + pnpm
REM (when missing) and produces frontend\dist, which Start.bat serves.
REM
REM Why: frontend\dist is git-ignored, so a fresh clone has no UI (GET / -> 404)
REM and Build.bat refuses to run until "Setup.bat --frontend" installed Node.
REM This script does both steps in one go, with the same network preflight /
REM proxy / mirror handling as setup_env.bat.
REM
REM Toolchain location (identical to Setup.bat --frontend, so Build.bat reuses it):
REM   %LOCALAPPDATA%\QAIModelBuilder\node   portable Node.js %NODE_VERSION% + pnpm shim
REM   <repo>\data\pnpm-virtual-store        pnpm virtual store (frontend\pnpm-workspace.yaml)
REM   %LOCALAPPDATA%\QAIModelBuilder\corepack   corepack cache (pnpm tarball)
REM
REM Usage: build_frontend.bat [options]    (--help for the list)

setlocal EnableExtensions
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

REM Keep in sync with Setup.bat Step 5b and frontend\package.json "packageManager".
set "NODE_VERSION=22.20.0"
set "PNPM_VERSION=11.9.0"
set "NODE_MAJOR_MIN=22"

set "FORCED_ARCH="
set "OPT_PROXY="
set "OPT_MIRROR=auto"
set "NO_PROBE=0"
set "NO_PAUSE=0"
set "USE_SYSTEM_NODE=0"
set "BUILD_ARGS="
set "FORCE_INSTALL=0"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--help"        goto :help
if /i "%~1"=="-h"            goto :help
if /i "%~1"=="/?"            goto :help
if /i "%~1"=="--no-pause"    ( set "NO_PAUSE=1"        & shift & goto :parse )
if /i "%~1"=="--no-probe"    ( set "NO_PROBE=1"        & shift & goto :parse )
if /i "%~1"=="--system-node" ( set "USE_SYSTEM_NODE=1" & shift & goto :parse )
if /i "%~1"=="--install"     ( set "FORCE_INSTALL=1"   & set "BUILD_ARGS=%BUILD_ARGS% --install" & shift & goto :parse )
if /i "%~1"=="--clean"       ( set "FORCE_INSTALL=1"   & set "BUILD_ARGS=%BUILD_ARGS% --clean"   & shift & goto :parse )
if /i "%~1"=="--full"        ( set "BUILD_ARGS=%BUILD_ARGS% --full" & shift & goto :parse )
if /i "%~1"=="--arch"   goto :opt_arch
if /i "%~1"=="--proxy"  goto :opt_proxy
if /i "%~1"=="--mirror" goto :opt_mirror
echo [ERROR] Unknown option: %~1   ^(see --help^)
exit /b 2
:opt_arch
if /i "%~2"=="arm64" ( set "FORCED_ARCH=arm64" & shift & shift & goto :parse )
if /i "%~2"=="x64"   ( set "FORCED_ARCH=x64"   & shift & shift & goto :parse )
echo [ERROR] --arch needs arm64 or x64
exit /b 2
:opt_proxy
if "%~2"=="" ( echo [ERROR] --proxy needs a URL, e.g. http://host:port & exit /b 2 )
set "OPT_PROXY=%~2"
shift & shift & goto :parse
:opt_mirror
if /i "%~2"=="auto"     ( set "OPT_MIRROR=auto"     & shift & shift & goto :parse )
if /i "%~2"=="official" ( set "OPT_MIRROR=official" & shift & shift & goto :parse )
if /i "%~2"=="cn"       ( set "OPT_MIRROR=cn"       & shift & shift & goto :parse )
echo [ERROR] --mirror needs auto, official or cn
exit /b 2
:parsed

REM ---- architecture: flag > data\config\host_arch > detect (same as the other launchers)
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
if /i "%HOST_ARCH%"=="x64" ( set "HOST_ARCH=x64" & set "NODE_ARCH=x64" ) else ( set "HOST_ARCH=arm64" & set "NODE_ARCH=arm64" )

set "QAI_LAD=%LOCALAPPDATA%\QAIModelBuilder"
set "NODE_DIR=%QAI_LAD%\node"
set "DL_DIR=data\downloads"
if not exist "%DL_DIR%" mkdir "%DL_DIR%"
if not exist "%QAI_LAD%" mkdir "%QAI_LAD%"

REM corepack: never prompt, cache under our own tree so clean_env can remove it.
set "COREPACK_ENABLE_DOWNLOAD_PROMPT=0"
set "COREPACK_ENABLE_STRICT=0"
if not defined COREPACK_HOME set "COREPACK_HOME=%QAI_LAD%\corepack"
REM NOTE: do NOT set CI=true here - pnpm then forces --frozen-lockfile, while
REM Build.bat deliberately runs a non-frozen install (and already passes
REM --config.confirmModulesPurge=false to avoid the purge prompt).
set "_SYS_NODE="

REM ---- timing ---------------------------------------------------------------
set "TIMING_FILE=%DL_DIR%\_timing_frontend.txt"
set "_TOTAL_CS=0"
set "_MAXD=-1"
set "_MAXL="
if exist "%TIMING_FILE%" del /f /q "%TIMING_FILE%"
call :tick_init

echo.
echo  +--------------------------------------------------+
echo  ^|   QAI ModelBuilder  -  build_frontend            ^|
echo  +--------------------------------------------------+
echo [INFO] arch=%HOST_ARCH%  node=%NODE_DIR%
echo [INFO] source=%ROOT_DIR%
echo.

REM ===========================================================================
REM  Step 1: network preflight (nodejs.org + npm registry) -> proxy / mirror env
REM ===========================================================================
echo -- Step 1/4: network preflight ----------------------------------------------
if exist "%DL_DIR%\_net_env_fe.bat" del /f /q "%DL_DIR%\_net_env_fe.bat"
set "_NETARGS=-Mirror %OPT_MIRROR% -Need frontend -Emit "%DL_DIR%\_net_env_fe.bat""
if defined OPT_PROXY set "_NETARGS=%_NETARGS% -Proxy "%OPT_PROXY%""
if "%NO_PROBE%"=="1" set "_NETARGS=%_NETARGS% -NoProbe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\net_config.ps1" %_NETARGS%
if exist "%DL_DIR%\_net_env_fe.bat" (
    call "%DL_DIR%\_net_env_fe.bat"
) else (
    echo [WARN] net_config.ps1 produced no settings; continuing with defaults.
)
REM Node's own fetch (used by corepack) only honours HTTP(S)_PROXY with this flag.
if defined HTTPS_PROXY set "NODE_USE_ENV_PROXY=1"
call :tick "1  network preflight"

REM ===========================================================================
REM  Step 2: Node.js (portable, host arch)
REM ===========================================================================
echo.
echo -- Step 2/4: Node.js -------------------------------------------------------
if "%USE_SYSTEM_NODE%"=="1" goto :node_system
if not exist "%NODE_DIR%\node.exe" goto :node_get
call :node_major "%NODE_DIR%\node.exe"
call :node_check_major
if errorlevel 1 goto :node_too_old
echo [SKIP] portable Node.js present: %NODE_DIR% ^(v%_NODE_VER%^)
goto :node_ready
:node_too_old
echo [WARN] portable Node.js v%_NODE_VER% is older than %NODE_MAJOR_MIN%; reinstalling.
:node_get
call :install_node
if errorlevel 1 goto :fail_node
goto :node_ready

:node_system
where node >nul 2>&1
if errorlevel 1 ( echo [ERROR] --system-node given but node is not on PATH. & goto :fail )
for /f "delims=" %%N in ('where node') do if not defined _SYS_NODE set "_SYS_NODE=%%N"
call :node_major "%_SYS_NODE%"
call :node_check_major
if errorlevel 1 ( echo [ERROR] system node v%_NODE_VER% is older than %NODE_MAJOR_MIN% ^(frontend\package.json engines^). & goto :fail )
for %%D in ("%_SYS_NODE%") do set "NODE_DIR=%%~dpD"
if "%NODE_DIR:~-1%"=="\" set "NODE_DIR=%NODE_DIR:~0,-1%"
echo [INFO] using system Node.js v%_NODE_VER%: %NODE_DIR%

:node_ready
set "PATH=%NODE_DIR%;%PATH%"
"%NODE_DIR%\node.exe" --version >nul 2>&1 || goto :fail_node
call :tick "2  Node.js"

REM ===========================================================================
REM  Step 3: pnpm via corepack (pinned to frontend\package.json packageManager)
REM ===========================================================================
echo.
echo -- Step 3/4: pnpm %PNPM_VERSION% ------------------------------------------------
call :ensure_pnpm
if errorlevel 1 goto :fail_pnpm
call :tick "3  pnpm (corepack)"

REM ===========================================================================
REM  Step 4: pnpm install + vite build (delegated to Build.bat)
REM ===========================================================================
echo.
echo -- Step 4/4: build frontend\dist -------------------------------------------
REM A node_modules tree whose virtual store (data\pnpm-virtual-store) is gone, or
REM that was created by another arch/pnpm, makes Build.bat's "skip install" path
REM wrong. Build.bat already probes for that; we only pass the user's flags through.
REM Build.bat reads NODE_DIR from %LOCALAPPDATA%; for --system-node we put node on
REM PATH above, which Build.bat falls back to.
set "_DIST_BEFORE=0"
if exist "frontend\dist\index.html" set "_DIST_BEFORE=1"
call "%ROOT_DIR%Build.bat" --arch %HOST_ARCH% %BUILD_ARGS%
set "_BUILD_RC=%ERRORLEVEL%"
call :tick "4  pnpm install + vite build"
if not "%_BUILD_RC%"=="0" goto :fail_build
if not exist "frontend\dist\index.html" goto :fail_build

for /f %%C in ('dir /s /b /a-d "frontend\dist" ^| find /c /v ""') do set "_DIST_FILES=%%C"
call :timing_summary "OK"
echo.
echo  ================================================================
echo      build_frontend finished ^(arch=%HOST_ARCH%, profile=%QAI_NET_PROFILE%^)
echo      frontend\dist: %_DIST_FILES% files
echo  ================================================================
echo   Run the server :  Start.bat
echo   Rebuild later  :  Build.bat   ^(Node + pnpm are now installed^)
echo.
if "%NO_PAUSE%"=="0" pause
endlocal & exit /b 0

REM ===========================================================================
REM  Failure exits
REM ===========================================================================
:fail_node
echo [ERROR] Could not obtain Node.js %NODE_VERSION% ^(%NODE_ARCH%^).
echo [ERROR] Try:  build_frontend.bat --mirror cn   or   --proxy http://host:port
echo [ERROR]   or install Node ^>= %NODE_MAJOR_MIN% yourself and use --system-node
goto :fail
:fail_pnpm
echo [ERROR] corepack could not provide pnpm %PNPM_VERSION%.
echo [ERROR] Try:  build_frontend.bat --mirror cn   ^(npm registry mirror^)   or   --proxy http://host:port
goto :fail
:fail_build
echo [ERROR] frontend build failed ^(Build.bat exit %_BUILD_RC%^).
echo [ERROR] Network errors during "pnpm install": retry with --mirror cn or --proxy.
echo [ERROR] A corrupted node_modules: retry with --clean.
goto :fail
:fail
call :tick "(failed step, time until error)"
call :timing_summary "FAILED"
if "%NO_PAUSE%"=="0" pause
endlocal & exit /b 1

:help
echo Usage: build_frontend.bat [options]
echo.
echo   Installs portable Node.js %NODE_VERSION% + pnpm %PNPM_VERSION% if missing, then builds
echo   frontend\dist ^(the WebUI that Start.bat serves^).
echo.
echo   --arch arm64^|x64       force architecture ^(default: host_arch file, else detect^)
echo   --proxy URL            HTTP proxy for Node / npm downloads
echo   --mirror auto^|official^|cn
echo                          auto ^(default^): use npmmirror only if nodejs.org / npmjs is unreachable
echo   --no-probe             skip the connectivity probe
echo   --system-node          use the node already on PATH ^(must be ^>= %NODE_MAJOR_MIN%^) instead of
echo                          downloading the portable one
echo   --install              force "pnpm install" even if node_modules looks complete
echo   --clean                wipe frontend\node_modules and reinstall
echo   --full                 full verified build ^(gen:types + typecheck + lint + test + build^)
echo   --no-pause             do not pause at the end
exit /b 0

REM ===========================================================================
REM  Subroutines
REM ===========================================================================

REM :install_node - download + unpack the portable Node zip into %NODE_DIR%.
:install_node
set "_NODE_BASE=https://nodejs.org/dist"
if defined QAI_NODE_MIRROR set "_NODE_BASE=%QAI_NODE_MIRROR%"
set "_NODE_NAME=node-v%NODE_VERSION%-win-%NODE_ARCH%"
set "_NODE_ZIP=%DL_DIR%\%_NODE_NAME%.zip"
call :ensure_aria2c
set "_ARIA="
if exist "data\bin\aria2c\aria2c.exe" set "_ARIA=-Aria2cExe "data\bin\aria2c\aria2c.exe""
set "_PX="
if defined HTTPS_PROXY set "_PX=-ProxyUrl "%HTTPS_PROXY%""
echo [INFO] GET %_NODE_BASE%/v%NODE_VERSION%/%_NODE_NAME%.zip
REM -Connections 4: some CDNs / proxies ignore Range; -ZipTest catches a bad stitch.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" -Url "%_NODE_BASE%/v%NODE_VERSION%/%_NODE_NAME%.zip" -OutFile "%_NODE_ZIP%" %_ARIA% -MinSize 20000000 -ZipTest -MaxRetries 4 -StallTimeoutSec 60 -AttemptTimeoutSec 600 -Connections 4 %_PX%
if not exist "%_NODE_ZIP%" exit /b 1
echo [INFO] extracting to %NODE_DIR% ...
if exist "%NODE_DIR%" rmdir /s /q "%NODE_DIR%" 2>nul
if exist "%DL_DIR%\_node_tmp" rmdir /s /q "%DL_DIR%\_node_tmp" 2>nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%_NODE_ZIP%' -DestinationPath '%DL_DIR%\_node_tmp' -Force; $i = Get-ChildItem -LiteralPath '%DL_DIR%\_node_tmp' -Directory | Select-Object -First 1; if ($i) { Move-Item -LiteralPath $i.FullName -Destination '%NODE_DIR%' -Force } else { Move-Item -LiteralPath '%DL_DIR%\_node_tmp' -Destination '%NODE_DIR%' -Force }; if (Test-Path '%DL_DIR%\_node_tmp') { Remove-Item -LiteralPath '%DL_DIR%\_node_tmp' -Recurse -Force }"
if not exist "%NODE_DIR%\node.exe" exit /b 1
del /f /q "%_NODE_ZIP%" 2>nul
for /f "delims=" %%V in ('"%NODE_DIR%\node.exe" --version') do echo [OK]   Node.js %%V installed: %NODE_DIR%
exit /b 0

REM :ensure_aria2c - same as setup_env.bat: optional multi-connection downloader (~2.5MB),
REM   shared at data\bin\aria2c; non-fatal (falls back to single-thread download).
:ensure_aria2c
if exist "data\bin\aria2c\aria2c.exe" exit /b 0
if not exist "data\bin\aria2c" mkdir "data\bin\aria2c"
set "_ARIA_PATH="
for /f "delims=" %%A in ('where aria2c.exe 2^>nul') do if not defined _ARIA_PATH set "_ARIA_PATH=%%A"
if defined _ARIA_PATH (
    copy /y "%_ARIA_PATH%" "data\bin\aria2c\aria2c.exe" >nul
    if exist "data\bin\aria2c\aria2c.exe" ( echo [SKIP] using aria2c from PATH. & exit /b 0 )
)
set "_AU=https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
if defined QAI_GITHUB_MIRROR set "_AU=%QAI_GITHUB_MIRROR%%_AU%"
set "_PX="
if defined HTTPS_PROXY set "_PX=-ProxyUrl "%HTTPS_PROXY%""
echo [INFO] downloading aria2c ^(multi-connection downloader, ~2.5MB^)...
echo [INFO] GET %_AU%
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" -Url "%_AU%" -OutFile "%DL_DIR%\_aria2c_tmp.zip" -MinSize 1000000 -ZipTest -MaxRetries 5 -StallTimeoutSec 60 -AttemptTimeoutSec 180 %_PX%
if errorlevel 1 ( echo [WARN] aria2c unavailable; using single-thread download. & exit /b 0 )
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%DL_DIR%\_aria2c_tmp.zip' -DestinationPath '%DL_DIR%\_aria2c_tmp' -Force; Get-ChildItem '%DL_DIR%\_aria2c_tmp' -Recurse -Filter aria2c.exe | Copy-Item -Destination 'data\bin\aria2c\aria2c.exe' -Force; Remove-Item '%DL_DIR%\_aria2c_tmp' -Recurse -Force"
del /f /q "%DL_DIR%\_aria2c_tmp.zip" 2>nul
if exist "data\bin\aria2c\aria2c.exe" ( echo [OK] aria2c ready. ) else ( echo [WARN] aria2c unpack failed; using single-thread download. )
exit /b 0

REM :node_major <node.exe> -> _NODE_VER (e.g. 22.20.0), _NODE_MAJOR (e.g. 22)
:node_major
set "_NODE_VER="
set "_NODE_MAJOR=0"
for /f "delims=" %%V in ('"%~1" --version 2^>nul') do set "_NODE_VER=%%V"
if not defined _NODE_VER exit /b 0
set "_NODE_VER=%_NODE_VER:v=%"
for /f "tokens=1 delims=." %%M in ("%_NODE_VER%") do set "_NODE_MAJOR=%%M"
exit /b 0

:node_check_major
if %_NODE_MAJOR% GEQ %NODE_MAJOR_MIN% exit /b 0
exit /b 1

REM :ensure_pnpm - pnpm %PNPM_VERSION% via corepack, shim written next to node.exe.
REM   --system-node: corepack cannot write into Program Files without admin, so the
REM   shims go to our own dir which is put first on PATH.
:ensure_pnpm
set "_SHIM_DIR=%NODE_DIR%"
if "%USE_SYSTEM_NODE%"=="1" set "_SHIM_DIR=%QAI_LAD%\pnpm-shim"
if not exist "%_SHIM_DIR%" mkdir "%_SHIM_DIR%"
set "PATH=%_SHIM_DIR%;%PATH%"
call :pnpm_version
if "%_PNPM_VER%"=="%PNPM_VERSION%" (
    echo [SKIP] pnpm %_PNPM_VER% already active.
    exit /b 0
)
if not exist "%NODE_DIR%\corepack.cmd" (
    echo [ERROR] corepack not found next to node ^(%NODE_DIR%^). Node 25+ no longer bundles it;
    echo [ERROR] drop --system-node to use the portable Node %NODE_VERSION%.
    exit /b 1
)
echo [INFO] corepack enable pnpm ^(shim dir: %_SHIM_DIR%^)
call "%NODE_DIR%\corepack.cmd" enable --install-directory "%_SHIM_DIR%" pnpm
if errorlevel 1 exit /b 1
echo [INFO] corepack prepare pnpm@%PNPM_VERSION% --activate ^(registry: %COREPACK_NPM_REGISTRY%^)
call "%NODE_DIR%\corepack.cmd" prepare pnpm@%PNPM_VERSION% --activate
if errorlevel 1 exit /b 1
call :pnpm_version
if not "%_PNPM_VER%"=="%PNPM_VERSION%" (
    echo [ERROR] pnpm reports "%_PNPM_VER%", expected %PNPM_VERSION%.
    exit /b 1
)
echo [OK]   pnpm %_PNPM_VER% ready.
exit /b 0

REM :pnpm_version -> _PNPM_VER (empty when pnpm is not runnable). Run from frontend\
REM   so corepack honours the packageManager pin there.
:pnpm_version
set "_PNPM_VER="
where pnpm >nul 2>&1 || exit /b 0
pushd "%ROOT_DIR%frontend"
for /f "delims=" %%P in ('pnpm --version 2^>nul') do set "_PNPM_VER=%%P"
popd
exit /b 0

REM ---------------------------------------------------------------------------
REM Timing helpers (same as setup_env.bat)
REM ---------------------------------------------------------------------------
:clock
set "_CT=%TIME: =0%"
for /f "tokens=1-4 delims=:.," %%a in ("%_CT%") do set /a "_NOW=((1%%a-100)*3600+(1%%b-100)*60+(1%%c-100))*100+(1%%d-100)"
exit /b 0

:tick_init
call :clock
set "_LAST=%_NOW%"
exit /b 0

:tick
call :clock
set /a "_D=_NOW-_LAST"
if %_D% LSS 0 set /a "_D+=8640000"
set "_LAST=%_NOW%"
set /a "_TOTAL_CS+=_D"
if %_D% GTR %_MAXD% ( set "_MAXD=%_D%" & set "_MAXL=%~1" )
call :fmt %_D%
>>"%TIMING_FILE%" echo %_FMT%  %~1
echo [TIME] %_FMT%  %~1
exit /b 0

:fmt
set /a "_FS=%~1/100, _FT=(%~1%%100)/10, _FM=_FS/60, _FSS=_FS%%60"
if %_FM% GTR 0 ( set "_FMT=%_FM%m %_FSS%.%_FT%s" ) else ( set "_FMT=%_FSS%.%_FT%s" )
set "_FMT=        %_FMT%"
set "_FMT=%_FMT:~-9%"
exit /b 0

:timing_summary
if not exist "%TIMING_FILE%" exit /b 0
call :fmt %_TOTAL_CS%
echo.
echo  -------------------- time per step (%~1) --------------------
type "%TIMING_FILE%"
echo  ------------------------------------------------------------
echo  %_FMT%  TOTAL
if not defined _MAXL goto :ts_nomax
call :fmt %_MAXD%
echo  slowest: %_FMT%  %_MAXL%
:ts_nomax
if not exist "data\logs" mkdir "data\logs"
>>"data\logs\build_frontend_timing.log" echo === %DATE% %TIME%  arch=%HOST_ARCH%  profile=%QAI_NET_PROFILE%  result=%~1  total_cs=%_TOTAL_CS%
>>"data\logs\build_frontend_timing.log" type "%TIMING_FILE%"
exit /b 0
