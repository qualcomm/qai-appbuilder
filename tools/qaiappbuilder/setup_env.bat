@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM setup_env.bat - lean, network-tolerant environment setup for QAI ModelBuilder.
REM
REM Differences from Setup.bat (which is left untouched):
REM   * network preflight + proxy / mirror profile (scripts\setup\net_config.ps1)
REM   * x64 never downloads the ARM64-only vendor wheels (~170MB) or openblas
REM   * only the pieces needed to run the server are installed by default;
REM     everything heavy is opt-in
REM   * same layout as Setup.bat, so Start.bat / qai.bat / Console.bat / Uninstall.bat
REM     work on the result unchanged:
REM       venv        %LOCALAPPDATA%\QAIModelBuilder\envs\.venv_<arch>_313
REM       data        <repo>\data   (uv, aria2c, 7zr, config\host_arch, qai.db ...)
REM
REM Usage: setup_env.bat [options]     (setup_env.bat --help for the list)

setlocal EnableExtensions
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"
set "PYTHONPYCACHEPREFIX=%ROOT_DIR%data\caches\pycache"

REM ---- defaults -------------------------------------------------------------
set "FORCED_ARCH="
set "OPT_PROXY="
set "OPT_MIRROR=auto"
set "WITH_BROWSER=0"
set "WITH_ASTGREP=0"
set "WITH_APPS=0"
set "WITH_BUILDER=0"
set "NO_PROBE=0"
set "NO_PAUSE=0"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--help"         goto :help
if /i "%~1"=="-h"             goto :help
if /i "%~1"=="/?"             goto :help
if /i "%~1"=="--no-pause"     ( set "NO_PAUSE=1"     & shift & goto :parse )
if /i "%~1"=="--no-probe"     ( set "NO_PROBE=1"     & shift & goto :parse )
if /i "%~1"=="--with-browser" ( set "WITH_BROWSER=1" & shift & goto :parse )
if /i "%~1"=="--with-astgrep" ( set "WITH_ASTGREP=1" & shift & goto :parse )
if /i "%~1"=="--with-apps"    ( set "WITH_APPS=1"    & shift & goto :parse )
if /i "%~1"=="--builder"      ( set "WITH_BUILDER=1" & shift & goto :parse )
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

REM ---- timing: :tick <label> prints time since the previous checkpoint ---------
set "TIMING_FILE=data\downloads\_timing.txt"
set "_TOTAL_CS=0"
set "_MAXD=-1"
set "_MAXL="
if not exist "data\downloads" mkdir "data\downloads"
if exist "%TIMING_FILE%" del /f /q "%TIMING_FILE%"
call :tick_init

REM ---- architecture (same priority as Setup.bat: flag > host_arch file > detect) --
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
if /i "%HOST_ARCH%"=="x64" (
    set "HOST_ARCH=x64"
    set "UV_ARCH=x86_64"
    set "WHL_PLAT=win_amd64"
    set "PY_SPEC=cpython-3.13-windows-x86_64"
    set "VENV_DIR_NAME=.venv_x64_313"
    set "ASTGREP_ASSET=app-x86_64-pc-windows-msvc.zip"
) else (
    set "HOST_ARCH=arm64"
    set "UV_ARCH=aarch64"
    set "WHL_PLAT=win_arm64"
    set "PY_SPEC=cpython-3.13-windows-aarch64"
    set "VENV_DIR_NAME=.venv_arm64_313"
    set "ASTGREP_ASSET=app-aarch64-pc-windows-msvc.zip"
)
set "ENVS_DIR=%LOCALAPPDATA%\QAIModelBuilder\envs"
set "VENV_DIR=%ENVS_DIR%\%VENV_DIR_NAME%"
set "VPY=%VENV_DIR%\Scripts\python.exe"
set "DL_DIR=data\downloads"
set "UV_BIN_DIR=data\bin\uv"
REM qai_appbuilder wheel selection.
REM   QAI_AB_RELEASE_TAG  GitHub release tag (v<tag>) - the same for every architecture.
REM   QAI_AB_WHL_VERSION  version of the wheel to install - depends on the architecture:
REM                         x64   <tag>.1  pure x86_64 build, published on the GitHub release.
REM                                        (The PyPI win_amd64 wheel <tag> is an ARM64EC build: its
REM                                         libappbuilder.dll etc. carry ARM64 entry code and cannot load
REM                                         on an Intel/AMD CPU, so x64 never installs from PyPI.)
REM                         arm64 <tag>    vendored wheel in vendor\whl.
set "QAI_AB_RELEASE_TAG=2.48.40"
if /i "%HOST_ARCH%"=="x64" (
    set "QAI_AB_WHL_VERSION=%QAI_AB_RELEASE_TAG%.1"
) else (
    set "QAI_AB_WHL_VERSION=%QAI_AB_RELEASE_TAG%"
)
set "QAI_AB_WHL_FILE=qai_appbuilder-%QAI_AB_WHL_VERSION%-cp313-cp313-%WHL_PLAT%.whl"
set "RELEASE_BASE=https://github.com/qualcomm/qai-appbuilder/releases/download/v%QAI_AB_RELEASE_TAG%"
set "VDEPS_URL=https://www.aidevhome.com/data/adh2/attr/vendor-deps.7z"

echo.
echo  +--------------------------------------------------+
echo  ^|   QAI ModelBuilder  -  setup_env                 ^|
echo  +--------------------------------------------------+
echo [INFO] arch=%HOST_ARCH%  venv=%VENV_DIR%
echo [INFO] source=%ROOT_DIR%
echo.

if not exist "data\bin\uv"   mkdir "data\bin\uv"
if not exist "%DL_DIR%"      mkdir "%DL_DIR%"
if not exist "%ENVS_DIR%"    mkdir "%ENVS_DIR%"

REM ===========================================================================
REM  Step 1: network preflight -> proxy + mirror environment
REM ===========================================================================
echo -- Step 1/7: network preflight ----------------------------------------------
set "_NEED=core"
if "%WITH_BROWSER%"=="1" set "_NEED=%_NEED%,browser"
if "%WITH_BUILDER%"=="1" set "_NEED=%_NEED%,browser,builder"
if exist "%DL_DIR%\_net_env.bat" del /f /q "%DL_DIR%\_net_env.bat"
set "_NETARGS=-Mirror %OPT_MIRROR% -Need %_NEED% -Emit "%DL_DIR%\_net_env.bat""
if defined OPT_PROXY set "_NETARGS=%_NETARGS% -Proxy "%OPT_PROXY%""
if "%NO_PROBE%"=="1" set "_NETARGS=%_NETARGS% -NoProbe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\net_config.ps1" %_NETARGS%
if exist "%DL_DIR%\_net_env.bat" (
    call "%DL_DIR%\_net_env.bat"
) else (
    echo [WARN] net_config.ps1 produced no settings; continuing with defaults.
)
call :tick "1  network preflight"

REM uv: trust the corporate certificate store, generous timeouts for slow links.
set "UV_SYSTEM_CERTS=1"
set "UV_NATIVE_TLS="
if not defined UV_HTTP_TIMEOUT set "UV_HTTP_TIMEOUT=300"
if not defined UV_HTTP_RETRIES set "UV_HTTP_RETRIES=5"
REM cryptography (vendored arm64 build) warns "OpenSSL 3's legacy provider failed to load" on
REM import; nothing here needs legacy ciphers, so skip loading it (silences the warning).
if not defined CRYPTOGRAPHY_OPENSSL_NO_LEGACY set "CRYPTOGRAPHY_OPENSSL_NO_LEGACY=1"

REM ===========================================================================
REM  Step 2: uv  (PATH copy first, else download)
REM ===========================================================================
echo.
echo -- Step 2/7: aria2c + uv ----------------------------------------------------
REM aria2c first (all archs): every later GitHub/large download goes multi-connection.
call :ensure_aria2c
call :tick "2a aria2c (download, or reuse)"
set "UV_EXE=data\bin\uv\uv.exe"
if exist "%UV_EXE%" goto :uv_ready
where uv >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] using uv from PATH.
    set "UV_EXE=uv"
    goto :uv_ready
)
REM uv ships win_arm64 / win_amd64 wheels on PyPI (uv.exe inside): no GitHub needed, and the
REM tuna mirror serves them in China. GitHub release zip is the fallback.
call :pypi_exe uv %WHL_PLAT% "uv.exe,uvx.exe" "%UV_BIN_DIR%" ""
if exist "%UV_EXE%" goto :uv_ready
echo [INFO] downloading uv from GitHub ^(%UV_ARCH%^)...
set "DL_ZIPTEST=1"
call :dl "https://github.com/astral-sh/uv/releases/latest/download/uv-%UV_ARCH%-pc-windows-msvc.zip" "%DL_DIR%\_uv_tmp.zip" 5000000 300
if errorlevel 1 goto :fail_uv
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%DL_DIR%\_uv_tmp.zip' -DestinationPath '%UV_BIN_DIR%' -Force"
if errorlevel 1 goto :fail_uv
del /f /q "%DL_DIR%\_uv_tmp.zip" 2>nul
:uv_ready
set "PATH=%ROOT_DIR%data\bin\uv;%PATH%"
"%UV_EXE%" --version
if errorlevel 1 goto :fail_uv
call :tick "2b uv (download+unpack, or reuse)"

REM ===========================================================================
REM  Step 3: Python 3.13 + venv
REM ===========================================================================
echo.
echo -- Step 3/7: Python 3.13 + venv ---------------------------------------------
"%UV_EXE%" python find %PY_SPEC% >nul 2>&1
if errorlevel 1 (
    echo [INFO] installing %PY_SPEC% ...
    "%UV_EXE%" python install --no-bin --no-registry %PY_SPEC%
    if errorlevel 1 goto :fail_python
) else (
    echo [SKIP] %PY_SPEC% already installed.
)
call :tick "3a Python 3.13 interpreter"
set "_VENV_OK=0"
if exist "%VPY%" if exist "%VENV_DIR%\Scripts\activate.bat" (
    "%VPY%" -m pip --version >nul 2>&1
    if not errorlevel 1 set "_VENV_OK=1"
)
if "%_VENV_OK%"=="1" (
    echo [SKIP] venv already complete: %VENV_DIR%
) else (
    echo [INFO] creating venv ...
    "%UV_EXE%" venv "%VENV_DIR%" --python %PY_SPEC% --seed --allow-existing
    if errorlevel 1 goto :fail_venv
)
call "%VENV_DIR%\Scripts\activate.bat"
set "PYTHONPATH=%ROOT_DIR%src;%ROOT_DIR%"
"%VPY%" "%ROOT_DIR%scripts\setup\install_pycache_pth.py" --venv "%VENV_DIR%" --repo-root "%ROOT_DIR%." >nul 2>&1
call :tick "3b venv create/check"

REM ===========================================================================
REM  Step 4: vendor bundle (ARM64 only) + guard DLLs
REM ===========================================================================
echo.
echo -- Step 4/7: vendor wheels / native helpers ---------------------------------
set "USE_VENDOR=0"
if /i "%HOST_ARCH%"=="arm64" (
    if not exist "vendor\whl\%QAI_AB_WHL_FILE%" (
        call :fetch_vendor
        if errorlevel 1 goto :fail_vendor
    )
    call :tick "4a vendor-deps bundle (arm64)"
    if exist "vendor\whl\%QAI_AB_WHL_FILE%" set "USE_VENDOR=1"
) else (
    echo [SKIP] x64: ARM64 vendor wheels / openblas are not needed ^(saves ~190MB^).
)
call :fetch_guard
call :tick "4b guard.zip"
if "%WITH_ASTGREP%"=="1" call :fetch_astgrep
if "%WITH_ASTGREP%"=="1" call :tick "4c ast-grep"

REM ===========================================================================
REM  Step 5: Python dependencies
REM ===========================================================================
echo.
echo -- Step 5/7: Python dependencies --------------------------------------------
set "_EXTRAS=--extra cc-sdk"
if "%WITH_BROWSER%"=="1" set "_EXTRAS=%_EXTRAS% --extra search"
if "%WITH_BUILDER%"=="1" set "_EXTRAS=--extra cc-sdk --extra search"
set "_FL="
if "%USE_VENDOR%"=="1" (
    echo [INFO] installing vendored ARM64 wheels ^(--no-deps^)...
    call :install_vendor_whls
    if errorlevel 1 goto :fail_install
    call :tick "5a vendored arm64 wheels"
    set "_FL=--find-links vendor\whl"
    if exist "data\downloads\_constraints.txt" set "_FL=--find-links vendor\whl -c data\downloads\_constraints.txt"
)
"%VPY%" -c "import fastapi, uvicorn, structlog, paramiko, claude_agent_sdk" >nul 2>&1
if not errorlevel 1 if "%WITH_BROWSER%"=="0" if "%WITH_BUILDER%"=="0" (
    echo [SKIP] dependency closure already importable.
    goto :deps_done
)
set "_IDX=%UV_DEFAULT_INDEX%"
if not defined _IDX set "_IDX=pypi.org"
echo [INFO] uv pip install -r pyproject.toml %_EXTRAS% ^(index=%_IDX%^)
"%UV_EXE%" pip install -r pyproject.toml %_EXTRAS% %_FL%
if errorlevel 1 goto :fail_install
:deps_done
call :tick "5b pyproject deps (uv pip install)"

if "%WITH_BROWSER%"=="1" (
    echo [INFO] installing Playwright Chromium ^(non-fatal^)...
    "%VPY%" -m playwright install chromium
    if errorlevel 1 echo [WARN] Chromium download failed; browser tools stay disabled. Retry later with --with-browser.
    call :tick "5c Playwright Chromium"
)

REM qai_appbuilder (inference binding). ARM64: vendored wheel. x64: GitHub release wheel (version .1).
call :install_qai_appbuilder
call :tick "5d qai_appbuilder wheel"

if "%WITH_APPS%"=="1" (
    echo [INFO] installing App Builder pack deps ^(non-fatal^)...
    "%VPY%" -m scripts.setup.install_app_builder_deps
    if errorlevel 1 echo [WARN] some App Builder pack deps failed; re-run with --with-apps later.
    call :tick "5e App Builder pack deps"
)

REM ===========================================================================
REM  Step 6: initialise data\ tree
REM ===========================================================================
echo.
echo -- Step 6/7: initialise data\ -----------------------------------------------
"%VPY%" -m scripts.init.install --apply --factory-root "%ROOT_DIR%factory" --data-root "%ROOT_DIR%data" --sql-migrations "%ROOT_DIR%src\qai\platform\persistence\migrations_sql" --secret-backend auto --skip compile_factory
if errorlevel 1 (
    echo [WARN] data initialisation reported errors ^(non-fatal^).
) else (
    echo [OK]   data\ initialised.
)
if exist "data\config\qairt_env.json" "%VPY%" "scripts\setup\patch_qairt_env_arch.py" "data\config\qairt_env.json" "%HOST_ARCH%" >nul 2>&1
call :tick "6  data init (install pipeline)"

REM ===========================================================================
REM  Step 7: verify
REM ===========================================================================
echo.
echo -- Step 7/7: verify ---------------------------------------------------------
set "_BAD=0"
"%VPY%" -c "import sys; print('[OK]   python', sys.version.split()[0], sys.maxsize.bit_length()+1, 'bit')" || set "_BAD=1"
"%VPY%" -c "import qai; print('[OK]   qai from', qai.__file__)" || set "_BAD=1"
"%VPY%" -c "import fastapi, uvicorn, structlog, sqlalchemy, paramiko; print('[OK]   server deps import')" || set "_BAD=1"
"%VPY%" -c "import apps.cli.serve; print('[OK]   apps.cli.serve import')" || set "_BAD=1"
call :check_qab
call :tick "7  verify (imports)"
if "%_BAD%"=="1" goto :fail_verify

if not exist "data\config" mkdir "data\config"
<nul set /p "=%HOST_ARCH%" >"data\config\host_arch"
echo.>>"data\config\host_arch"

if "%WITH_BUILDER%"=="1" (
    echo.
    echo -- Builder: delegating QAIRT SDK / x64 converter venv / VS tools to Setup.bat --builder ...
    call "%ROOT_DIR%Setup.bat" --builder --arch %HOST_ARCH% --no-pause
    call :tick "8  builder (Setup.bat --builder)"
)

call :timing_summary "OK"
if not exist "frontend\dist\index.html" (
    echo.
    echo [WARN] frontend\dist is missing ^(it is git-ignored, so a fresh clone has no WebUI^).
    echo [WARN] Start.bat will serve the API only and GET / returns 404. Build it once with:
    echo [WARN]     build_frontend.bat     ^(installs Node.js + pnpm if needed, ~1-2 min^)
)
echo.
echo  ================================================================
echo      setup_env finished ^(arch=%HOST_ARCH%, profile=%QAI_NET_PROFILE%^)
echo  ================================================================
echo   Build the WebUI:  build_frontend.bat   ^(only if frontend\dist is missing^)
echo   Run the server :  Start.bat
echo   CLI            :  qai.bat --help
echo   Python shell   :  Console.bat
echo   Remove it all  :  clean_env.bat   ^(--help for options^)
echo.
if "%NO_PAUSE%"=="0" pause
endlocal & exit /b 0

REM ===========================================================================
REM  Failure exits
REM ===========================================================================
:fail_uv
echo [ERROR] Could not obtain uv. If github.com is blocked try:  setup_env.bat --mirror cn   or   --proxy http://host:port
goto :fail
:fail_python
echo [ERROR] uv could not install %PY_SPEC%. Try --mirror cn or --proxy.
goto :fail
:fail_venv
echo [ERROR] venv creation failed. On a clean Windows box install the VC++ Redistributable
echo [ERROR] ^(https://aka.ms/vs/17/release/vc_redist.%HOST_ARCH%.exe^); on a WDAC-locked box use Setup.bat instead.
goto :fail
:fail_vendor
echo [ERROR] The ARM64 vendor bundle ^(vendor-deps.7z^) could not be downloaded or unpacked, so the install is stopped:
echo [ERROR] ARM64 needs its wheels ^(aiohttp, cryptography, numpy, qai_appbuilder ...^) and they are not on PyPI.
echo [ERROR]   * Just run setup_env.bat again ^(a damaged archive is deleted automatically^).
echo [ERROR]   * Slow or blocked link: use --proxy http://host:port
echo [ERROR]   * Manual download: %VDEPS_URL%
echo [ERROR]     save it as  %ROOT_DIR%%DL_DIR%\vendor-deps.7z  and run setup_env.bat again.
goto :fail
:fail_install
echo [ERROR] dependency install failed. Re-run is safe ^(resumes^). Try --mirror cn or --proxy http://host:port
goto :fail
:fail_verify
echo [ERROR] verification failed - see the lines above.
goto :fail
:fail
call :tick "(failed step, time until error)"
call :timing_summary "FAILED"
if "%NO_PAUSE%"=="0" pause
endlocal & exit /b 1

:help
echo Usage: setup_env.bat [options]
echo.
echo   --arch arm64^|x64       force architecture ^(default: host_arch file, else detect^)
echo   --proxy URL            HTTP proxy for every download, e.g. http://127.0.0.1:7890
echo                          ^(default: HTTPS_PROXY env, then the Windows system proxy^)
echo   --mirror auto^|official^|cn
echo                          auto ^(default^): use cn mirrors only if pypi.org is unreachable
echo   --no-probe             skip the connectivity probe
echo   --with-browser         Playwright + Chromium ^(~150MB; browser search / browser tool^)
echo   --with-astgrep         ast-grep CLI ^(ast_grep / ast_edit tools^)
echo   --with-apps            App Builder pack deps ^(whisper / melotts / ocr^)
echo   --builder              also run Setup.bat --builder ^(QAIRT SDK ~2GB, converter venv, VS tools^)
echo   --no-pause             do not pause at the end
echo.
echo Environment variables:
echo   QAI_NO_PYPI_EXE=1      do not take uv / ast-grep from their PyPI wheels; use the GitHub releases
echo   QAI_UNPACK=7zr         unpack vendor-deps.7z with 7zr.exe instead of the built-in tar.exe
echo   QAI_GITHUB_MIRROR=URL  prefix for github.com downloads ^(set automatically for --mirror cn, and for auto when github.com is unreachable^)
echo.
echo uv and ast-grep come from PyPI wheels ^(pypi.org, or the tuna mirror first for cn^), so a blocked
echo github.com does not stop the core install. GitHub is still needed for the x64 qai_appbuilder wheel and
echo guard.zip ^(optional FileGuard^); without it on x64 only NPU inference is unavailable.
echo.
echo Installs to the same places as Setup.bat, so Start.bat / qai.bat / Console.bat just work.
exit /b 0

REM ===========================================================================
REM  Subroutines
REM ===========================================================================

REM ---------------------------------------------------------------------------
REM Timing helpers. Centisecond clock from %TIME% (locale-safe: ':' '.' ',' delimiters,
REM leading blank -> 0, 1xx-100 trick avoids octal parsing of 08/09). Midnight wrap handled.
REM ---------------------------------------------------------------------------
:clock
set "_CT=%TIME: =0%"
for /f "tokens=1-4 delims=:.," %%a in ("%_CT%") do set /a "_NOW=((1%%a-100)*3600+(1%%b-100)*60+(1%%c-100))*100+(1%%d-100)"
exit /b 0

:tick_init
call :clock
set "_LAST=%_NOW%"
set "_FIRST=%_NOW%"
exit /b 0

REM :tick <label>  - time since previous checkpoint, attributed to <label>
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

REM :fmt <centiseconds> -> _FMT like "  1m 23.4s" or "    12.3s"
:fmt
set /a "_FS=%~1/100, _FT=(%~1%%100)/10, _FM=_FS/60, _FSS=_FS%%60"
if %_FM% GTR 0 ( set "_FMT=%_FM%m %_FSS%.%_FT%s" ) else ( set "_FMT=%_FSS%.%_FT%s" )
set "_FMT=        %_FMT%"
set "_FMT=%_FMT:~-9%"
exit /b 0

REM :timing_summary <OK|FAILED> - ranked table + append to data\logs\setup_env_timing.log
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
>>"data\logs\setup_env_timing.log" echo === %DATE% %TIME%  arch=%HOST_ARCH%  profile=%QAI_NET_PROFILE%  result=%~1  total_cs=%_TOTAL_CS%
>>"data\logs\setup_env_timing.log" type "%TIMING_FILE%"
exit /b 0

REM :dl <url> <outfile> <minsize> <attempt-timeout-sec>
REM   Honors QAI_GITHUB_MIRROR, HTTPS_PROXY, DL_ZIPTEST=1, and uses aria2c if present.
:dl
set "_U=%~1"
if defined QAI_GITHUB_MIRROR if "%_U:~0,19%"=="https://github.com/" set "_U=%QAI_GITHUB_MIRROR%%_U%"
set "_DLX="
if defined HTTPS_PROXY set "_DLX=-ProxyUrl "%HTTPS_PROXY%""
if "%DL_ZIPTEST%"=="1" set "_DLX=%_DLX% -ZipTest"
set "DL_ZIPTEST="
set "_ARIA="
if exist "data\bin\aria2c\aria2c.exe" set "_ARIA=-Aria2cExe "data\bin\aria2c\aria2c.exe""
echo [INFO] GET %_U%
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" -Url "%_U%" -OutFile "%~2" %_ARIA% -MinSize %~3 -MaxRetries 5 -StallTimeoutSec 60 -AttemptTimeoutSec %~4 %_DLX%
exit /b %errorlevel%

REM :ensure_aria2c - optional multi-connection downloader (~2.5MB); non-fatal. Reuses PATH copy.
:ensure_aria2c
if exist "data\bin\aria2c\aria2c.exe" ( echo [SKIP] aria2c already present. & exit /b 0 )
if not exist "data\bin\aria2c" mkdir "data\bin\aria2c"
set "_ARIA_PATH="
for /f "delims=" %%A in ('where aria2c.exe 2^>nul') do if not defined _ARIA_PATH set "_ARIA_PATH=%%A"
if defined _ARIA_PATH (
    copy /y "%_ARIA_PATH%" "data\bin\aria2c\aria2c.exe" >nul
    if exist "data\bin\aria2c\aria2c.exe" ( echo [SKIP] using aria2c from PATH. & exit /b 0 )
)
echo [INFO] downloading aria2c ^(multi-connection downloader, ~2.5MB^)...
set "DL_ZIPTEST=1"
call :dl "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip" "%DL_DIR%\_aria2c_tmp.zip" 1000000 180
if errorlevel 1 ( echo [WARN] aria2c unavailable; using single-thread download. & exit /b 0 )
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%DL_DIR%\_aria2c_tmp.zip' -DestinationPath '%DL_DIR%\_aria2c_tmp' -Force; Get-ChildItem '%DL_DIR%\_aria2c_tmp' -Recurse -Filter aria2c.exe | Copy-Item -Destination 'data\bin\aria2c\aria2c.exe' -Force; Remove-Item '%DL_DIR%\_aria2c_tmp' -Recurse -Force"
del /f /q "%DL_DIR%\_aria2c_tmp.zip" 2>nul
if exist "data\bin\aria2c\aria2c.exe" ( echo [OK] aria2c ready. ) else ( echo [WARN] aria2c unpack failed; using single-thread download. )
exit /b 0

REM :fetch_vendor - ARM64 only. Downloads vendor-deps.7z (~210MB, ~420MB unpacked) and keeps only what
REM   ARM64 needs: whl, bin\arm64, tiktoken, nltk_data, g2p_data (drops openblas + bin\x64 dups
REM   are kept small).
:fetch_vendor
echo [INFO] ARM64 vendor wheels missing; fetching vendor-deps.7z ^(~210MB download, ~420MB unpacked^)...
if not exist "%DL_DIR%\vendor-deps.7z" (
    call :dl "%VDEPS_URL%" "%DL_DIR%\vendor-deps.7z" 20000000 1800
)
if not exist "%DL_DIR%\vendor-deps.7z" (
    echo [ERROR] vendor-deps.7z download failed.
    exit /b 1
)
REM Unpack: the Windows built-in tar.exe ^(bsdtar^) reads .7z, so no extra download. The archive uses an
REM ARM64 BCJ filter that older bsdtar builds may not know, so the result is verified and 7zr.exe
REM ^(downloaded only then^) is the fallback. Full path on purpose: a GNU tar earlier on PATH cannot read 7z.
REM QAI_UNPACK=7zr forces 7zr.exe.
set "_VD=%DL_DIR%\_vd_tmp"
set "_UNPACKED="
if exist "%_VD%" rmdir /s /q "%_VD%"
if /i not "%QAI_UNPACK%"=="7zr" if exist "%SystemRoot%\System32\tar.exe" (
    echo [INFO] unpacking with built-in tar.exe ...
    mkdir "%_VD%"
    "%SystemRoot%\System32\tar.exe" -xf "%DL_DIR%\vendor-deps.7z" -C "%_VD%"
    if not errorlevel 1 if exist "%_VD%\whl" set "_UNPACKED=tar"
    if not errorlevel 1 if exist "%_VD%\vendor\whl" set "_UNPACKED=tar"
    if not defined _UNPACKED (
        echo [WARN] tar.exe could not unpack the bundle; trying 7zr.exe instead.
        rmdir /s /q "%_VD%" 2>nul
    )
)
if not defined _UNPACKED call :unpack_7zr
if not defined _UNPACKED (
    echo [ERROR] vendor-deps.7z could not be unpacked ^(damaged download?^). The file was deleted so the next run fetches a fresh copy.
    del /f /q "%DL_DIR%\vendor-deps.7z" 2>nul
    rmdir /s /q "%_VD%" 2>nul
    exit /b 1
)
if not exist "vendor" mkdir "vendor"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$t='%_VD%'; $s= if (Test-Path (Join-Path $t 'vendor')) { Join-Path $t 'vendor' } else { $t }; foreach ($n in 'whl','tiktoken','nltk_data','g2p_data') { $p=Join-Path $s $n; if (Test-Path $p) { Copy-Item -LiteralPath $p -Destination 'vendor' -Recurse -Force } }; New-Item -ItemType Directory -Force 'vendor\bin\arm64' | Out-Null; $b=Join-Path $s 'bin\arm64'; if (Test-Path $b) { Copy-Item -Path (Join-Path $b '*') -Destination 'vendor\bin\arm64' -Recurse -Force }"
rmdir /s /q "%_VD%" 2>nul
if not exist "vendor\whl\%QAI_AB_WHL_FILE%" (
    echo [ERROR] the unpacked bundle has no vendor\whl\%QAI_AB_WHL_FILE% ^(wrong or incomplete archive^).
    del /f /q "%DL_DIR%\vendor-deps.7z" 2>nul
    exit /b 1
)
del /f /q "%DL_DIR%\vendor-deps.7z" 2>nul
echo [OK]   vendor bundle merged.
exit /b 0

REM :unpack_7zr - fallback unpacker: fetch 7zr.exe ^(~0.6MB^) if needed, unpack into %_VD%. Sets _UNPACKED=7zr on success.
:unpack_7zr
if not exist "data\bin\7zr" mkdir "data\bin\7zr"
if not exist "data\bin\7zr\7zr.exe" (
    call :dl "https://www.7-zip.org/a/7zr.exe" "data\bin\7zr\7zr.exe" 100000 60
)
if not exist "data\bin\7zr\7zr.exe" ( echo [WARN] no 7zr.exe available. & exit /b 0 )
echo [INFO] unpacking with 7zr.exe ...
if exist "%_VD%" rmdir /s /q "%_VD%"
"data\bin\7zr\7zr.exe" x -y "-o%_VD%" "%DL_DIR%\vendor-deps.7z" -bso0 -bsp0
if errorlevel 1 exit /b 0
if exist "%_VD%\whl" set "_UNPACKED=7zr"
if exist "%_VD%\vendor\whl" set "_UNPACKED=7zr"
exit /b 0

REM :fetch_guard - guard64.dll (+ ARM64 injector); small, non-fatal. Skipped when present.
:fetch_guard
if exist "vendor\bin\%HOST_ARCH%\guard64.dll" (
    echo [SKIP] guard64.dll present.
    exit /b 0
)
if not exist "vendor\bin\arm64" mkdir "vendor\bin\arm64"
if not exist "vendor\bin\x64"   mkdir "vendor\bin\x64"
set "DL_ZIPTEST=1"
call :dl "%RELEASE_BASE%/guard.zip" "%DL_DIR%\guard.zip" 300000 300
if errorlevel 1 ( echo [WARN] guard.zip unavailable; native FileGuard inactive. & exit /b 0 )
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%DL_DIR%\guard.zip' -DestinationPath '%DL_DIR%\_guard_tmp' -Force; foreach ($f in 'arm64\guard64.dll','x64\guard64.dll','arm64\guard-injector.exe') { $s=Join-Path '%DL_DIR%\_guard_tmp' $f; if (Test-Path $s) { Copy-Item $s (Join-Path 'vendor\bin' $f) -Force } }; Remove-Item '%DL_DIR%\_guard_tmp' -Recurse -Force"
del /f /q "%DL_DIR%\guard.zip" 2>nul
exit /b 0

REM :pypi_exe <package> <platform> <exe,exe> <outdir> <version|""> - take native exes out of a PyPI wheel
REM   (scripts\setup\fetch_pypi_exe.ps1). Never fatal: the caller checks the exe and falls back to GitHub.
REM   Index order follows QAI_NET_PROFILE (cn = tuna first). QAI_NO_PYPI_EXE=1 skips this path.
:pypi_exe
if "%QAI_NO_PYPI_EXE%"=="1" ( echo [SKIP] QAI_NO_PYPI_EXE=1: not using PyPI wheels for %~1. & exit /b 0 )
set "_PF=-Profile official"
if /i "%QAI_NET_PROFILE%"=="cn" set "_PF=-Profile cn"
set "_PA="
if exist "data\bin\aria2c\aria2c.exe" set "_PA=-Aria2cExe "data\bin\aria2c\aria2c.exe""
set "_PP="
if defined HTTPS_PROXY set "_PP=-ProxyUrl "%HTTPS_PROXY%""
set "_PV="
if not "%~5"=="" set "_PV=-Version %~5"
echo [INFO] %~1 from PyPI wheel ^(%~2^)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\fetch_pypi_exe.ps1" -Package "%~1" -Platform "%~2" -Exe "%~3" -OutDir "%~4" -WorkDir "%DL_DIR%" %_PV% %_PF% %_PA% %_PP%
if errorlevel 1 echo [WARN] PyPI wheel path failed for %~1; falling back to the GitHub release.
exit /b 0

REM :fetch_astgrep - opt-in (--with-astgrep).
:fetch_astgrep
if exist "vendor\bin\%HOST_ARCH%\ast-grep.exe" (
    echo [SKIP] ast-grep present.
    exit /b 0
)
if not exist "vendor\bin\%HOST_ARCH%" mkdir "vendor\bin\%HOST_ARCH%"
REM Pinned 0.45.1 (same as the GitHub fallback); PyPI wheel first, no GitHub needed.
call :pypi_exe ast-grep-cli %WHL_PLAT% "ast-grep.exe,sg.exe" "vendor\bin\%HOST_ARCH%" 0.45.1
if exist "vendor\bin\%HOST_ARCH%\ast-grep.exe" exit /b 0
echo [INFO] ast-grep: PyPI wheel unavailable, trying the GitHub release...
set "DL_ZIPTEST=1"
call :dl "https://github.com/ast-grep/ast-grep/releases/download/0.45.1/%ASTGREP_ASSET%" "%DL_DIR%\ast-grep.zip" 5000000 300
if errorlevel 1 ( echo [WARN] ast-grep unavailable; ast_grep / ast_edit tools disabled. & exit /b 0 )
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%DL_DIR%\ast-grep.zip' -DestinationPath '%DL_DIR%\_ag_tmp' -Force; foreach ($n in 'ast-grep.exe','sg.exe') { $s=Join-Path '%DL_DIR%\_ag_tmp' $n; if (Test-Path $s) { Copy-Item $s 'vendor\bin\%HOST_ARCH%' -Force } }; Remove-Item '%DL_DIR%\_ag_tmp' -Recurse -Force"
del /f /q "%DL_DIR%\ast-grep.zip" 2>nul
exit /b 0

REM :check_qab - import qai_appbuilder and, when that fails, SHOW the real error (it used to be hidden by 2^>nul).
REM   Not fatal: the Model Builder UI starts without it, only NPU inference is unavailable.
:check_qab
set "_QF=%DL_DIR%\_qab_check.txt"
"%VPY%" -c "import qai_appbuilder as q; print('[OK]   qai_appbuilder', getattr(q,'__version__','?'))" >"%_QF%" 2>&1
if not errorlevel 1 ( type "%_QF%" & del /f /q "%_QF%" 2>nul & exit /b 0 )
echo [WARN] qai_appbuilder not importable ^(NPU inference unavailable; Model Builder UI still starts^). Python said:
type "%_QF%"
findstr /i /c:"DLL load failed" "%_QF%" >nul 2>&1 && (
    echo [WARN] A native DLL of qai_appbuilder did not load. Usual causes:
    echo [WARN]   * missing / old Microsoft Visual C++ runtime: install https://aka.ms/vs/17/release/vc_redist.%HOST_ARCH%.exe
    echo [WARN]   * an Application Control / AppLocker / antivirus policy blocked the unsigned .pyd/.dll under
    echo [WARN]     %VENV_DIR%\Lib\site-packages\qai_appbuilder ^(Event Viewer: Microsoft-Windows-CodeIntegrity^)
    echo [WARN]   * wrong architecture: this venv is %HOST_ARCH%; the wheel must match ^(win_%HOST_ARCH:x64=amd64%^)
    echo [WARN]   * "initialization routine failed" ^(WinError 1114^): the DLL was found but its start-up code failed - a
    echo [WARN]     runtime-version clash or a CPU/OS feature it needs; the diagnostic report below shows which
)
findstr /i /c:"No module named" "%_QF%" >nul 2>&1 && (
    echo [WARN] A Python dependency of qai_appbuilder is missing: install it with
    echo [WARN]     "%UV_EXE%" pip install --python "%VPY%" pyyaml "numpy<=2.4.4"
)
echo [WARN] Full output saved in %_QF% ^(also re-check with: Console.bat  then  import qai_appbuilder^)
REM Collect the facts needed to explain it (exact Windows error per DLL, runtime DLLs really loaded,
REM signatures, Code Integrity / AppLocker / Defender events). Read-only, a few seconds.
echo [INFO] collecting a diagnostic report ^(scripts\setup\diag_qab.ps1^) ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\diag_qab.ps1" -Python "%VPY%" -OutFile "data\logs\qab_diag.txt" >nul 2>&1
if exist "data\logs\qab_diag.txt" echo [WARN] Diagnostic report: %ROOT_DIR%data\logs\qab_diag.txt  ^(send this file when asking for help^)
exit /b 0

REM :install_vendor_whls - ARM64 only; same 14 wheels / pins as Setup.bat Step 4a.
:install_vendor_whls
set "W=vendor\whl"
"%UV_EXE%" pip install --no-deps "%W%\qai_appbuilder-2.48.40-cp313-cp313-win_arm64.whl" "%W%\claude_agent_sdk-0.1.72-py3-none-win_arm64.whl" "%W%\ripgrep-15.0.0-py3-none-win_arm64.whl" "%W%\kaldi_native_fbank-1.22.3-cp313-cp313-win_arm64.whl" "%W%\pyclipper-1.4.0-cp313-cp313-win_arm64.whl" "%W%\tiktoken-0.12.0-cp313-cp313-win_arm64.whl" "%W%\opencv_python_headless-4.10.0.84-cp313-cp313-win_arm64.whl" "%W%\httptools-0.7.1-cp313-cp313-win_arm64.whl" "%W%\numpy-2.3.1-cp313-cp313-win_arm64.whl" "%W%\aiohttp-3.13.5-cp313-cp313-win_arm64.whl" "%W%\cryptography-45.0.5-cp313-abi3-win_arm64.whl" "%W%\MarkupSafe-2.1.5-cp313-cp313-win_arm64.whl" "%W%\soundfile-0.13.1-cp313-cp313-win_arm64.whl" "%W%\grimp-3.14-cp313-cp313-win_arm64.whl"
if errorlevel 1 exit /b 1
(
    echo aiohttp==3.13.5
    echo cryptography==45.0.5
    echo httptools==0.7.1
    echo MarkupSafe==2.1.5
    echo numpy==2.3.1
    echo opencv-python-headless==4.10.0.84
    echo qai_appbuilder==2.48.40
    echo soundfile==0.13.1
    echo tiktoken==0.12.0
    echo grimp==3.14
    echo claude-agent-sdk==0.1.72
    echo ripgrep==15.0.0
    echo kaldi-native-fbank==1.22.3
    echo pyclipper==1.4.0
) > "data\downloads\_constraints.txt"
exit /b 0

REM :install_qai_appbuilder
:install_qai_appbuilder
if /i "%HOST_ARCH%"=="arm64" goto :qab_arm64

REM ---- x64: GitHub release wheel %QAI_AB_WHL_VERSION% only (never PyPI, see the note at the top) ----
REM Skip only when exactly this version is installed. A bare prefix test on 2.48.40 would also accept
REM the PyPI ARM64EC wheel (2.48.40) and leave a broken package in place. Read the version from the
REM package metadata, not from import, so a wheel that cannot be imported is still recognised.
"%VPY%" -c "import importlib.metadata as m, sys; sys.exit(0 if m.version('qai_appbuilder')=='%QAI_AB_WHL_VERSION%' else 1)" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] qai_appbuilder %QAI_AB_WHL_VERSION% already installed.
    exit /b 0
)
set "_WN=%QAI_AB_WHL_FILE%"
REM A cached file below MinSize is a partial download from an earlier run: drop it and fetch again.
if exist "%DL_DIR%\%_WN%" for %%F in ("%DL_DIR%\%_WN%") do if %%~zF LSS 500000 del /f /q "%DL_DIR%\%_WN%"
if not exist "%DL_DIR%\%_WN%" (
    echo [INFO] downloading qai_appbuilder %QAI_AB_WHL_VERSION% ^(x64^) from the GitHub release...
    call :dl "%RELEASE_BASE%/%_WN%" "%DL_DIR%\%_WN%" 500000 300
)
if not exist "%DL_DIR%\%_WN%" goto :qab_fail
"%UV_EXE%" pip install --reinstall --no-deps "%DL_DIR%\%_WN%"
if errorlevel 1 goto :qab_fail
echo [OK]   qai_appbuilder %QAI_AB_WHL_VERSION% installed ^(x64, GitHub release^).
exit /b 0

:qab_arm64
REM ---- ARM64: vendored wheel (offline-safe), version %QAI_AB_WHL_VERSION% ----
"%VPY%" -c "import importlib.metadata as m, sys; sys.exit(0 if m.version('qai_appbuilder')=='%QAI_AB_WHL_VERSION%' else 1)" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] qai_appbuilder %QAI_AB_WHL_VERSION% already installed.
    exit /b 0
)
if exist "vendor\whl\%QAI_AB_WHL_FILE%" (
    "%UV_EXE%" pip install --reinstall --no-deps "vendor\whl\%QAI_AB_WHL_FILE%"
    if not errorlevel 1 exit /b 0
)
:qab_fail
echo [WARN] qai_appbuilder not installed; NPU inference unavailable until it is.
exit /b 0
