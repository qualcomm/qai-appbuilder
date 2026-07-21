@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM QAIModelBuilder - One-click Python environment setup (uv)
REM Downloads uv, installs Python 3.13 ARM64 native, creates venv in %LOCALAPPDATA%\QAIModelBuilder\envs, installs dependencies.

REM NOTE: Do NOT use "chcp 65001" here - it causes cmd.exe to silently drop leading
REM       characters from command output on some Windows versions (known OS bug).

echo.
echo  +--------------------------------------------------+
echo  ^|   QAI ModelBuilder  -  Setup Python Environment  ^|
echo  +--------------------------------------------------+
echo.

set "ROOT_DIR=%~dp0"

REM Route Python bytecode caches out of the source tree into data\caches\pycache
REM (keeps the source tree clean; data\ is the per-user runtime root and is
REM git-ignored). %~dp0 has a trailing backslash so no extra separator is needed.
set "PYTHONPYCACHEPREFIX=%~dp0data\caches\pycache"

REM Switch to the script's own directory so all relative paths work correctly,
REM and uv does not receive absolute paths with spaces or CJK characters.
cd /d "%ROOT_DIR%"

REM --- Argument parsing -------------------------------------------------------
REM   --no-builder : skip the QAIRT model-builder environment (Step 8, ~2GB SDK
REM                  download + x64 venv + VS build tools). The base runtime
REM                  (pre-built models) does NOT need it.
REM   --no-pause   : do not pause at the end (for non-interactive / scripted runs).
REM This script merges the former V1 Setup_Builder_Env.bat in full (Step 8);
REM everything runs in-process via scripts\setup\setup_qairt_env.py and
REM scripts\setup\install_vs.ps1 - it does NOT depend on the qai.exe CLI.
REM   --dev        : also install the dev + e2e extras (pytest / mypy / ruff /
REM                  pre-commit / import-linter / playwright + the ~150MB
REM                  Chromium browser). These are for CONTRIBUTORS running the
REM                  test suite only; a normal end-user install does NOT need
REM                  them and should not pay the extra download. Default OFF.
set "NO_BUILDER=0"
set "NO_PAUSE=0"
set "DEV_EXTRAS=0"
set "DESKTOP_EXTRAS=0"
:parse_setup_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--help" goto :print_help
if /i "%~1"=="-h" goto :print_help
if /i "%~1"=="/?" goto :print_help
if /i "%~1"=="--no-builder" ( set "NO_BUILDER=1" & shift & goto :parse_setup_args )
if /i "%~1"=="--no-pause" ( set "NO_PAUSE=1" & shift & goto :parse_setup_args )
if /i "%~1"=="--dev" ( set "DEV_EXTRAS=1" & shift & goto :parse_setup_args )
if /i "%~1"=="--desktop" ( set "DESKTOP_EXTRAS=1" & shift & goto :parse_setup_args )
shift & goto :parse_setup_args
:args_done

REM Clear deprecated UV_NATIVE_TLS if set by old activate scripts or parent shell
set "UV_NATIVE_TLS="

set "_T=%TIME: =0%"
for /f "tokens=1-3 delims=:." %%a in ("%_T%") do set /a "_START_S=(1%%a-100)*3600+(1%%b-100)*60+(1%%c-100)"

REM All paths below are relative to ROOT_DIR (current directory after cd above).
REM Install-time temporary downloads (uv / aria2c / PortableGit / QAIRT SDK /
REM vendor-deps archives + aria2c logs) live under data\downloads so they sit
REM beside the runtime data root. Temp archives are deleted right after
REM extraction. NOTE: Uninstall.bat does NOT delete data\ (it is user data:
REM qai.db, logs, downloaded model weights, config), so do not rely on the
REM uninstaller to clean these up -- delete temp archives inline after use.
set "DL_DIR=data\downloads"
set "UV_BIN_DIR=data\bin\uv"
set "UV_EXE=data\bin\uv\uv.exe"
set "UV_URL=https://github.com/astral-sh/uv/releases/latest/download/uv-aarch64-pc-windows-msvc.zip"
set "UV_ZIP=data\downloads\_uv_tmp.zip"
set "VENV_DIR=%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313"
set "VENDOR_DIR=vendor"
set "WHL_DIR=vendor\whl"
REM Only ARM64-native wheels that PyPI does NOT provide (or has revoked
REM for cp313) are stored in vendor\whl\.  Everything else is fetched
REM from PyPI by uv pip install (uv reads pyproject.toml [project] +
REM [project.optional-dependencies]).  Keep this list in sync with
REM vendor\whl\README.md.
REM
REM IMPORTANT: do NOT delete files from vendor\whl\.  Three wheels
REM (aiohttp 3.13.5 / cryptography 45.0.5 / MarkupSafe 2.1.5) have been
REM revoked from PyPI for cp313 ARM64 and the local copies are the only
REM source.  See vendor\whl\README.md.
set "WHL_QAI_APPBUILDER=vendor\whl\qai_appbuilder-2.48.40-cp313-cp313-win_arm64.whl"
set "WHL_CLAUDE_AGENT_SDK=vendor\whl\claude_agent_sdk-0.1.72-py3-none-win_arm64.whl"
set "WHL_RIPGREP=vendor\whl\ripgrep-15.0.0-py3-none-win_arm64.whl"
set "WHL_KALDI=vendor\whl\kaldi_native_fbank-1.22.3-cp313-cp313-win_arm64.whl"
set "WHL_PYCLIPPER=vendor\whl\pyclipper-1.4.0-cp313-cp313-win_arm64.whl"
set "WHL_TIKTOKEN=vendor\whl\tiktoken-0.12.0-cp313-cp313-win_arm64.whl"
set "WHL_OPENCV=vendor\whl\opencv_python_headless-4.10.0.84-cp313-cp313-win_arm64.whl"
set "WHL_HTTPTOOLS=vendor\whl\httptools-0.7.1-cp313-cp313-win_arm64.whl"
set "WHL_NUMPY=vendor\whl\numpy-2.3.1-cp313-cp313-win_arm64.whl"
set "WHL_AIOHTTP=vendor\whl\aiohttp-3.13.5-cp313-cp313-win_arm64.whl"
set "WHL_CRYPTOGRAPHY=vendor\whl\cryptography-45.0.5-cp313-abi3-win_arm64.whl"
set "WHL_MARKUPSAFE=vendor\whl\MarkupSafe-2.1.5-cp313-cp313-win_arm64.whl"
set "WHL_SOUNDFILE=vendor\whl\soundfile-0.13.1-cp313-cp313-win_arm64.whl"
set "WHL_GRIMP=vendor\whl\grimp-3.14-cp313-cp313-win_arm64.whl"

if not exist "data\bin\uv" mkdir "data\bin\uv"
if not exist "data\bin\aria2c" mkdir "data\bin\aria2c"
if not exist "data\bin\7zr" mkdir "data\bin\7zr"
if not exist "%LOCALAPPDATA%\QAIModelBuilder\envs" mkdir "%LOCALAPPDATA%\QAIModelBuilder\envs"
if not exist "%DL_DIR%" mkdir "%DL_DIR%"

REM --- Step 0: Download aria2c if not present (needed for fast multi-thread downloads) ---
REM
REM aria2c is the LAUNCHPAD: every other download in this script (uv,
REM PortableGit, Node.js, vendor-deps, voice/TTS models, the 2GB QAIRT SDK)
REM goes through it via scripts\setup\download_with_aria2c.ps1, which wraps
REM aria2c with retry+stall-watchdog+integrity-check. So aria2c.exe itself
REM is the only download that CANNOT use that wrapper's aria2c path -- it
REM has to come down via the PowerShell single-thread fallback.
REM Even so, we route it through the SAME wrapper so:
REM   1) the PS fallback gets retry + size-check protection,
REM   2) aria2c failing here is non-fatal (script logs [WARN] and continues
REM      to the PowerShell-only path for everything downstream).
if exist "data\bin\aria2c\aria2c.exe" goto :aria2c_ready

echo [INFO] Downloading aria2c ^(multi-thread downloader^)...
set "ARIA2C_URL=https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
set "ARIA2C_ZIP=%DL_DIR%\_aria2c_tmp.zip"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%ARIA2C_URL%" ^
    -OutFile "%ARIA2C_ZIP%" ^
    -MinSize 1000000 ^
    -ZipTest ^
    -MaxRetries 3 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 180
if errorlevel 1 (
    echo [WARN] Failed to download aria2c. Will use single-thread downloads.
    del /f /q "%ARIA2C_ZIP%" 2>nul
    goto :aria2c_ready
)

echo [INFO] Extracting aria2c.exe...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Path '%ARIA2C_ZIP%' -DestinationPath '%DL_DIR%\_aria2c_tmp' -Force; " ^
    "Get-ChildItem '%DL_DIR%\_aria2c_tmp' -Recurse -Filter 'aria2c.exe' | Copy-Item -Destination 'data\bin\aria2c\aria2c.exe' -Force; " ^
    "Remove-Item '%DL_DIR%\_aria2c_tmp' -Recurse -Force"
del /f /q "%ARIA2C_ZIP%" 2>nul

if exist "data\bin\aria2c\aria2c.exe" (
    echo [OK]   aria2c installed: data\bin\aria2c\aria2c.exe
) else (
    echo [WARN] aria2c extraction failed. Will use single-thread downloads.
)

:aria2c_ready

REM --- Step 0b: Fetch + merge vendor dependency bundle (vendor-deps.7z) ---
REM
REM Release artifacts do NOT ship the heavy vendor/ dependency caches
REM (vendor\whl\ ARM64 wheels + vendor\g2p_data\ + vendor\nltk_data\ +
REM vendor\tiktoken\); see scripts\release\manifest.toml [exclude]. They are
REM published as a single downloadable archive and merged into vendor\ here,
REM BEFORE Step 4a installs the ARM64-only wheels from vendor\whl\.
REM
REM Idempotent: if all four dirs already exist (dev checkout, or a prior run),
REM the download is skipped. Non-fatal: a failure only warns; if vendor\whl\
REM ends up missing, Step 4a is skipped and uv falls back to PyPI.
call :fetch_vendor_deps

REM --- Step 0c: Download guard64.dll (native file-guard DLL, ARM64 + x64) ---
REM
REM guard64.dll is NOT shipped in the repository (binary artifact, excluded from
REM source control). It is published as guard.zip on the GitHub release page and
REM must be downloaded and extracted into vendor\bin\{arm64,x64}\ AFTER the
REM vendor-deps bundle (Step 0b) has been merged, so that vendor\bin\ already
REM exists and the DLL lands in the correct location.
REM
REM Idempotent: skipped when both DLLs are already present (dev checkout or
REM prior run). Non-fatal: a failure only warns; the app still starts but the
REM native file-guard hook will be inactive.
call :fetch_guard_dlls

REM --- Step 1: Download uv if not present ---
REM
REM Goes through download_with_aria2c.ps1 which: tries aria2c first (when
REM Step 0 produced data\bin\aria2c\aria2c.exe), falls back to single-thread
REM PowerShell otherwise. Both paths get retry + stall-watchdog + size+zip
REM integrity checks (the bare Invoke-WebRequest this used to be had no
REM retries and no size verification -- a corrupt 0-byte download would
REM pass straight through to extraction and crash there).
if exist "%UV_EXE%" goto :uv_ready

echo [INFO] Downloading uv ^(ARM64^)...
echo [INFO] URL: %UV_URL%

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%UV_URL%" ^
    -OutFile "%UV_ZIP%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 5000000 ^
    -ZipTest ^
    -MaxRetries 5 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 300
if errorlevel 1 goto :download_error

echo [INFO] Extracting uv.exe...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path \"%UV_ZIP%\" -DestinationPath \"%UV_BIN_DIR%\" -Force"
if errorlevel 1 goto :extract_error

del /f /q "%UV_ZIP%" 2>nul
echo [INFO] uv installed successfully.

:uv_ready
set "PATH=%UV_BIN_DIR%;%PATH%"

REM Use system certificates so uv trusts the corporate proxy certificate store
set "UV_SYSTEM_CERTS=1"

REM --- Step 2: Install Python 3.13 (ARM64 native) ---
REM Idempotent: skip the (re)install when uv can already resolve a managed
REM cpython-3.13 ARM64 interpreter. ``uv python find`` returns exit 0 + the
REM interpreter path when present; only then do we avoid the slower
REM ``uv python install`` round-trip (which is itself idempotent but prints
REM "already installed" and re-resolves every run).
REM NOTE: use a goto-based branch (not if/else blocks) so the literal "(ARM64)"
REM in the echo text below is never parsed as a cmd block delimiter.
uv python find cpython-3.13-windows-aarch64 >nul 2>&1
if not errorlevel 1 goto :py313_ready
echo [INFO] Installing Python 3.13 ARM64...
REM --no-bin: skip writing python3.13.exe shim into ~/.local/bin (we use the
REM venv's python.exe directly; the shim PATH-warning + leftover-shim
REM "Failed to install executable" line otherwise alarm users for no reason).
REM --no-registry: skip Windows registry registration (one fewer thing to
REM clean up at uninstall time; the venv reference works without it).
uv python install --no-bin --no-registry cpython-3.13-windows-aarch64
if errorlevel 1 goto :python_error
goto :py313_done
:py313_ready
echo [SKIP] Python 3.13 ARM64 already installed ^(uv-managed^).
:py313_done

REM --- Step 3: Create virtual environment ---
REM Only create venv if python.exe is missing. If it exists, verify it is
REM COMPLETE (not merely that python.exe runs).
REM
REM State-Truth-First (AGENTS.md ??????1): probe the REAL completion markers,
REM not a weak proxy. The old check only ran ``python -c "import sys"`` -- but
REM ``import sys`` is the stdlib and succeeds even on a HALF-created venv that
REM has python.exe yet is missing ``Scripts\activate.bat`` and pip (e.g. a
REM ``uv venv`` interrupted before it wrote the activation scripts, or a venv
REM created without ``--seed``). That false "functional" verdict made Setup
REM SKIP the rebuild, then ``call ...\activate.bat`` (Step :venv_ready) failed
REM with "is not recognized" and the whole ``uv pip install`` aborted -- a
REM silent install failure on a venv Setup itself declared healthy.
REM
REM Fix: require BOTH ``Scripts\activate.bat`` to exist AND ``python -m pip
REM --version`` to succeed before trusting the venv. If EITHER is missing the
REM venv is incomplete; we fall through to the rebuild below (``uv venv --seed
REM --allow-existing`` re-creates the activation scripts + pip in place).
if exist "%VENV_DIR%\Scripts\python.exe" if exist "%VENV_DIR%\Scripts\activate.bat" (
    "%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
    if not errorlevel 1 (
        echo [SKIP] Virtual environment already exists and is complete: %VENV_DIR%
        goto :venv_ready
    )
)
if exist "%VENV_DIR%\Scripts\python.exe" echo [WARN] Existing venv is incomplete (missing activate.bat / pip), recreating...

echo [INFO] Creating virtual environment ^(.venv^)...
REM --seed: automatically installs pip/setuptools/wheel into the venv.
REM --allow-existing: don't fail if directory already exists (just recreate scripts).
uv venv "%VENV_DIR%" --python cpython-3.13-windows-aarch64 --seed --allow-existing
if errorlevel 1 (
    REM If --allow-existing not supported by this uv version, try without it
    uv venv "%VENV_DIR%" --python cpython-3.13-windows-aarch64 --seed 2>nul
    if errorlevel 1 (
        REM Last resort: only proceed if the venv is actually USABLE -- it
        REM must have BOTH python.exe AND activate.bat (Step :venv_ready calls
        REM activate.bat next; proceeding with python.exe alone re-introduces
        REM the "is not recognized" silent-failure this guard exists to stop).
        if exist "%VENV_DIR%\Scripts\python.exe" if exist "%VENV_DIR%\Scripts\activate.bat" (
            echo [WARN] uv venv reported an error, but a usable venv exists. Continuing...
            goto :venv_ready
        )
        goto :venv_error
    )
)

:venv_ready
REM Activate the venv so all subsequent "uv pip install" calls use it automatically,
REM avoiding --python with an absolute path that may contain spaces or CJK characters.
call "%VENV_DIR%\Scripts\activate.bat"

REM --- Check if dependencies are already installed (skip reinstall) ---
REM State-Truth-First (AGENTS.md): probe the REAL completion marker, not a
REM proxy. The old check imported only ``fastapi`` -- but fastapi is one of
REM the FIRST packages to land during ``uv pip install -e .``, so a run that
REM was interrupted after fastapi but before the rest (structlog, ..., and the
REM editable ``qai`` package whose .pth / dist-info are written LAST) would
REM leave a HALF-installed venv that still passed ``import fastapi`` -- and the
REM skip below then made that broken state permanent (Setup printed SUCCESS but
REM Start.bat crashed with ``No module named 'structlog' / 'qai'``).
REM
REM Fix: require the WHOLE closure to import -- including ``qai`` (the editable
REM package, which only resolves once ``pip install -e .`` fully completed) and
REM ``structlog`` (a late-installed runtime dep). If ANY is missing we do NOT
REM skip; we fall through and (re)run the install. ``uv pip install`` is
REM idempotent, so already-present packages are skipped and only the missing
REM ones are fetched -- a re-run is cheap and self-heals a half install.
REM
REM ``claude_agent_sdk`` is included in the probe too: Step 4a installs its
REM vendored wheel with ``--no-deps`` and Step 4b2 resolves its ``mcp`` (and
REM sub-deps) via the ``[cc-sdk]`` extra. A venv left by an OLDER Setup.bat that
REM installed the SDK wheel but never pulled ``mcp`` imports ``fastapi/.../qai``
REM fine yet fails ``import claude_agent_sdk`` (``No module named 'mcp'``) and
REM degrades the CC backend to pure-HTTP. Probing it here makes such a venv
REM fall through to the (idempotent) install so the missing ``mcp`` is healed.
"%VENV_DIR%\Scripts\python.exe" -c "import fastapi, uvicorn, structlog, qai, claude_agent_sdk" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] Dependencies already installed and complete ^(fastapi, uvicorn, structlog, qai, claude_agent_sdk^). Skipping pip steps.
    goto :skip_all_installs
)
echo [INFO] Dependency closure incomplete or missing; running full install to ^(re^)complete it...

REM --- Step 4a: Pre-install ARM64-only wheels from vendor\whl\ ---
REM
REM TODO(release vendor bootstrap): In release artifacts the `vendor/` tree is
REM   NOT shipped (manifest.toml [exclude] vendor/). The plan is to package
REM   vendor/ as a separate downloadable archive and, on install, fetch +
REM   extract it BEFORE this step so `%WHL_DIR%` exists on the end-user box.
REM   That download/extract bootstrap is not implemented yet; until it lands,
REM   this step is skipped when `%WHL_DIR%` is absent (see the guard below),
REM   and on dev machines it uses the in-repo vendor/whl/ directly.
REM
REM These wheels are NOT available on PyPI for win_arm64 cp313 (or have
REM been revoked); pip would otherwise fall back to building them from
REM sdist (and fail because the dev box has no native toolchain).
REM
REM ``--no-deps`` is critical: a couple of these wheels declare
REM transitive dependencies that PyPI only ships as sdist on ARM64
REM (e.g. claude-agent-sdk -> mcp -> pyjwt[crypto] -> cryptography sdist
REM for newer versions).  We just want the wheel file unpacked into the
REM venv; the proper resolution of all transitives happens in step 4b2.
REM
REM IMPORTANT: vendor\whl\ files are precious - three wheels
REM (aiohttp 3.13.5 / cryptography 45.0.5 / MarkupSafe 2.1.5) have been
REM revoked from PyPI for cp313 ARM64 and the local copies are the only
REM source.  Do NOT delete files from vendor\whl\.  See vendor\whl\README.md.
if not exist "%WHL_DIR%" goto :skip_vendor_whls
echo [INFO] Installing pre-built ARM64 wheels from %WHL_DIR%...
uv pip install --no-deps ^
    "%WHL_QAI_APPBUILDER%" ^
    "%WHL_CLAUDE_AGENT_SDK%" ^
    "%WHL_RIPGREP%" ^
    "%WHL_KALDI%" ^
    "%WHL_PYCLIPPER%" ^
    "%WHL_TIKTOKEN%" ^
    "%WHL_OPENCV%" ^
    "%WHL_HTTPTOOLS%" ^
    "%WHL_NUMPY%" ^
    "%WHL_AIOHTTP%" ^
    "%WHL_CRYPTOGRAPHY%" ^
    "%WHL_MARKUPSAFE%" ^
    "%WHL_SOUNDFILE%" ^
    "%WHL_GRIMP%"
if errorlevel 1 goto :install_error

REM Pin the vendored versions so step 4b's resolver does not try to
REM upgrade them and rebuild from sdist.
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
) > "_constraints_tmp.txt"

:skip_vendor_whls
REM --- Step 4b: Install all runtime dependencies (from pyproject.toml) ---
REM
REM The legacy requirements.txt was removed in S8 PR-084; pyproject.toml
REM [project].dependencies is now the single source of truth for runtime
REM deps. They are installed by step 4b2's editable install (`pip install
REM -e .`) below, so there is no separate `-r requirements.txt` step here.
echo.
echo [INFO] Installing dependencies from pyproject.toml...
echo [INFO] ^(First run may take a few minutes^)
echo.

REM --- Step 4b2: Editable install (runtime deps + entry points + src layout) ---
REM
REM ``pip install -e .`` reads pyproject.toml [project].dependencies and
REM installs all runtime dependencies, registers console-script entry points
REM (qai, qai-serve, qai-uninstall - see pyproject.toml [project.scripts]
REM for the single-entry strategy) and the ``src/`` package in develop mode.
REM
REM The ``[cc-sdk]`` extra is included so the Claude Code SDK backend
REM (``claude_agent_sdk``, V1 file checkpoint/rewind parity) is actually
REM USABLE: Step 4a pre-installs the vendored ARM64 ``claude_agent_sdk`` wheel
REM with ``--no-deps``, which deliberately skips its transitive dependencies
REM (chiefly ``mcp``). Without this extra, ``import claude_agent_sdk`` fails at
REM ``from mcp.types import ToolAnnotations`` with ``No module named 'mcp'`` and
REM the CC backend silently degrades to the pure-HTTP adapter. Resolving the
REM ``[cc-sdk]`` extra here pulls ``mcp`` (and its sub-deps: httpx-sse /
REM jsonschema / pyjwt / pywin32 / sse-starlette / rpds-py, all wheels on PyPI
REM / vendor\whl) so the SDK imports cleanly. ``claude_agent_sdk`` itself stays
REM satisfied by the already-installed vendored wheel (``--find-links``).
REM The extra remains OPTIONAL in pyproject.toml (cloud-only / Linux installs
REM that never run Setup.bat are not forced to pull it; the SDK provider is
REM import-guarded) -- Setup.bat is the Windows full-install entry point, so it
REM enables the extra explicitly here.
echo [INFO] Installing editable package ^(pip install -e .[cc-sdk]^)...
if exist "_constraints_tmp.txt" (
    uv pip install -e ".[cc-sdk]" --find-links "%WHL_DIR%" -c "_constraints_tmp.txt"
) else (
    uv pip install -e ".[cc-sdk]" --find-links "%WHL_DIR%"
)
if errorlevel 1 goto :install_error

REM --- Step 4b-extra: Install dev + e2e extras (CONTRIBUTORS only, --dev) ---
REM
REM dev = pytest / mypy / ruff / pre-commit / import-linter
REM e2e = playwright / pytest-playwright
REM
REM These are development / test-suite dependencies. A normal end-user install
REM does NOT need them, so they are gated behind ``Setup.bat --dev`` to avoid
REM forcing every user to download the test toolchain (and, in Step 4d, the
REM ~150MB Chromium browser). Default: skipped.
REM
REM ``grimp 3.14`` ARM64 wheel is bundled in vendor\whl\ (locally compiled
REM 2026-05-30); ``import-linter`` is therefore unpinned and follows the
REM upper bound declared in pyproject.toml.
if not "%DEV_EXTRAS%"=="1" (
    echo [SKIP] dev + e2e extras not requested ^(pass Setup.bat --dev for the test toolchain^).
    del /f /q "_constraints_tmp.txt" 2>nul
    goto :after_dev_extras
)
echo [INFO] Installing dev + e2e extras from pyproject.toml...
if exist "_constraints_tmp.txt" (
    uv pip install --find-links "%WHL_DIR%" -c "_constraints_tmp.txt" -e ".[dev,e2e,cc-sdk]"
) else (
    uv pip install --find-links "%WHL_DIR%" -e ".[dev,e2e,cc-sdk]"
)
if errorlevel 1 goto :install_error

del /f /q "_constraints_tmp.txt" 2>nul
:after_dev_extras

REM --- Step 4c: Ensure pip is available inside .venv ---
echo [INFO] Ensuring pip is available in .venv...
"%VENV_DIR%\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1
if errorlevel 1 (
    REM ensurepip failed (e.g. already present or not bundled) - try uv pip install pip
    uv pip install pip >nul 2>&1
)
REM Upgrade pip to latest to avoid "pip is too old" warnings
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>nul
echo [INFO] pip is ready in .venv.

REM --- Step 4d: Install Playwright Chromium browser (CONTRIBUTORS only, --dev) ---
REM The ``playwright`` Python package is only installed under --dev (Step
REM 4b-extra); this step downloads the ~150MB Chromium binary into the
REM user-scoped cache at %LOCALAPPDATA%\ms-playwright\chromium-*. It is used
REM ONLY by the e2e test suite, so it is gated behind --dev too. Idempotent.
if not "%DEV_EXTRAS%"=="1" (
    echo [SKIP] Playwright Chromium not requested ^(test-only; pass Setup.bat --dev^).
    goto :after_chromium
)
echo [INFO] Installing Playwright Chromium browser ^(skipped if already present^)...
"%VENV_DIR%\Scripts\python.exe" -m playwright install chromium 2>nul
if errorlevel 1 (
    echo [WARN] Playwright chromium install failed. e2e tests that need a
    echo [WARN] browser will skip; non-browser tests are unaffected.
)
:after_chromium

REM --- Step 4e: Aggregate + install App Builder Pack dependencies ---
REM
REM V1 parity (refactor docs/85-tasks/install-uninstall-v1-alignment-plan.md, D-1):
REM V1's Setup_Builder_Env.bat -> setup_qairt_env.py --install-inference-deps
REM auto-aggregated every Pack's requirements.txt into the ARM64 venv. V2
REM ships pre-built models, so the Pack-specific deps (openai-whisper /
REM scipy / jieba / pypinyin / cn2an / g2p_en / pillow / more-itertools /
REM tqdm / regex) must be installed here for whisper-base / melotts-zh /
REM ppocrv4 inference to work out of the box.
REM
REM Runs AFTER the editable install (Step 4b2) so the unified ``qai`` CLI
REM dispatcher (and its ``install-pack-deps`` subcommand) is on PATH inside
REM the venv. Non-fatal: individual package failures only warn; setup still
REM succeeds (matches V1 install_app_builder_deps behaviour).
echo.
echo [INFO] Installing App Builder Pack dependencies ^(factory\app_builder\models\*\requirements.txt^)...
"%VENV_DIR%\Scripts\python.exe" -m scripts.setup.install_app_builder_deps
if errorlevel 1 (
    echo [WARN] Some App Builder Pack deps failed to install. Voice / OCR / TTS
    echo [WARN] models may show ImportError on first run. Re-run later with:
    echo [WARN]     "%VENV_DIR%\Scripts\python.exe" -m scripts.setup.install_app_builder_deps
)

:skip_all_installs

REM --- Force-(re)install qai_appbuilder wheel every run -----------------------
REM The qai_appbuilder package is tightly coupled to the QAIRT SDK version and
REM must always match the pinned wheel in vendor\whl\. Because the general dep
REM skip-check above (import qai) cannot distinguish versions, we unconditionally
REM force-reinstall here so a version bump is never missed on re-run.
if exist "%WHL_QAI_APPBUILDER%" (
    echo [INFO] Force-installing %WHL_QAI_APPBUILDER% ...
    uv pip install --reinstall --no-deps "%WHL_QAI_APPBUILDER%"
    if errorlevel 1 (
        echo [WARN] Failed to force-install qai_appbuilder wheel.
    ) else (
        echo [OK]   qai_appbuilder wheel installed.
    )
) else (
    echo [WARN] qai_appbuilder wheel not found: %WHL_QAI_APPBUILDER%
)

REM --- Step 4f: Initialize the runtime data/ tree (install pipeline) ---
REM
REM AGENTS.md project-level rule (2026-06-19): a freshly-installed package
REM ships WITHOUT a data/ directory; Setup.bat is the user-facing install
REM entry point and MUST initialise data/ to "ready-to-use" state. The
REM install pipeline (scripts.init.install) is the project's purpose-built
REM mechanism for "blank -> complete data/" — idempotent, stage-based, with
REM dry-run/verify modes. Setup MUST drive it (do NOT hand-roll SQL or seed
REM logic here).
REM
REM Stages run (4 of 5 — compile_factory is skipped):
REM   data_dir         create data/db/qai.db + schema migrations + dirs
REM   seed_defaults    INSERT factory/db_staging/*.jsonl into kv_user_prefs +
REM                    model_catalog_entry (cloud-gateway provider, default
REM                    selected model, toolbar prefs, etc.)
REM   secret_bootstrap register SecretStore namespaces (empty placeholders)
REM   edition_secrets  internal-only: provision factory cloud-provider API
REM                    keys (e.g. cloud LLM service) into the SecretStore. No-op
REM                    on external editions (gated by Settings.is_internal).
REM
REM compile_factory is SKIPPED because user-machine release packages do NOT
REM ship factory/_source/; the pre-built factory/db_staging/*.jsonl seeds
REM are bundled directly. compile_factory would auto-skip on missing source
REM anyway, but the explicit --skip keeps the stage log free of confusing
REM "skipped: no source" notes.
REM
REM Position: AFTER ``:skip_all_installs`` so that a fast re-run (where the
REM dep closure check at Step 4 hits the [SKIP] branch and jumps straight
REM here) STILL runs the install pipeline. The pipeline itself is fully
REM idempotent — every stage short-circuits on already-applied state
REM (kv_user_prefs uses INSERT OR IGNORE; secret_bootstrap.exists() guards
REM the write so user-set credentials are NEVER clobbered back to "";
REM edition_secrets refuses to overwrite a non-empty user value) — so a
REM second pass costs only stat-level checks (~1-2s).
REM
REM Failure handling: a seed failure must NOT block Setup. By this point
REM venv + Python deps are fully installed; the user can still launch the
REM UI, manually populate provider configs, and re-run install later. We
REM log [WARN] (matching the PortableGit / TTS-predeploy patterns above)
REM and proceed to Step 5.
echo.
echo -- Step 4f: Initialize data/ tree ^(install pipeline^) -------------------------
"%VENV_DIR%\Scripts\python.exe" -m scripts.init.install --apply ^
    --factory-root "%ROOT_DIR%factory" ^
    --data-root "%ROOT_DIR%data" ^
    --sql-migrations "%ROOT_DIR%src\qai\platform\persistence\migrations_sql" ^
    --secret-backend auto ^
    --skip compile_factory
if errorlevel 1 (
    echo [WARN] data/ initialisation reported errors. Some factory defaults
    echo [WARN] may not be seeded. The UI is still usable; you can re-run with:
    echo [WARN]     "%VENV_DIR%\Scripts\python.exe" -m scripts.init.install --apply ^^
    echo [WARN]         --factory-root "%ROOT_DIR%factory" ^^
    echo [WARN]         --data-root "%ROOT_DIR%data" ^^
    echo [WARN]         --sql-migrations "%ROOT_DIR%src\qai\platform\persistence\migrations_sql" ^^
    echo [WARN]         --secret-backend auto --skip compile_factory
) else (
    echo [OK]   data/ initialised ^(qai.db + factory seeds + secret namespaces^).
)

REM ===========================================================================
REM  Step 5: PortableGit (ARM64)
REM ===========================================================================

echo.
echo -- Step 5: PortableGit -------------------------------------------------------

set "PORTABLE_GIT_DIR=%LOCALAPPDATA%\QAIModelBuilder\git"
set "GIT_VERSION=2.54.0"
set "GIT_URL=https://github.com/git-for-windows/git/releases/download/v%GIT_VERSION%.windows.1/PortableGit-%GIT_VERSION%-arm64.7z.exe"

REM === Step 5a: Ensure 7zr.exe is available in data\bin\7zr\ ====================
REM 7zr.exe is a small standalone tool (~600 KB) from 7-zip.org that handles .7z
REM archives natively. It is used here (PortableGit extraction), in Step 0b
REM (vendor-deps.7z), and in Step 8 (:ensure_7za bootstrap). We check for it
REM BEFORE the PortableGit [SKIP] guard so that a re-run after data\bin\ was
REM cleaned always re-downloads it, regardless of whether PortableGit itself
REM needs reinstalling.
set "SEVEN_ZIP=data\bin\7zr\7zr.exe"

if not exist "%SEVEN_ZIP%" (
    echo [INFO] Downloading standalone 7zr.exe ^(~600KB^) to data\bin\7zr\ ...
    if not exist "data\bin\7zr" mkdir "data\bin\7zr"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
        -Url "https://www.7-zip.org/a/7zr.exe" ^
        -OutFile "data\bin\7zr\7zr.exe" ^
        -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
        -MinSize 100000 ^
        -MaxRetries 3 ^
        -StallTimeoutSec 30 ^
        -AttemptTimeoutSec 60
    if exist "%SEVEN_ZIP%" (
        echo [OK]   7zr.exe downloaded: %SEVEN_ZIP%
    ) else (
        echo [WARN] Failed to download 7zr.exe from 7-zip.org.
        set "SEVEN_ZIP="
    )
) else (
    echo [SKIP] 7zr.exe already present: %SEVEN_ZIP%
)

REM Skip PortableGit install if already present (PortableGit 2.50+ uses cmd\git.exe; older uses bin\git.exe)
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" (
    echo [SKIP] PortableGit already installed: %PORTABLE_GIT_DIR%
    goto :git_done
)
if exist "%PORTABLE_GIT_DIR%\bin\git.exe" (
    echo [SKIP] PortableGit already installed: %PORTABLE_GIT_DIR%
    goto :git_done
)

set "GIT_ARCHIVE=%DL_DIR%\PortableGit-%GIT_VERSION%-arm64.7z.exe"
if not exist "%DL_DIR%" mkdir "%DL_DIR%"

REM === Download via the unified wrapper ============================================
REM download_with_aria2c.ps1 handles:
REM   * idempotency (skip if archive already present and >= MinSize)
REM   * truncated-archive detection via -MinSize 57671680 (55 MB threshold for the
REM     ~57 MB PortableGit 2.54 ARM64 7z.exe; replaces the two hand-rolled
REM     "for %%S in ... %%~zS LSS 57671680" guards)
REM   * stall watchdog + retry (the failure mode that froze Setup.bat for users)
REM   * aria2c primary, PowerShell fallback when aria2c.exe is missing
echo [INFO] Downloading PortableGit %GIT_VERSION% ^(ARM64^)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%GIT_URL%" ^
    -OutFile "%GIT_ARCHIVE%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 57671680 ^
    -MaxRetries 5 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 600 ^
    -Connections 8

if not exist "%GIT_ARCHIVE%" (
    echo [WARN] Failed to download PortableGit. Git tools will not be available.
    goto :git_done
)

:git_extract
echo [INFO] Extracting PortableGit to %PORTABLE_GIT_DIR% ...
if not exist "%PORTABLE_GIT_DIR%" mkdir "%PORTABLE_GIT_DIR%"

REM Extraction strategy:
REM   PRIMARY: Use our bundled 7zr.exe (downloaded above in Step 5a).
REM            This is the most reliable method - works on all Windows versions,
REM            no GUI needed, no system 7-Zip required, exit codes are accurate.
REM   FALLBACK 1: Self-extractor (PortableGit-*.7z.exe -y -o<dir>) - works only
REM               in some interactive environments; can fail silently with
REM               errorlevel=1 when the GUI subsystem is unavailable.
REM   FALLBACK 2: PowerShell using Expand-7Zip module (if available).
REM
REM Reference: launcher's Install_Tools.py uses pixi-managed 7zip the same way.
REM
REM Success is detected by either:
REM   - <PORTABLE_GIT_DIR>\cmd\git.exe (preferred location, used by PortableGit 2.50+)
REM   - <PORTABLE_GIT_DIR>\bin\git.exe (legacy location)

REM === Attempt 1: Use our bundled 7zr.exe (primary method) ===
REM Note: We do NOT wrap the SFX call in an `if (...)` block, because cmd.exe
REM expands %errorlevel% at block-entry time (giving stale 0), not after the
REM call returns. Using a goto-around pattern preserves the real exit code.
if not defined SEVEN_ZIP goto :try_sfx_attempt
echo [INFO] Attempt 1/3: Using bundled 7zr.exe: %SEVEN_ZIP%
echo [INFO] Running: "%SEVEN_ZIP%" x -y -o"%PORTABLE_GIT_DIR%" "%GIT_ARCHIVE%"
"%SEVEN_ZIP%" x -y "-o%PORTABLE_GIT_DIR%" "%GIT_ARCHIVE%" -bso0 -bsp1
echo [INFO] 7zr.exe exit code: %errorlevel%
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" goto :git_extract_done
if exist "%PORTABLE_GIT_DIR%\bin\git.exe" goto :git_extract_done
echo [WARN] 7zr.exe extraction did not produce git.exe.

:try_sfx_attempt

REM === Attempt 2: Self-extractor (fallback if 7zr.exe download failed) ===
echo [INFO] Attempt 2/3: Self-extractor ^(a small progress dialog may appear^)...
echo [INFO] Running: "%GIT_ARCHIVE%" -y -o"%PORTABLE_GIT_DIR%"
"%GIT_ARCHIVE%" -y -o"%PORTABLE_GIT_DIR%"
echo [INFO] SFX exit code: %errorlevel%
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" goto :git_extract_done
if exist "%PORTABLE_GIT_DIR%\bin\git.exe" goto :git_extract_done
echo [WARN] Self-extractor did not produce git.exe.

REM === Attempt 3: PowerShell using Expand-7Zip module (if available) ===
echo [INFO] Attempt 3/3: PowerShell fallback...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$arc = '%GIT_ARCHIVE%'; $dst = '%PORTABLE_GIT_DIR%'; " ^
    "if (Get-Module -ListAvailable -Name 7Zip4PowerShell) { " ^
    "  Import-Module 7Zip4PowerShell; " ^
    "  Expand-7Zip -ArchiveFileName $arc -TargetPath $dst -Verbose " ^
    "} else { " ^
    "  Write-Host '[WARN] 7Zip4PowerShell module not installed, cannot extract.'; " ^
    "  exit 1 " ^
    "}"

:git_extract_done

REM Success check: PortableGit 2.50+ puts git.exe at cmd\git.exe; older versions used bin\git.exe.
set "GIT_EXE="
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" set "GIT_EXE=%PORTABLE_GIT_DIR%\cmd\git.exe"
if not defined GIT_EXE if exist "%PORTABLE_GIT_DIR%\bin\git.exe" set "GIT_EXE=%PORTABLE_GIT_DIR%\bin\git.exe"

if defined GIT_EXE (
    echo [OK]   PortableGit installed: %PORTABLE_GIT_DIR%
    "%GIT_EXE%" --version
    REM Extraction succeeded -- delete the downloaded self-extractor archive.
    del /f /q "%GIT_ARCHIVE%" 2>nul
) else (
    echo [WARN] PortableGit extraction may have failed. Check %PORTABLE_GIT_DIR%
    echo [INFO] To install manually, open a cmd prompt and run one of:
    echo [INFO]   "%GIT_ARCHIVE%" -y -o"%PORTABLE_GIT_DIR%"
    echo [INFO]   data\bin\7zr\7zr.exe x -y "-o%PORTABLE_GIT_DIR%" "%GIT_ARCHIVE%"
)

:git_done

REM ===========================================================================
REM  Step 5b: Node.js (ARM64 portable) + pnpm  -- frontend build toolchain
REM ===========================================================================
REM Build.bat compiles the Vue/Vite WebUI with pnpm, which needs Node.js. We
REM install a PORTABLE ARM64 Node into %LOCALAPPDATA%\QAIModelBuilder\node
REM (same pattern as PortableGit: no system install, no admin; removed by
REM Uninstall.bat together with the rest of %LOCALAPPDATA%\QAIModelBuilder).
REM pnpm is enabled via Node's bundled corepack. Idempotent: skips when
REM node.exe + a pnpm shim are already present.
echo.
echo -- Step 5b: Node.js ^(ARM64^) + pnpm -------------------------------------------

set "NODE_VERSION=22.20.0"
set "NODE_DIR=%LOCALAPPDATA%\QAIModelBuilder\node"
set "NODE_EXE=%NODE_DIR%\node.exe"
set "NODE_URL=https://nodejs.org/dist/v%NODE_VERSION%/node-v%NODE_VERSION%-win-arm64.zip"
set "NODE_ZIP=%DL_DIR%\node-v%NODE_VERSION%-win-arm64.zip"

REM Skip if node.exe already present and a pnpm shim exists.
if not exist "%NODE_EXE%" goto :node_install
if exist "%NODE_DIR%\pnpm.cmd" goto :node_have
if exist "%NODE_DIR%\pnpm" goto :node_have
REM node present but pnpm shim missing -> (re)enable pnpm below.
set "PATH=%NODE_DIR%;%PATH%"
echo [INFO] Node.js present; enabling pnpm via corepack...
goto :node_enable_pnpm

:node_have
echo [SKIP] Node.js + pnpm already installed: %NODE_DIR%
set "PATH=%NODE_DIR%;%PATH%"
goto :node_done

:node_install
echo [INFO] Downloading Node.js %NODE_VERSION% ^(ARM64^)...
echo [INFO] URL: %NODE_URL%
REM Modest -Connections 4 because some CDNs / corporate proxies do not
REM honour HTTP Range and reply with the whole body per chunk; -ZipTest
REM ensures a Range-broken stitch never reaches extraction (it gets
REM caught by download_with_aria2c.ps1 and triggers a retry).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%NODE_URL%" ^
    -OutFile "%NODE_ZIP%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 20000000 ^
    -ZipTest ^
    -MaxRetries 4 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 600 ^
    -Connections 4
if not exist "%NODE_ZIP%" goto :node_dl_failed
goto :node_extract

:node_dl_failed
echo [WARN] Failed to download Node.js. Frontend build will be unavailable.
echo [WARN] Re-run Setup.bat later, or install Node.js ARM64 manually.
goto :node_done

:node_extract
echo [INFO] Extracting Node.js to %NODE_DIR% ...
if exist "%NODE_DIR%" rmdir /s /q "%NODE_DIR%" 2>nul
set "NODE_TMP=%DL_DIR%\_node_tmp"
if exist "%NODE_TMP%" rmdir /s /q "%NODE_TMP%" 2>nul
REM The zip nests everything under node-v<VER>-win-arm64\ ; flatten that one
REM level so node.exe lands directly in %NODE_DIR%.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; " ^
    "Expand-Archive -Path '%NODE_ZIP%' -DestinationPath '%NODE_TMP%' -Force; " ^
    "$inner = Get-ChildItem -LiteralPath '%NODE_TMP%' -Directory | Select-Object -First 1; " ^
    "if ($inner) { Move-Item -LiteralPath $inner.FullName -Destination '%NODE_DIR%' -Force } " ^
    "else { Move-Item -LiteralPath '%NODE_TMP%' -Destination '%NODE_DIR%' -Force }; " ^
    "if (Test-Path '%NODE_TMP%') { Remove-Item -LiteralPath '%NODE_TMP%' -Recurse -Force -ErrorAction SilentlyContinue }"
if not exist "%NODE_EXE%" goto :node_extract_failed
del /f /q "%NODE_ZIP%" 2>nul
set "PATH=%NODE_DIR%;%PATH%"
echo [OK]   Node.js installed: %NODE_DIR%
"%NODE_EXE%" --version
goto :node_enable_pnpm

:node_extract_failed
echo [WARN] Node.js extraction failed; node.exe not found at %NODE_DIR%.
echo [WARN] Frontend build will be unavailable until Node is installed.
goto :node_done

:node_enable_pnpm
REM Enable pnpm via Node's bundled corepack. corepack writes pnpm shims into
REM %NODE_DIR%; the first `pnpm install` later fetches the pinned pnpm version.
REM Non-fatal: a failure only warns.
REM
REM COREPACK_ENABLE_DOWNLOAD_PROMPT=0 disables the interactive
REM "Do you want to continue? [Y/n]" prompt corepack shows the FIRST time it
REM has to download a package manager tarball from npmjs. Without this, the
REM very first `pnpm install` from Build.bat (or any pnpm invocation) hangs
REM waiting for user input. We also pre-warm the download here by invoking
REM `pnpm --version`, so Build.bat finds pnpm fully ready (no first-run
REM download hop on the developer iteration loop).
echo [INFO] Enabling pnpm via corepack...
set "COREPACK_ENABLE_DOWNLOAD_PROMPT=0"
call "%NODE_DIR%\corepack.cmd" enable >nul 2>&1
REM Pin the exact pnpm version instead of @latest so every machine that runs
REM Setup.bat gets the SAME pnpm, reproducibly, regardless of when/where it runs
REM (@latest resolves differently over time / behind proxies). This MUST match
REM the "packageManager" field in frontend/package.json (corepack honours that
REM field first when pnpm runs inside frontend/, so keeping both in sync avoids
REM a redundant second download). pnpm 11.9.0 is the version that ships with the
REM corepack bundled in Node %NODE_VERSION% (LTS) -- Node/pnpm stay matched.
set "PNPM_VERSION=11.9.0"
call "%NODE_DIR%\corepack.cmd" prepare pnpm@%PNPM_VERSION% --activate >nul 2>&1
REM Pre-warm: forces corepack to actually download the pnpm tarball now.
call "%NODE_DIR%\pnpm.cmd" --version >nul 2>&1
if exist "%NODE_DIR%\pnpm.cmd" goto :node_pnpm_ok
where pnpm >nul 2>&1
if not errorlevel 1 goto :node_pnpm_ok
echo [WARN] corepack could not enable pnpm. Build.bat may fail until pnpm is
echo [WARN] available. Try: "%NODE_DIR%\corepack.cmd" enable pnpm
goto :node_done
:node_pnpm_ok
echo [OK]   pnpm enabled via corepack.

:node_done

REM ===========================================================================
REM  Step 5c: Rust toolchain + tauri-cli  -- MOVED to after Step 8
REM ===========================================================================
REM The desktop build toolchain (Rust + tauri-cli) used to run here, but
REM `cargo install tauri-cli` compiles native C crates (e.g. `ring`) that
REM REQUIRE the MSVC C++ toolchain + Windows SDK headers (assert.h, etc.).
REM Those are installed by Step 8.3 (scripts\setup\install_vs.ps1). Running
REM cargo BEFORE Step 8 meant the headers were absent -> "fatal error:
REM 'assert.h' file not found" and the whole tauri-cli build failed.
REM
REM Fix: the desktop toolchain now runs as subroutine :setup_desktop_toolchain
REM AFTER Step 8 completes (see the call near :builder_done), so VS C++ tools
REM exist and we can load their environment (vcvars) before invoking cargo.
REM Nothing to do here anymore.

REM ===========================================================================
REM  Step 6: Pre-deploy TTS runtime data (NLTK / jieba / g2p_en)
REM ===========================================================================
REM
REM Why: features/app-builder/models/melotts-zh/runner.py needs NLTK corpora
REM (averaged_perceptron_tagger* + cmudict) at inference time. Downloading
REM them lazily on first inference would make the user wait ~5 s on a fast
REM network, and would *fail entirely* on offline machines. So we cache the
REM data into vendor\nltk_data\ here, once, while the network is available.
REM
REM Idempotent: re-runs are cheap. NLTK skips already-downloaded packages.
REM Non-fatal: a failure here just means slower (or offline-broken) first
REM inference; setup as a whole is still considered successful.
REM
echo.
echo -- Step 6: Pre-deploy TTS runtime data ---------------------------------------
if not exist "scripts\setup\predeploy_tts_runtime.py" (
    echo [SKIP] scripts\setup\predeploy_tts_runtime.py not found, skipping TTS pre-deploy.
    goto :predeploy_done
)
REM Suppress noisy SyntaxWarning spam from third-party packages (jieba /
REM g2p_en use invalid escape sequences like "\." in their regex string
REM literals, which Python 3.13 flags at import time). These warnings come
REM from inside those libraries -- not our code -- and are harmless, so we
REM silence only SyntaxWarning via -W (other warnings still surface).
REM Passing -W on the command line keeps "if errorlevel 1" below valid
REM (no intervening "set" that would reset the exit code).
"%VENV_DIR%\Scripts\python.exe" -W ignore::SyntaxWarning "scripts\setup\predeploy_tts_runtime.py"
if errorlevel 1 (
    echo [WARN] TTS runtime pre-deploy reported issues. First TTS inference may be slow
    echo [WARN] or fail offline. You can re-run later with:
    echo [WARN]     "%VENV_DIR%\Scripts\python.exe" scripts\setup\predeploy_tts_runtime.py
) else (
    echo [OK]   TTS runtime data ready at vendor\nltk_data\
)
:predeploy_done

REM ===========================================================================
REM  Step 7: Verify installation (import key modules)
REM ===========================================================================
echo.
echo -- Step 7: Verify installation --------------------------------------------------
"%VENV_DIR%\Scripts\python.exe" -c "import fastapi; import uvicorn; import pydantic; import qai; print('[OK]   Core imports verified: fastapi, uvicorn, pydantic, qai')"
if errorlevel 1 (
    echo [WARN] Some core modules failed to import. The environment may be incomplete.
    echo [WARN] Try re-running Setup.bat or check errors above.
) else (
    echo [OK]   All core modules import successfully.
)

REM ===========================================================================
REM  Step 7a: Wire the version-controlled git pre-commit hook (zero user action)
REM ===========================================================================
REM .git/hooks is NOT distributed by `git clone`, so we ship the hook under the
REM version-controlled .githooks/ directory and point git at it via
REM core.hooksPath. Doing it here means anyone who runs Setup.bat gets the
REM read-only secret-scan gating automatically -- they never have to run
REM `pre-commit install` or configure anything by hand. Idempotent + non-fatal:
REM only runs inside a git work tree, and a failure just warns.
echo.
echo -- Step 7a: Configure git pre-commit hook ------------------------------------
if exist "%ROOT_DIR%.githooks\pre-commit" (
    git -C "%ROOT_DIR%." rev-parse --is-inside-work-tree >nul 2>&1
    if not errorlevel 1 (
        git -C "%ROOT_DIR%." config core.hooksPath .githooks >nul 2>&1
        if not errorlevel 1 (
            echo [OK]   git core.hooksPath -^> .githooks ^(read-only secret-scan gating active^).
        ) else (
            echo [WARN] Could not set core.hooksPath. Commits will not be auto-gated locally.
        )
    ) else (
        echo [SKIP] Not a git work tree; skipping hook configuration.
    )
) else (
    echo [SKIP] .githooks\pre-commit not found; skipping hook configuration.
)

REM ===========================================================================
REM  Step 7b: Pre-download voice / TTS model weights
REM ===========================================================================
REM
REM Best-effort download of the three on-device voice/TTS QNN context-binary
REM archives (whisper_medium / zipformer / melotts_zh, all v0.55.0) from
REM qai-hub public assets, picking the device-specific zip (x_elite vs
REM x2_elite) via PowerShell CPU detection. Each archive is fetched with
REM aria2c (multi-thread + resume) when available, or PowerShell as fallback,
REM then extracted with our bundled 7zr.exe.
REM
REM Hard-constraint (2): missing weights NEVER crash the app. The runner-side
REM auto-download (factory\app_builder\shared\weight_downloader.py) covers the
REM "first inference on a fresh machine" case at runtime; this step only
REM warms the cache so the first inference is fast. Any failure here is
REM logged as [WARN] and Setup proceeds (overall exit 0).
REM
REM Skip with: Setup.bat --no-builder ALSO skips this step? No ??? voice/TTS
REM models are needed for the base runtime (inference), not the model-builder
REM environment. So this runs regardless of --no-builder.
REM
echo.
echo -- Step 7b: Pre-download voice / TTS model weights --------------------------
call :predownload_voice_models
:predownload_voice_models_done

REM ===========================================================================
REM  Step 8: QAIRT model-builder environment (merged from V1 Setup_Builder_Env.bat)
REM ===========================================================================
REM Installs everything model conversion needs, with NO dependency on qai.exe:
REM   8.1 x86_64 Python 3.10 venv (.venv_x64_310) for the QNN converter
REM   8.2 QAIRT SDK (~2GB, aria2c/PowerShell download + extract)
REM   8.3 VS 2022 C++ ARM64 build tools (scripts\setup\install_vs.ps1)
REM   8.4 QAIRT dependency check (informational)
REM   8.5 x64 converter deps + ARM64 inference deps (setup_qairt_env.py)
REM   8.6 generate data\config\qairt_env.json (setup_qairt_env.py --gen-config)
REM   8.7 verify (setup_qairt_env.py --verify)
REM Skip with: Setup.bat --no-builder
if "%NO_BUILDER%"=="1" (
    echo.
    echo -- Step 8: QAIRT model-builder env -- [SKIP] --no-builder specified ----------
    echo [INFO] Base runtime is ready. To enable model conversion later, re-run
    echo [INFO] Setup.bat without --no-builder.
    goto :builder_done
)
call :setup_builder_env
if errorlevel 1 (
    echo.
    echo [ERROR] Step 8 ^(QAIRT model-builder environment^) aborted. Setup will not continue.
    exit /b 1
)
:builder_done

REM ===========================================================================
REM  Step 9: Desktop build toolchain (Rust + tauri-cli)  -- opt-in (--desktop)
REM ===========================================================================
REM Runs AFTER Step 8 on purpose: `cargo install tauri-cli` compiles native C
REM crates (ring, etc.) that need the MSVC C++ toolchain + Windows SDK headers
REM installed by Step 8.3 (install_vs.ps1). We also load the VS build
REM environment (vcvars) inside the subroutine before invoking cargo, so
REM cl.exe / the CRT + SDK INCLUDE/LIB paths are visible.
call :setup_desktop_toolchain

REM ===========================================================================
REM  Step 10: WebView2 Fixed Version Runtime (always runs, no flag needed)
REM ===========================================================================
REM The desktop exe needs WebView2 to render its UI. This step runs for ALL
REM users (not just --desktop) because running the desktop exe is a normal
REM user action that should work out-of-the-box without extra flags.
REM Rust/tauri-cli (Step 9) is developer-only (--desktop); WebView2 is user-facing.
call :install_webview2

echo.
echo  ================================================================
echo.
echo      *****   SETUP COMPLETED SUCCESSFULLY!   *****
echo.
echo      QAI ModelBuilder is fully installed and ready to use.
echo.
echo  ================================================================
echo.
echo  +--------------------------------------------------+
echo  ^|   Next steps:                                    ^|
echo  ^|                                                  ^|
echo  ^|     Start server:  Start.bat                     ^|
echo  ^|     Run a CLI cmd: qai.bat ^<args^>                 ^|
echo  ^|     Open console:  Console.bat                   ^|
echo  +--------------------------------------------------+
echo.
echo [INFO] The "qai" command-line tool is now installed in this environment.
echo [INFO] Quick ways to use it:
echo [INFO]     qai.bat --help                 ^(one-shot, no shell needed^)
echo [INFO]     qai.bat config provider list   ^(configure cloud model providers^)
echo [INFO]     qai.bat app ^<pack^> --image x.png   ^(run an App Builder model^)
echo [INFO]     qai.bat build                  ^(Model Builder conversion session^)
echo [INFO]
echo [INFO] To install extra Python packages or run ad-hoc Python commands,
echo [INFO] use Console.bat ^(drops you into the activated venv^).
echo [INFO] ^(Power-user fallback if you prefer manual activation:
echo [INFO]      call "%VENV_DIR%\Scripts\activate.bat"  ^&^&  qai --help^)
echo.
echo [INFO] Model conversion ^(model-builder^) environment was set up in Step 8
echo [INFO] ^(QAIRT SDK + x64 converter venv + VS build tools + qairt_env.json^).
echo.
REM Only pause when run directly (not when --no-pause is passed for scripted runs)
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 0

:fetch_guard_dlls
echo.
echo -- Step 0c: Native FileGuard DLLs ^(guard.zip^) ------------------------------

set "GUARD_URL=https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/guard.zip"
set "GUARD_ZIP=%DL_DIR%\guard.zip"
set "GUARD_TMP=%DL_DIR%\_guard_tmp"

REM Idempotency: release/dev checkout may already contain the DLLs.
if exist "vendor\bin\arm64\guard64.dll" if exist "vendor\bin\x64\guard64.dll" (
    echo [SKIP] guard64.dll already present: vendor\bin\arm64 + vendor\bin\x64
    exit /b 0
)

if not exist "vendor\bin\arm64" mkdir "vendor\bin\arm64"
if not exist "vendor\bin\x64" mkdir "vendor\bin\x64"
if not exist "%DL_DIR%" mkdir "%DL_DIR%"

REM Reuse an existing archive if it is already present.
if exist "%GUARD_ZIP%" (
    echo [SKIP] guard.zip already downloaded: %GUARD_ZIP%
    goto :guard_extract
)

echo [INFO] Downloading native FileGuard DLLs...
echo [INFO] URL: %GUARD_URL%
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%GUARD_URL%" ^
    -OutFile "%GUARD_ZIP%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 500000 ^
    -ZipTest ^
    -MaxRetries 5 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 300

if not exist "%GUARD_ZIP%" (
    echo [WARN] Failed to download guard.zip. Native FileGuard will be unavailable.
    echo [WARN] Re-run Setup.bat later, or manually download from:
    echo [WARN]     %GUARD_URL%
    echo [WARN] and extract guard64.dll into vendor\bin\arm64 and vendor\bin\x64.
    exit /b 0
)

:guard_extract
echo [INFO] Extracting guard.zip...
if exist "%GUARD_TMP%" rmdir /s /q "%GUARD_TMP%" 2>nul
mkdir "%GUARD_TMP%"
REM guard.zip layout (verified): arm64\guard64.dll + x64\guard64.dll
REM Expand directly into GUARD_TMP, then copy each DLL to its vendor\bin\ slot.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; " ^
    "Expand-Archive -Path '%GUARD_ZIP%' -DestinationPath '%GUARD_TMP%' -Force; " ^
    "$arm = Join-Path '%GUARD_TMP%' 'arm64\guard64.dll'; " ^
    "$x64 = Join-Path '%GUARD_TMP%' 'x64\guard64.dll'; " ^
    "if (Test-Path $arm) { Copy-Item -LiteralPath $arm -Destination 'vendor\bin\arm64\guard64.dll' -Force; Write-Host '[INFO] arm64\guard64.dll copied.' } else { Write-Host '[WARN] arm64\guard64.dll not found in archive.' }; " ^
    "if (Test-Path $x64) { Copy-Item -LiteralPath $x64 -Destination 'vendor\bin\x64\guard64.dll' -Force; Write-Host '[INFO] x64\guard64.dll copied.' } else { Write-Host '[WARN] x64\guard64.dll not found in archive.' }"

if exist "%GUARD_TMP%" rmdir /s /q "%GUARD_TMP%" 2>nul

if exist "vendor\bin\arm64\guard64.dll" if exist "vendor\bin\x64\guard64.dll" (
    echo [OK]   guard64.dll installed to vendor\bin\arm64 and vendor\bin\x64.
    del /f /q "%GUARD_ZIP%" 2>nul
    exit /b 0
)

echo [WARN] guard.zip extracted but both guard DLLs were not found.
echo [WARN] Check archive layout at %GUARD_ZIP%.
exit /b 0


REM ===========================================================================
REM  Subroutine: pre-download voice / TTS model weights (Step 7b)
REM ===========================================================================
REM Best-effort: each model failure logs [WARN] and proceeds; the runner-side
REM auto-download covers the rest at first inference. Never aborts Setup.
REM Respects HTTPS_PROXY / ALL_PROXY when set in the environment (aria2c
REM ``--all-proxy``; PowerShell ``Invoke-WebRequest`` honours the variables
REM natively). Source of truth for the proxy URL is the runtime
REM ToolsSettings.global_proxy (file-backed config) read by the apps/api
REM wiring root and re-exported as env vars for the server's runner spawns;
REM Setup.bat trusts whatever the OS env carries - installer-time proxy is
REM the user's responsibility.
:predownload_voice_models
REM --- Detect SoC family (snapdragon_x_elite vs snapdragon_x2_elite) ---
set "VOICE_PLATFORM=snapdragon_x_elite"
for /f "usebackq tokens=*" %%P in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$n=''; try{$n=((Get-CimInstance Win32_Processor | Select-Object -First 1).Name).ToLower()}catch{}; if(-not $n){$n=$env:PROCESSOR_IDENTIFIER.ToLower()}; if($n -match 'family 8 model 2' -or $n -match ' x2 ' -or $n -match 'x2e'){'snapdragon_x2_elite'}else{'snapdragon_x_elite'}"`) do set "VOICE_PLATFORM=%%P"
echo [INFO] Detected voice/TTS platform: %VOICE_PLATFORM%

REM --- Per-model download/extract ---
set "VOICE_S3=https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models"

set "VOICE_MODEL_DIR=models\whisper-base"
set "VOICE_ARCHIVE=whisper_medium-qnn_context_binary-float-qualcomm_%VOICE_PLATFORM%.zip"
set "VOICE_URL=%VOICE_S3%/whisper_medium/releases/v0.55.0/%VOICE_ARCHIVE%"
call :predownload_one_model "whisper_medium" "%VOICE_MODEL_DIR%" "%VOICE_ARCHIVE%" "%VOICE_URL%" "encoder.bin" "decoder.bin"

set "VOICE_MODEL_DIR=models\zipformer-zh"
set "VOICE_ARCHIVE=zipformer-qnn_context_binary-float-qualcomm_%VOICE_PLATFORM%.zip"
set "VOICE_URL=%VOICE_S3%/zipformer/releases/v0.55.0/%VOICE_ARCHIVE%"
call :predownload_one_model "zipformer" "%VOICE_MODEL_DIR%" "%VOICE_ARCHIVE%" "%VOICE_URL%" "encoder.bin" "decoder.bin" "joiner.bin"

set "VOICE_MODEL_DIR=models\melotts-zh"
set "VOICE_ARCHIVE=melotts_zh-voice_ai-mixed_with_float-qualcomm_%VOICE_PLATFORM%.zip"
set "VOICE_URL=%VOICE_S3%/melotts_zh/releases/v0.55.0/%VOICE_ARCHIVE%"
call :predownload_one_model "melotts_zh" "%VOICE_MODEL_DIR%" "%VOICE_ARCHIVE%" "%VOICE_URL%" "encoder.bin" "flow.bin" "decoder.bin" "bert_wrapper.bin"

REM Return from the called :predownload_voice_models subroutine.
goto :eof

REM ---------------------------------------------------------------------------
REM  Subroutine: download + extract one voice/TTS model.
REM  Args: %1=tag %2=target dir %3=archive name %4=url %5..%n=required bin files
REM  Behaviour: idempotent (skip when all required files present); aria2c
REM  primary (with --all-proxy when HTTPS_PROXY is set), PowerShell fallback;
REM  Expand-Archive to a temp dir, then move bin files into target dir. Any
REM  failure logs [WARN] and returns ??? never aborts.
REM ---------------------------------------------------------------------------
:predownload_one_model
setlocal EnableDelayedExpansion
set "VM_TAG=%~1"
set "VM_DIR=%~2"
set "VM_ARC=%~3"
set "VM_URL=%~4"
shift & shift & shift & shift
set "VM_REQ="
:vm_collect
if "%~1"=="" goto :vm_after_collect
set "VM_REQ=!VM_REQ! %~1"
shift
goto :vm_collect
:vm_after_collect

REM Idempotent check: skip when all required files already present.
set "VM_NEED=0"
for %%F in (!VM_REQ!) do (
    if not exist "!VM_DIR!\%%F" set "VM_NEED=1"
)
if "!VM_NEED!"=="0" (
    echo [SKIP] %VM_TAG%: all weight files already present at !VM_DIR!
    endlocal & goto :eof
)

if not exist "!VM_DIR!" mkdir "!VM_DIR!" 2>nul
set "VM_ARCHIVE_PATH=!VM_DIR!\!VM_ARC!"
set "VM_TMP=!VM_DIR!\_extract_tmp"

REM --- Optional proxy URL (env-based; Setup.bat trusts OS env) ---
set "VM_PROXY_URL="
if defined HTTPS_PROXY set "VM_PROXY_URL=!HTTPS_PROXY!"
if not defined HTTPS_PROXY if defined ALL_PROXY set "VM_PROXY_URL=!ALL_PROXY!"
if not defined HTTPS_PROXY if not defined ALL_PROXY if defined HTTP_PROXY set "VM_PROXY_URL=!HTTP_PROXY!"

REM --- Download via the unified wrapper ---
if exist "!VM_ARCHIVE_PATH!" goto :vm_after_dl
echo [INFO] %VM_TAG%: downloading !VM_ARC! ...
if defined VM_PROXY_URL (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
        -Url "!VM_URL!" -OutFile "!VM_ARCHIVE_PATH!" ^
        -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
        -ProxyUrl "!VM_PROXY_URL!" ^
        -ZipTest -MaxRetries 4 -StallTimeoutSec 60 -AttemptTimeoutSec 600
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
        -Url "!VM_URL!" -OutFile "!VM_ARCHIVE_PATH!" ^
        -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
        -ZipTest -MaxRetries 4 -StallTimeoutSec 60 -AttemptTimeoutSec 600
)
if errorlevel 1 (
    echo [WARN] %VM_TAG%: download failed; runtime auto-download will retry on first inference.
    del /f /q "!VM_ARCHIVE_PATH!" 2>nul
    endlocal & goto :eof
)
:vm_after_dl
if not exist "!VM_ARCHIVE_PATH!" (
    echo [WARN] %VM_TAG%: archive missing after download attempt.
    endlocal & goto :eof
)

REM --- Extract via Expand-Archive (zip) -----------------------------------
REM NOTE: Cannot use the bundled data\bin\7zr\7zr.exe here -- 7zr is the
REM "7z-format-only" reduced build (7zr = "7z reduced") and rejects .zip
REM input with "Cannot open the file as archive" (exit 2). For .zip, use
REM either Expand-Archive (this path) or 7za.exe (the full 7-Zip console
REM build, used for the much larger QAIRT SDK zip in Step 8.2 -- see
REM :qairt_try_7za + :ensure_7za). The voice/TTS zips are only a few
REM hundred MB each, so Expand-Archive is acceptable here.
if exist "!VM_TMP!" rd /s /q "!VM_TMP!" 2>nul
mkdir "!VM_TMP!" 2>nul
echo [INFO] %VM_TAG%: extracting...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Expand-Archive -Path '!VM_ARCHIVE_PATH!' -DestinationPath '!VM_TMP!' -Force } catch { Write-Host '[WARN] extract failed:' $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo [WARN] %VM_TAG%: extraction failed; runtime auto-download will retry on first inference.
    rd /s /q "!VM_TMP!" 2>nul
    del /f /q "!VM_ARCHIVE_PATH!" 2>nul
    endlocal & goto :eof
)

REM --- Copy required + optional files into target dir ---
REM Search for each required file anywhere under !VM_TMP! (the zip nests
REM under a device-specific subdir; some zips put files at root ??? handle both).
for %%F in (!VM_REQ!) do (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$src = Get-ChildItem -Path '!VM_TMP!' -Recurse -Filter '%%F' -File | Select-Object -First 1; if ($src) { Copy-Item -Path $src.FullName -Destination '!VM_DIR!\%%F' -Force } else { exit 1 }"
)
REM Optional metadata.json (best-effort)
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$src = Get-ChildItem -Path '!VM_TMP!' -Recurse -Filter 'metadata.json' -File | Select-Object -First 1; if ($src) { Copy-Item -Path $src.FullName -Destination '!VM_DIR!\metadata.json' -Force }"

REM Cleanup
rd /s /q "!VM_TMP!" 2>nul
del /f /q "!VM_ARCHIVE_PATH!" 2>nul

REM Verify
set "VM_OK=1"
for %%F in (!VM_REQ!) do (
    if not exist "!VM_DIR!\%%F" set "VM_OK=0"
)
if "!VM_OK!"=="1" (
    echo [OK]   %VM_TAG%: weights ready at !VM_DIR!
) else (
    echo [WARN] %VM_TAG%: some required files still missing after extract; runtime auto-download will retry.
)
endlocal & goto :eof

:download_error
echo [ERROR] Failed to download uv. Check network or proxy settings.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 1

:extract_error
del /f /q "%UV_ZIP%" 2>nul
echo [ERROR] Extraction failed.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 1

:python_error
echo [ERROR] Failed to install Python 3.13 ARM64.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 1

:venv_error
echo [ERROR] Failed to create virtual environment.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 1

:install_error
del /f /q "_constraints_tmp.txt" 2>nul
echo.
echo [ERROR] Dependency installation failed. See errors above.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 1

:print_help
echo.
echo  QAIModelBuilder - Setup.bat
echo  Sets up the Python environment ^(uv + ARM64 Python 3.13 venv^), installs
echo  runtime dependencies, downloads tools ^(PortableGit / QAIRT SDK^), and
echo  pre-deploys model/TTS data.
echo.
echo  USAGE:
echo      Setup.bat [options]
echo.
echo  OPTIONS:
echo      --dev          Also install the developer / test toolchain:
echo                     dev extras  ^(ruff, mypy, pytest, import-linter, ...^)
echo                     e2e extras  ^(playwright, pytest-playwright^)
echo                     + the ~150MB Chromium browser for e2e tests.
echo                     Default OFF. End users do NOT need this; only
echo                     contributors who run the test suite should pass it.
echo.
echo      --no-builder   Skip Step 8 ^(QAIRT model-builder environment:
echo                     ~2GB SDK download + x64 converter venv + VS build
echo                     tools^). The base runtime with pre-built models does
echo                     NOT need it. Re-run without this flag later to add it.
echo.
echo      --desktop      Also install the desktop-shell BUILD toolchain
echo                     ^(Step 9: Rust toolchain + tauri-cli^) needed to
echo                     COMPILE Build.bat --desktop. This is for DEVELOPERS
echo                     only — compiling the Tauri shell from source. Costs
echo                     ^~600MB rustup download + 5-10min tauri-cli compile.
echo                     NOT needed to RUN the desktop exe ^(WebView2 is
echo                     installed unconditionally in Step 10^). Do NOT combine
echo                     with --no-builder ^(MSVC C++ tools from Step 8.3 are
echo                     required to compile tauri-cli's native deps^).
echo.
echo      --no-pause     Do not pause at the end ^(used when Setup.bat is
echo                     called from another script^).
echo.
echo      --help, -h, /? Show this help and exit ^(installs nothing^).
echo.
echo  EXAMPLES:
echo      Setup.bat                     End-user install ^(recommended^).
echo      Setup.bat --dev               Full developer environment + tests.
echo      Setup.bat --no-builder        Runtime only, skip model conversion env.
echo      Setup.bat --desktop           Also install Rust + tauri-cli for
echo                                    Build.bat --desktop ^(adds ^~10min^).
echo      Setup.bat --dev --no-builder  Dev tools, but skip the 2GB QAIRT SDK.
echo.
echo  After setup:  Start.bat ^(run server^)  ^|  qai.bat ^(CLI^)  ^|  Console.bat ^(venv shell^)
echo.
exit /b 0

:print_elapsed
set "_T=%TIME: =0%"
for /f "tokens=1-3 delims=:." %%a in ("%_T%") do set /a "_END_S=(1%%a-100)*3600+(1%%b-100)*60+(1%%c-100)"
set /a "_ELAPSED=_END_S-_START_S"
if %_ELAPSED% lss 0 set /a "_ELAPSED+=86400"
set /a "_EM=_ELAPSED/60"
set /a "_ES=_ELAPSED%%60"
echo [INFO] Total elapsed: %_EM%m %_ES%s
exit /b 0


REM ===========================================================================
REM  Subroutine: setup_desktop_toolchain  (Step 9; opt-in via --desktop)
REM ===========================================================================
REM Build.bat --desktop / --desktop-dev compiles the Tauri 2 desktop shell
REM (desktop\src-tauri\, Rust). That needs:
REM   1) cargo (Rust toolchain)        -- from rustup, into %USERPROFILE%\.cargo
REM   2) MSVC C++ toolchain + Win SDK  -- installed by Step 8.3 (install_vs.ps1);
REM                                       REQUIRED to compile native crates like
REM                                       `ring` (assert.h / cl.exe / link.exe).
REM   3) `cargo tauri` subcommand      -- tauri-cli, via `cargo install --locked`
REM   (4) WebView2 runtime             -- preinstalled on Win11; Win10 handled by
REM                                       the Tauri bundle installer at runtime.
REM
REM This runs AFTER Step 8 so (2) exists. We then load the VS build environment
REM (vcvars / VsDevCmd) so cargo's cc-rs build scripts see INCLUDE / LIB / cl.exe
REM -- the earlier ordering (Step 5c before Step 8) is exactly why the previous
REM run died with "fatal error: 'assert.h' file not found" while building `ring`.
REM
REM Cost: rustup minimal toolchain ~600 MB; tauri-cli compile ~300 MB + 5-10 min
REM on ARM64. OPT-IN: skipped unless Setup.bat --desktop was passed.
REM
REM Idempotent: cargo present -> skip rustup; `cargo tauri --version` ok -> skip
REM the (re)install. Non-fatal: every failure only warns; the Web UI is fine.
:setup_desktop_toolchain
echo.
echo -- Step 9: Rust toolchain + tauri-cli ^(desktop build^) ------------------------
if not "%DESKTOP_EXTRAS%"=="1" (
    echo [SKIP] Desktop build toolchain not requested ^(opt-in: pass --desktop^).
    echo [INFO] Web UI ^(Start.bat^) does NOT need it. To enable
    echo [INFO] Build.bat --desktop, re-run:  Setup.bat --desktop
    exit /b 0
)

REM --- Pre-check: MSVC C++ toolchain must be present BEFORE we spend 5-10 min ---
REM compiling. `cargo install tauri-cli` builds native crates (ring) that need
REM cl.exe + the CRT/SDK headers. If Step 8 was skipped (--no-builder) or VS is
REM otherwise absent, fail fast with a clear hint rather than after a long build.
if "%NO_BUILDER%"=="1" (
    echo [WARN] --no-builder was passed, so Step 8.3 did NOT install the MSVC C++
    echo [WARN] toolchain. tauri-cli's native deps ^(ring^) cannot compile without
    echo [WARN] it. Re-run WITHOUT --no-builder ^(keep --desktop^) to install both.
    exit /b 0
)

REM --- Inject cargo bin into PATH (cargo may have been installed previously) ---
set "CARGO_BIN=%USERPROFILE%\.cargo\bin"
if exist "%CARGO_BIN%\cargo.exe" set "PATH=%CARGO_BIN%;%PATH%"

REM --- 9.1: Ensure cargo (Rust toolchain) is installed ---
where cargo >nul 2>&1
if not errorlevel 1 goto :sd_rust_ready

echo [INFO] cargo not found; installing Rust toolchain via rustup-init...
set "RUSTUP_URL=https://win.rustup.rs/aarch64"
set "RUSTUP_EXE=%DL_DIR%\rustup-init.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%RUSTUP_URL%" ^
    -OutFile "%RUSTUP_EXE%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 100000 ^
    -MaxRetries 3 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 300
if not exist "%RUSTUP_EXE%" (
    echo [WARN] Failed to download rustup-init.exe. Desktop build will be unavailable.
    echo [WARN] Install Rust manually from https://rustup.rs then re-run Setup.bat --desktop.
    exit /b 0
)

echo [INFO] Running rustup-init ^(silent, minimal profile; may take a few minutes^)...
REM -y / --default-host aarch64 / --default-toolchain stable / --profile minimal
REM / --no-modify-path (we manage PATH ourselves here and in Build.bat).
"%RUSTUP_EXE%" -y --default-host aarch64-pc-windows-msvc --default-toolchain stable --profile minimal --no-modify-path
set "RUSTUP_EXIT=%ERRORLEVEL%"
del /f /q "%RUSTUP_EXE%" 2>nul
if not "%RUSTUP_EXIT%"=="0" (
    echo [WARN] rustup-init failed ^(exit %RUSTUP_EXIT%^). Desktop build will be unavailable.
    echo [WARN] Install Rust manually from https://rustup.rs then re-run Setup.bat --desktop.
    exit /b 0
)
if exist "%CARGO_BIN%\cargo.exe" set "PATH=%CARGO_BIN%;%PATH%"
where cargo >nul 2>&1
if errorlevel 1 (
    echo [WARN] rustup-init claimed success but cargo.exe is not on PATH.
    echo [WARN] Expected at: %CARGO_BIN%\cargo.exe
    exit /b 0
)
echo [OK]   Rust toolchain installed:
cargo --version

:sd_rust_ready

REM --- 9.1b: Functional verification of cargo ---
REM `where cargo` only checks that cargo.exe EXISTS on PATH. But cargo.exe
REM may be a rustup proxy shim whose backing toolchain has been removed
REM (e.g. ~/.rustup was deleted). In that state, `where cargo` succeeds but
REM `cargo --version` fails with "rustup could not choose a version of cargo
REM to run, because ... no default is configured". We must detect this and
REM repair it by installing/restoring the default toolchain.
cargo --version >nul 2>&1
if not errorlevel 1 goto :sd_cargo_functional

REM cargo shim exists but no toolchain behind it. Try to repair.
echo [WARN] cargo.exe found but no usable toolchain ^(proxy shim without backing^).
where rustup >nul 2>&1
if errorlevel 1 (
    echo [WARN] rustup.exe also not found; cannot self-repair. Will attempt fresh install.
    goto :sd_fresh_rustup_install
)
echo [INFO] Attempting repair: rustup default stable ^(downloads stable toolchain^)...
rustup default stable
if errorlevel 1 (
    echo [WARN] `rustup default stable` failed. Attempting fresh install via rustup-init...
    goto :sd_fresh_rustup_install
)
cargo --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] cargo still non-functional after `rustup default stable`. Trying fresh install...
    goto :sd_fresh_rustup_install
)
echo [OK]   Rust toolchain repaired:
cargo --version
goto :sd_cargo_functional

:sd_fresh_rustup_install
REM Full fresh install via rustup-init (handles the case where both shim and
REM ~/.rustup are in a broken state — rustup-init will overwrite/recreate).
echo [INFO] Downloading rustup-init for fresh Rust install...
set "RUSTUP_URL=https://win.rustup.rs/aarch64"
set "RUSTUP_EXE=%DL_DIR%\rustup-init.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%RUSTUP_URL%" ^
    -OutFile "%RUSTUP_EXE%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 100000 ^
    -MaxRetries 3 ^
    -StallTimeoutSec 60 ^
    -AttemptTimeoutSec 300
if not exist "%RUSTUP_EXE%" (
    echo [WARN] Failed to download rustup-init.exe. Desktop build will be unavailable.
    echo [WARN] Fix manually: rustup default stable  ^(or install from https://rustup.rs^)
    exit /b 0
)
"%RUSTUP_EXE%" -y --default-host aarch64-pc-windows-msvc --default-toolchain stable --profile minimal --no-modify-path
set "RUSTUP_EXIT=%ERRORLEVEL%"
del /f /q "%RUSTUP_EXE%" 2>nul
if not "%RUSTUP_EXIT%"=="0" (
    echo [WARN] rustup-init failed ^(exit %RUSTUP_EXIT%^). Desktop build will be unavailable.
    exit /b 0
)
if exist "%CARGO_BIN%\cargo.exe" set "PATH=%CARGO_BIN%;%PATH%"
cargo --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] Rust still non-functional after fresh install. Desktop build unavailable.
    exit /b 0
)
echo [OK]   Rust toolchain installed ^(fresh^):
cargo --version

:sd_cargo_functional

REM --- 9.2: tauri-cli idempotency check (skip if already usable) ---
cargo tauri --version >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] tauri-cli already installed:
    cargo tauri --version
    exit /b 0
)

REM --- 9.3: Load the MSVC build environment (vcvars) so cargo can compile ---
REM `cargo install tauri-cli` builds native C crates (ring) via cc-rs, which
REM needs cl.exe + INCLUDE (assert.h, ...) + LIB. A bare cmd does NOT have
REM these; we must run VS's vcvarsall.bat / VsDevCmd.bat first. We locate VS
REM with vswhere (the same tool install_vs.ps1 / Uninstall.bat use), then call
REM vcvarsall for the ARM64 target. This is REQUIRED -- it is the missing step
REM that caused the previous "'assert.h' file not found" failure.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VCVARSALL="
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.ARM64 -property installationPath 2^>nul`) do (
        if exist "%%I\VC\Auxiliary\Build\vcvarsall.bat" set "VCVARSALL=%%I\VC\Auxiliary\Build\vcvarsall.bat"
    )
    if not defined VCVARSALL (
        REM Fall back to any VS with C++ tools (ARM64 component query returned nothing).
        for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do (
            if exist "%%I\VC\Auxiliary\Build\vcvarsall.bat" set "VCVARSALL=%%I\VC\Auxiliary\Build\vcvarsall.bat"
        )
    )
)
if not defined VCVARSALL (
    echo [WARN] Could not locate vcvarsall.bat ^(VS C++ build tools not found via
    echo [WARN] vswhere^). tauri-cli's native deps ^(ring^) need cl.exe + the CRT /
    echo [WARN] Windows SDK headers. Step 8.3 ^(install_vs.ps1^) should have
    echo [WARN] installed them; re-run Setup.bat --desktop ^(without --no-builder^),
    echo [WARN] or open "x64_arm64 Native Tools Command Prompt for VS" and run:
    echo [WARN]     cargo install tauri-cli --version "^>=2.0.0" --locked
    exit /b 0
)

REM Host is ARM64; target is ARM64 -> use the native arm64 vcvars argument.
echo [INFO] Loading MSVC build environment: "%VCVARSALL%" arm64
call "%VCVARSALL%" arm64 >nul
if errorlevel 1 (
    echo [WARN] vcvarsall.bat arm64 failed; trying x64_arm64 cross tools...
    call "%VCVARSALL%" x64_arm64 >nul
)
REM Re-prepend cargo bin: vcvarsall rewrote PATH and may have dropped it.
if exist "%CARGO_BIN%\cargo.exe" set "PATH=%CARGO_BIN%;%PATH%"
REM Sanity: confirm cl.exe is now visible (best-effort log; not fatal).
where cl.exe >nul 2>&1
if errorlevel 1 (
    echo [WARN] cl.exe still not on PATH after vcvars; the cargo build may fail.
    echo [WARN] If it does, run it from the VS Native Tools Command Prompt.
) else (
    echo [OK]   MSVC environment loaded ^(cl.exe visible; INCLUDE / LIB set^).
)

REM --- 9.4: Install tauri-cli (now with MSVC env loaded) ---
REM Work around Windows schannel SSL certificate revocation check failures
REM (CRYPT_E_NO_REVOCATION_CHECK 0x80092012) commonly seen on corporate
REM networks / behind proxies where OCSP/CRL endpoints are blocked. cargo
REM uses Windows-native TLS (schannel) by default; if the revocation check
REM cannot reach the CRL/OCSP responder, the TLS handshake fails entirely.
REM Setting CARGO_HTTP_CHECK_REVOKE=false tells cargo's internal HTTP client
REM to skip certificate revocation verification. This is safe for a package
REM install from crates.io (the registry itself is signed with cargo's
REM built-in key). The variable is scoped to this setlocal block and does
REM NOT persist after Setup.bat exits.
set "CARGO_HTTP_CHECK_REVOKE=false"
echo [INFO] Installing tauri-cli ^(cargo install tauri-cli --version ^>=2.0.0 --locked^)...
echo [INFO] First-time install compiles from source; expect 5-10 minutes on ARM64.
cargo install tauri-cli --version ">=2.0.0" --locked
set "TAURI_CLI_EXIT=%ERRORLEVEL%"
if not "%TAURI_CLI_EXIT%"=="0" (
    echo [WARN] `cargo install tauri-cli` failed ^(exit %TAURI_CLI_EXIT%^).
    echo [WARN] Desktop build ^(Build.bat --desktop^) will be unavailable.
    echo [WARN] Common causes:
    echo [WARN]   * MSVC C++ toolchain / Windows SDK incomplete ^(needs cl.exe +
    echo [WARN]     CRT/SDK headers like assert.h^). Open the VS Installer and
    echo [WARN]     ensure "Desktop development with C++" + the ARM64 build
    echo [WARN]     tools + a Windows 11 SDK are installed.
    echo [WARN]   * Network/proxy blocking crates.io. Retry on a stable connection.
    echo [WARN] You can retry later from a VS Native Tools prompt with:
    echo [WARN]     cargo install tauri-cli --version "^>=2.0.0" --locked
    exit /b 0
)
echo [OK]   tauri-cli installed:
cargo tauri --version
exit /b 0


REM ===========================================================================
REM  Subroutine: install_webview2
REM  Installs the WebView2 Fixed Version Runtime into data\bin\webview2\.
REM  Runs unconditionally (no --desktop required) because ALL users who run
REM  the desktop exe need WebView2. Copies from the system Evergreen
REM  installation if present; otherwise warns with manual instructions.
REM  Idempotent, non-fatal.
REM ===========================================================================
:install_webview2
echo.
echo -- Step 10: WebView2 Fixed Version Runtime ^(ARM64^) ---------------------------

REM First, check if the system WebView2 Evergreen Runtime is properly registered
REM and usable. If it is, we don't need our own Fixed Version copy (~811 MB).
REM
REM Detection method: check the registry key that WebView2's
REM GetAvailableCoreWebView2BrowserVersionString() API reads. This is the same
REM mechanism Tauri/wry uses internally to locate the runtime. If the key exists
REM and points to a valid directory with msedgewebview2.exe, the system runtime
REM is functional and we skip the install entirely.
REM
REM The registry key lives at:
REM   HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BEB-13D6E2756B32}  (pv = version)
REM   or HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-...}
REM   or HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-...}
REM
REM If NONE of these exist, the Evergreen runtime is "unregistered" (the exact
REM scenario that caused the black screen on this machine after reboot).

set "WV2_DIR=data\bin\webview2"
set "WV2_SYSTEM_OK=0"

REM Check registry (same order as WebView2Loader.dll probes)
for %%K in (
    "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BEB-13D6E2756B32}"
    "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BEB-13D6E2756B32}"
    "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BEB-13D6E2756B32}"
) do (
    reg query %%K /v pv >nul 2>&1
    if not errorlevel 1 set "WV2_SYSTEM_OK=1"
)

REM Also verify the runtime binary actually exists (registry can point to a
REM deleted/moved location after a botched update)
if "%WV2_SYSTEM_OK%"=="1" (
    set "WV2_BINARY_FOUND=0"
    for /f "delims=" %%D in ('dir /b /ad "C:\Program Files (x86)\Microsoft\EdgeWebView\Application" 2^>nul ^| findstr /r "^[0-9]"') do (
        if exist "C:\Program Files (x86)\Microsoft\EdgeWebView\Application\%%D\msedgewebview2.exe" set "WV2_BINARY_FOUND=1"
    )
    if "!WV2_BINARY_FOUND!"=="0" (
        echo [INFO] Registry says WebView2 is installed but binary not found; will install Fixed Version.
        set "WV2_SYSTEM_OK=0"
    )
)

REM If system runtime is healthy AND we don't already have a local copy, skip.
if "%WV2_SYSTEM_OK%"=="1" (
    echo [SKIP] System WebView2 Evergreen Runtime is registered and functional.
    echo [SKIP] Fixed Version not needed on this machine.
    exit /b 0
)

REM System WebView2 is broken/missing. Check if we already installed Fixed Version.
for /f "delims=" %%V in ('dir /b /ad "%WV2_DIR%" 2^>nul') do (
    if exist "%WV2_DIR%\%%V\msedgewebview2.exe" (
        echo [SKIP] WebView2 Fixed Version %%V already installed at %WV2_DIR%\%%V
        exit /b 0
    )
)

REM Need to install. Copy from the system Evergreen directory (the binaries
REM exist even when the registry is broken — that's exactly the scenario we hit).
echo [INFO] System WebView2 registry is missing/broken. Installing Fixed Version
echo [INFO] so the desktop exe can render without depending on the system registry.
set "WV2_SYSTEM_DIR="
set "WV2_VERSION="
for /f "delims=" %%D in ('dir /b /ad "C:\Program Files (x86)\Microsoft\EdgeWebView\Application" 2^>nul ^| findstr /r "^[0-9]"') do (
    if exist "C:\Program Files (x86)\Microsoft\EdgeWebView\Application\%%D\msedgewebview2.exe" (
        set "WV2_SYSTEM_DIR=C:\Program Files (x86)\Microsoft\EdgeWebView\Application\%%D"
        set "WV2_VERSION=%%D"
    )
)

if not defined WV2_SYSTEM_DIR (
    echo [WARN] No system WebView2 binaries found to copy from.
    echo [WARN] The desktop exe may show a black screen without WebView2.
    echo [WARN] To fix, install WebView2 manually:
    echo [WARN]   1. Download "Evergreen Standalone Installer" ^(ARM64^) from:
    echo [WARN]      https://developer.microsoft.com/en-us/microsoft-edge/webview2/
    echo [WARN]   2. Run it ^(installs the system Evergreen Runtime^).
    echo [WARN]   3. Then re-run Setup.bat ^(it will copy it into data\bin\webview2\^).
    exit /b 0
)

set "WV2_TARGET=%WV2_DIR%\%WV2_VERSION%"
echo [INFO] Copying WebView2 %WV2_VERSION% to %WV2_TARGET% ...
echo [INFO] ^(This is needed because the system registry is broken; ~811 MB, ~10s^)
if not exist "%WV2_TARGET%" mkdir "%WV2_TARGET%"
robocopy "%WV2_SYSTEM_DIR%" "%WV2_TARGET%" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if exist "%WV2_TARGET%\msedgewebview2.exe" (
    echo [OK]   WebView2 Fixed Version %WV2_VERSION% installed.
) else (
    echo [WARN] WebView2 copy failed. Desktop exe may show a black screen.
)
exit /b 0


REM ===========================================================================
REM  Subroutine: fetch_vendor_deps
REM  Download vendor-deps.7z (heavy dependency caches NOT shipped in the
REM  release artifact) and merge its contents into the local vendor\ dir.
REM  Provides: vendor\whl\ vendor\g2p_data\ vendor\nltk_data\ vendor\tiktoken\
REM ===========================================================================
:fetch_vendor_deps
echo.
echo -- Step 0b: vendor dependency bundle ^(vendor-deps.7z^) ------------------------

set "VDEPS_URL=https://www.aidevhome.com/data/adh2/attr/vendor-deps.7z"
set "VDEPS_ARCHIVE=%DL_DIR%\vendor-deps.7z"
set "VDEPS_TMP=%DL_DIR%\_vendor_deps_tmp"
set "VDEPS_7ZR=data\bin\7zr\7zr.exe"

REM --- Ensure 7zr.exe is available BEFORE the idempotency skip guard ---
REM 7zr.exe is needed to extract vendor-deps.7z. We check here unconditionally
REM so that a re-run after data\bin\ was cleaned always re-downloads it,
REM regardless of whether the vendor caches already exist.
if not exist "%VDEPS_7ZR%" (
    echo [INFO] Downloading standalone 7zr.exe ^(~600KB^) to data\bin\7zr\ ...
    if not exist "data\bin\7zr" mkdir "data\bin\7zr"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
        -Url "https://www.7-zip.org/a/7zr.exe" ^
        -OutFile "data\bin\7zr\7zr.exe" ^
        -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
        -MinSize 100000 ^
        -MaxRetries 3 ^
        -StallTimeoutSec 30 ^
        -AttemptTimeoutSec 60
    if exist "%VDEPS_7ZR%" (
        echo [OK]   7zr.exe downloaded: %VDEPS_7ZR%
    ) else (
        echo [WARN] Failed to download 7zr.exe from 7-zip.org.
    )
) else (
    echo [SKIP] 7zr.exe already present: %VDEPS_7ZR%
)

REM --- Idempotency: skip if all four caches already present ---
REM Use a goto-based chain (not nested `if exist ... (`) because parentheses
REM inside the echo text below would otherwise break cmd's block parser.
if not exist "vendor\whl" goto :vdeps_need_fetch
if not exist "vendor\g2p_data" goto :vdeps_need_fetch
if not exist "vendor\nltk_data" goto :vdeps_need_fetch
if not exist "vendor\tiktoken" goto :vdeps_need_fetch
echo [SKIP] vendor dependency caches already present: whl / g2p_data / nltk_data / tiktoken
exit /b 0

:vdeps_need_fetch
REM --- Reuse a previously downloaded archive if present ---
if exist "%VDEPS_ARCHIVE%" (
    echo [SKIP] Archive already downloaded: %VDEPS_ARCHIVE%
    goto :vdeps_extract
)

echo [INFO] Downloading vendor dependency bundle...
echo [INFO] URL: %VDEPS_URL%
echo [INFO] Download supports resume - safe to interrupt and re-run.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%VDEPS_URL%" ^
    -OutFile "%VDEPS_ARCHIVE%" ^
    -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
    -MinSize 20000000 ^
    -MaxRetries 5 ^
    -StallTimeoutSec 90 ^
    -AttemptTimeoutSec 1800

:vdeps_check_dl
if not exist "%VDEPS_ARCHIVE%" (
    echo [WARN] Failed to download vendor-deps.7z. ARM64-only wheels and the
    echo [WARN] g2p / NLTK / tiktoken offline data will be unavailable; pip will
    echo [WARN] fall back to PyPI for what it can. You can re-run Setup.bat
    echo [WARN] later, or manually download from:
    echo [WARN]     %VDEPS_URL%
    echo [WARN] and place it at: %VDEPS_ARCHIVE%
    exit /b 0
)

:vdeps_extract
if not exist "%VDEPS_7ZR%" (
    echo [WARN] Could not obtain 7zr.exe; cannot extract vendor-deps.7z.
    echo [WARN] Install 7-Zip manually and extract %VDEPS_ARCHIVE% into vendor\.
    exit /b 0
)

echo [INFO] Extracting vendor-deps.7z...
if exist "%VDEPS_TMP%" rmdir /s /q "%VDEPS_TMP%" 2>nul
mkdir "%VDEPS_TMP%"
"%VDEPS_7ZR%" x -y "-o%VDEPS_TMP%" "%VDEPS_ARCHIVE%" -bso0 -bsp1
echo [INFO] 7zr.exe exit code: %errorlevel%

REM --- Merge extracted content into vendor\ ---
REM The archive may either contain a top-level vendor\ folder, or the four
REM cache dirs directly at its root. Detect and merge accordingly so both
REM packaging layouts work. We explicitly DROP any vendor\nltk_data\.predeploy_ok
REM sentinel that may have leaked into the archive: it is a per-machine state
REM file (records the packaging machine's timestamp + Python interpreter path)
REM and must be re-generated locally by predeploy_tts_runtime.py (Step 6), not
REM transplanted from the build machine.
if not exist "vendor" mkdir "vendor"
echo [INFO] Merging extracted files into vendor\ ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$tmp = '%VDEPS_TMP%'; $dest = 'vendor'; " ^
    "$src = if (Test-Path (Join-Path $tmp 'vendor')) { Join-Path $tmp 'vendor' } else { $tmp }; " ^
    "Get-ChildItem -LiteralPath $src -Force | ForEach-Object { " ^
    "  $target = Join-Path $dest $_.Name; " ^
    "  Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force " ^
    "}; " ^
    "Get-ChildItem -LiteralPath $dest -Recurse -Force -Filter '.predeploy_ok' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue; " ^
    "Write-Host '[INFO] Merge complete.'"

if exist "%VDEPS_TMP%" rmdir /s /q "%VDEPS_TMP%" 2>nul

if exist "vendor\whl" (
    echo [OK]   vendor dependency bundle merged into vendor\.
    REM Merge succeeded -- delete the downloaded archive to reclaim disk space.
    del /f /q "%VDEPS_ARCHIVE%" 2>nul
) else (
    echo [WARN] vendor-deps.7z extracted but vendor\whl\ not found afterwards.
    echo [WARN] Check the archive layout at %VDEPS_ARCHIVE%.
)
exit /b 0


REM ===========================================================================
REM  Step 8 implementation: QAIRT model-builder environment
REM  (full port of V1 Setup_Builder_Env.bat; uses setup_qairt_env.py +
REM   install_vs.ps1 only - NO qai.exe dependency)
REM ===========================================================================
:setup_builder_env
echo.
echo  +----------------------------------------------------------+
echo  ^|   Step 8: QAIRT model-builder environment                ^|
echo  +----------------------------------------------------------+

set "VENV_310_DIR=%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_x64_310"
set "SETUP_HELPER=scripts\setup\setup_qairt_env.py"
set "ARIA2C_EXE=data\bin\aria2c\aria2c.exe"

REM QAIRT SDK settings: config-file is the ONLY source of truth.
REM   scripts\qairt_release.json  (field "qairt_version")
REM Any pre-set QAIRT_VERSION / QAIRT_SDK_ROOT / QAIRT_DOWNLOAD_URL from the
REM environment is IGNORED here (previously env vars won via `if not defined`,
REM which caused stale-version bugs when an old shell had them exported).
REM If qairt_release.json is missing or unreadable, abort — no silent fallback.
set "QAIRT_VERSION="
set "QAIRT_SDK_ROOT="
set "QAIRT_DOWNLOAD_URL="
if not exist "%ROOT_DIR%scripts\qairt_release.json" (
    echo [ERROR] scripts\qairt_release.json not found.
    echo [ERROR] This file is the single source of truth for the QAIRT SDK version.
    exit /b 1
)
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Get-Content -Raw -LiteralPath '%ROOT_DIR%scripts\qairt_release.json' | ConvertFrom-Json).qairt_version } catch { exit 1 }" 2^>nul`) do set "QAIRT_VERSION=%%V"
if not defined QAIRT_VERSION (
    echo [ERROR] Could not read qairt_version from scripts\qairt_release.json
    echo [ERROR] The config file is the single source of truth for the QAIRT SDK version.
    echo [ERROR] Fix scripts\qairt_release.json ^(must contain a "qairt_version" field^) and re-run Setup.bat.
    exit /b 1
)
set "QAIRT_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\%QAIRT_VERSION%"
set "QAIRT_DOWNLOAD_URL=https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/%QAIRT_VERSION%/v%QAIRT_VERSION%.zip"
set "QAIRT_URL=%QAIRT_DOWNLOAD_URL%"
set "QAIRT_ZIP=%DL_DIR%\_qairt_tmp.zip"
set "QAIRT_VENDOR_ZIP=vendor\qairt\v%QAIRT_VERSION%.zip"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [ERROR] ARM64 venv missing; base setup must succeed before Step 8.
    exit /b 1
)

echo.
echo -- Step 8.0b: Pre-generate data\config\qairt_env.json ---------
REM Generate the (small, cheap) qairt_env.json early so that even if Step 8.2
REM (the ~2GB SDK download) is interrupted, the config file already exists for
REM diagnostic / partial-functionality use. Step 8.6 re-generates it later
REM with the post-install paths refined.
REM
REM ORDERING BUG FIX (2026-06-19): we MUST NOT pre-generate when the target
REM SDK version (%QAIRT_SDK_ROOT%) is not yet present on disk -- gen_config()
REM in setup_qairt_env.py falls back to "the latest other SDK version it can
REM find under C:\Qualcomm\AIStack\QAIRT" when the configured one is missing.
REM On a machine that still has an OLD QAIRT install from a previous setup,
REM this used to write a config pointing at the OLD version, even though
REM Step 8.2 was about to download a NEW one. Step 8.6 would later overwrite
REM the file, but if anything between 8.0b and 8.6 ran the model-builder
REM pipeline it would be stuck on the old SDK (and the new download would be
REM wasted).
REM
REM Fix: only pre-generate when the target SDK is ALREADY installed (== this
REM is the idempotent re-run case where 8.2 will [SKIP]). On a fresh install
REM (or a different version upgrade), we let 8.2 run first; 8.6 then writes
REM the config against the freshly-installed target version.
if exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    "%VENV_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --gen-config --sdk-root "%QAIRT_SDK_ROOT%"
    if errorlevel 1 (
        echo [WARN] Pre-generation of qairt_env.json failed; will retry in Step 8.6.
    ) else (
        echo [OK]   qairt_env.json pre-generated at data\config\qairt_env.json
    )
) else (
    echo [SKIP] Target SDK %QAIRT_VERSION% not yet installed; will gen-config after Step 8.2 download.
    echo [SKIP] ^(Avoids fallback to a stale older SDK from a previous install.^)
)

echo.
echo -- Step 8.1: x86_64 Python 3.10 venv ^(for model conversion^) ---
call :sb_install_venv_310
echo.
echo -- Step 8.2: QAIRT SDK %QAIRT_VERSION% ------------------------
call :sb_install_qairt_sdk
if errorlevel 1 (
    echo.
    echo [ERROR] QAIRT SDK %QAIRT_VERSION% could not be installed ^(see above^).
    echo [ERROR] Aborting: this project targets EXACTLY this SDK version and will
    echo [ERROR] NOT fall back to a different QAIRT version that may be
    echo [ERROR] incompatible with our toolchain.
    echo [ERROR] Fix the pinned version in scripts\qairt_release.json, or place a
    echo [ERROR] valid zip at %QAIRT_VENDOR_ZIP%, then re-run Setup.bat.
    exit /b 1
)
echo.
echo -- Step 8.3: Install / update VS 2022 build tools ^(silent^) ----
if exist "scripts\setup\install_vs.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\install_vs.ps1" -LogDir "%DL_DIR%"
    if errorlevel 1 echo [WARN] VS installation step encountered an issue.
) else (
    echo [WARN] scripts\setup\install_vs.ps1 not found; skipping VS install.
)
echo.
echo -- Step 8.4: QAIRT dependency verification ^(informational^) -----
set "QAIRT_CHECK_DEP=%QAIRT_SDK_ROOT%\bin\check-windows-dependency.ps1"
if exist "%QAIRT_CHECK_DEP%" (
    echo [INFO] Running QAIRT dependency checker ^(dry-run, informational^)...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; & '%QAIRT_CHECK_DEP%' -DryRun" 2>nul
) else (
    echo [INFO] check-windows-dependency.ps1 not present; skipping informational check.
)
echo.
echo -- Step 8.5a: x64 converter deps ^(into .venv_x64_310^) ----------
if exist "%VENV_310_DIR%\Scripts\python.exe" (
    "%VENV_310_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --install-python-deps
    if errorlevel 1 echo [WARN] Some x64 converter deps failed to install.
) else (
    echo [WARN] .venv_x64_310 missing; skipping x64 converter deps.
)
echo.
echo -- Step 8.5b: ARM64 inference deps + Pack deps ----------------
"%VENV_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --install-inference-deps
if errorlevel 1 echo [WARN] Some ARM64 inference deps failed to install.
echo.
echo -- Step 8.6: Generate data\config\qairt_env.json --------------
"%VENV_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --gen-config --sdk-root "%QAIRT_SDK_ROOT%"
if errorlevel 1 (
    echo [ERROR] Failed to generate qairt_env.json for %QAIRT_VERSION%.
    echo [ERROR] Aborting: refusing to leave the environment in a half-configured state.
    exit /b 1
) else (
    echo [OK]   qairt_env.json generated at data\config\qairt_env.json
)
echo.
echo -- Step 8.7: Verify model-builder environment -----------------
"%VENV_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --verify --sdk-root "%QAIRT_SDK_ROOT%"
REM State-Truth-First: report the REAL readiness of the model-conversion
REM environment instead of unconditionally claiming success. The QAIRT SDK
REM (~2GB) and qairt_env.json are both required to run the conversion pipeline;
REM if either is missing (e.g. interrupted download), say so clearly so the
REM user knows to re-run Setup.bat -- rather than discovering it later via an
REM obscure runtime error in the model-builder skill.
set "_MB_READY=1"
if not exist "data\config\qairt_env.json" set "_MB_READY=0"
if not exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" set "_MB_READY=0"
if "%_MB_READY%"=="1" (
    echo [OK]   QAIRT model-builder environment is READY ^(conversion pipeline usable^).
) else (
    echo [WARN] QAIRT model-builder environment is INCOMPLETE.
    echo [WARN]   - Model CONVERSION ^(custom ONNX -^> NPU .bin^) needs the QAIRT SDK
    echo [WARN]     + data\config\qairt_env.json; one or both are missing above.
    echo [WARN]   - Re-run Setup.bat ^(without --no-builder^) to finish; the SDK
    echo [WARN]     download supports resume, so re-running continues where it stopped.
    echo [WARN]   - NOTE: running pre-built models for INFERENCE does NOT need this
    echo [WARN]     environment -- use the aihub-model-run skill instead.
)
echo [OK]   QAIRT model-builder environment step complete.
exit /b 0


:sb_install_venv_310
REM State-Truth-First (AGENTS.md 铁律1): same completeness probe as Step 3's
REM ARM64 venv. The old check ran ``python -c "import sys"`` -- a weak proxy
REM that a HALF-created venv (missing Scripts\activate.bat / pip, e.g. an
REM interrupted ``uv venv --seed``) still passes, so it was wrongly skipped
REM and the later x64 converter-deps install silently failed. Require BOTH
REM ``Scripts\activate.bat`` to exist AND ``python -m pip --version`` to
REM succeed; if EITHER is missing the venv is incomplete and we recreate it.
if exist "%VENV_310_DIR%\Scripts\python.exe" if exist "%VENV_310_DIR%\Scripts\activate.bat" (
    "%VENV_310_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
    if not errorlevel 1 (
        echo [SKIP] .venv_x64_310 already exists and is complete: %VENV_310_DIR%
        exit /b 0
    )
)
if exist "%VENV_310_DIR%\Scripts\python.exe" echo [WARN] .venv_x64_310 exists but is incomplete (missing activate.bat / pip); recreating...
echo [INFO] Installing x86_64 Python 3.10 via uv...
REM --no-bin / --no-registry: same rationale as Step 2 (we use venv_310's
REM python.exe directly; no shim or registry registration needed).
uv python install --no-bin --no-registry cpython-3.10-windows-x86_64
if errorlevel 1 (
    echo [WARN] Failed to install x86_64 Python 3.10 ^(converter env unavailable^).
    exit /b 0
)
echo [INFO] Creating .venv_x64_310 ^(x86_64 Python 3.10^)...
uv venv "%VENV_310_DIR%" --python cpython-3.10-windows-x86_64 --seed --allow-existing
if errorlevel 1 (
    REM Retry without --allow-existing for older uv, then last-resort accept
    REM only a USABLE venv (python.exe AND activate.bat both present).
    uv venv "%VENV_310_DIR%" --python cpython-3.10-windows-x86_64 --seed 2>nul
    if errorlevel 1 (
        if exist "%VENV_310_DIR%\Scripts\python.exe" if exist "%VENV_310_DIR%\Scripts\activate.bat" (
            echo [WARN] uv venv reported an error, but a usable .venv_x64_310 exists. Continuing...
            exit /b 0
        )
        echo [WARN] Failed to create .venv_x64_310.
        exit /b 0
    )
)
echo [OK]   .venv_x64_310 created: %VENV_310_DIR%
exit /b 0


:sb_install_qairt_sdk
if exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    echo [SKIP] QAIRT SDK already installed: %QAIRT_SDK_ROOT%
    REM Even when we skip the (big) SDK extraction, still make sure 7za.exe is
    REM bootstrapped. :ensure_7za used to be reachable ONLY from the extraction
    REM path below (line ~2175); on a machine where the SDK was already present,
    REM that path never ran, so 7za.exe was never created -- which is exactly
    REM why it went missing despite Setup.bat completing successfully. It is
    REM idempotent (early-exits if 7za.exe is already cached) and never deletes
    REM 7za.exe, so calling it here on the skip path is safe and cheap.
    call :ensure_7za
    goto :sb_keep_qairt_zip
)
if exist "%QAIRT_VENDOR_ZIP%" (
    echo [INFO] Found vendor QAIRT zip: %QAIRT_VENDOR_ZIP%
    goto :sb_extract_qairt
)
echo [INFO] Downloading QAIRT SDK %QAIRT_VERSION% ^(~2GB^)...
echo [INFO] URL: %QAIRT_URL%
echo [INFO] Download supports resume ^(safe to interrupt and re-run^).

REM This is THE big one (~2 GB) and historically the place users hit
REM "Setup.bat looks frozen" most often. download_with_aria2c.ps1's stall
REM watchdog (kill aria2c if main file hasn't grown for StallTimeoutSec)
REM + retry loop (5 attempts with exponential backoff) is what stops the
REM hang. AttemptTimeoutSec=3600 gives a single attempt up to 1h on slow
REM networks (~600 KB/s sustained).
REM MinSize 1500000000 (1.5 GB): the QAIRT 2.46 zip is ~1.7 GB, so anything
REM below 1.5 GB after a "successful" aria2c run is corrupt -> retry.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
    -Url "%QAIRT_URL%" ^
    -OutFile "%QAIRT_ZIP%" ^
    -Aria2cExe "%ARIA2C_EXE%" ^
    -MinSize 1500000000 ^
    -MaxRetries 5 ^
    -StallTimeoutSec 90 ^
    -AttemptTimeoutSec 3600 ^
    -Connections 16

:sb_check_dl
if exist "%DL_DIR%\log" rmdir /s /q "%DL_DIR%\log" 2>nul
if not exist "%QAIRT_ZIP%" (
    echo [ERROR] QAIRT SDK download failed: %QAIRT_URL%
    echo [INFO]  Manually download from: %QAIRT_URL%
    echo [INFO]  Place the zip at: %QAIRT_VENDOR_ZIP%  then re-run Setup.bat.
    exit /b 1
)
set "QAIRT_VENDOR_ZIP=%QAIRT_ZIP%"

:sb_extract_qairt
echo [INFO] Extracting QAIRT SDK to %QAIRT_SDK_ROOT%...
set "QAIRT_EXTRACT_TMP=C:\Qualcomm\AIStack\QAIRT\_extract_tmp"
if not exist "C:\Qualcomm\AIStack\QAIRT" mkdir "C:\Qualcomm\AIStack\QAIRT"

REM --- Pre-extract guard: release any lingering lock on the .zip ---
REM State-Truth-First (AGENTS.md rule 5: exceptional-exit paths must be
REM covered). A PRIOR Setup that was force-killed (window closed / Ctrl+C)
REM can leave an ORPHAN aria2c.exe still holding the .zip's file handle, and
REM a stale "<zip>.aria2" control file beside it. On the next run the
REM download wrapper sees a complete zip and short-circuits to "[OK] Already
REM present" WITHOUT ever touching (and thus cleaning up) those leftovers --
REM so Expand-Archive below would hit "the process cannot access the file ...
REM because it is being used by another process" and the extraction would
REM silently produce no SDK. We therefore explicitly: (1) kill any aria2c
REM whose command line references THIS zip, (2) delete the stale .aria2
REM control file, (3) wait briefly for Windows to release the handle. This
REM is best-effort and never fails Setup.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='SilentlyContinue'; " ^
    "$zip = '%QAIRT_VENDOR_ZIP%'; " ^
    "$leaf = Split-Path $zip -Leaf; " ^
    "$killed = $false; " ^
    "Get-CimInstance Win32_Process -Filter \"Name='aria2c.exe'\" | Where-Object { $_.CommandLine -and ($_.CommandLine -like ('*' + $leaf + '*')) } | ForEach-Object { Write-Host ('[INFO] Killing orphan aria2c PID ' + $_.ProcessId + ' holding ' + $leaf); Stop-Process -Id $_.ProcessId -Force; $killed = $true }; " ^
    "if ($killed) { Start-Sleep -Seconds 2 }; " ^
    "$ctrl = $zip + '.aria2'; " ^
    "if (Test-Path -LiteralPath $ctrl) { Remove-Item -LiteralPath $ctrl -Force; Write-Host ('[INFO] Removed stale control file ' + $leaf + '.aria2') }"

REM --- Extract with 7za.exe (primary, fast, with progress) or Expand-Archive (fallback) ---
REM Even after the guard above, an AV scanner or a not-yet-flushed handle can
REM keep the zip locked for a second or two. Rather than fail on the first
REM "file in use", retry the extraction a few times with a short wait so a
REM transient lock self-heals instead of aborting the whole model-builder env.
REM
REM PRIMARY: data\bin\7zr\7za.exe -- the FULL standalone 7-Zip console build
REM   (~1 MB, console-only, no GUI, no installer). Supports .zip / .7z /
REM   .tar / .gz / .bz2 / etc. ~2-5 min for the ~1.7 GB QAIRT zip with its
REM   tens of thousands of small files. Bootstrapped from 7-Zip Extra
REM   archive (~530 KB) via :ensure_7za on first run, then cached.
REM
REM   We do NOT use 7zr.exe (already bundled for PortableGit / vendor-deps)
REM   here -- 7zr is the "7z-format-only" reduced build and rejects .zip
REM   input with "Cannot open the file as archive" (exit 2). 7za is the
REM   superset that handles .zip too.
REM
REM FALLBACK: PowerShell Expand-Archive (slow -- 15-40 min on SSD because
REM   of per-file PowerShell pipeline overhead -- but no extra download
REM   and works when 7-Zip Extra cannot be obtained).
REM
REM 7za.exe flags:
REM   x        extract with full paths
REM   -y       assume Yes to all prompts
REM   -o<dir>  output directory
REM   -bso1    standard output stream  (file lines)
REM   -bsp1    progress to standard output (percentage + speed; LIVE)
REM   -bse2    errors to stderr
REM This gives the user real-time progress like:
REM     45% 8123 - bin/aarch64-windows-msvc/qnn-net-run.exe
REM so the historical "Setup.bat looks frozen at Extracting QAIRT SDK"
REM symptom is gone.
set "QAIRT_EXTRACT_OK=0"
set "QAIRT_SEVEN_ZIP=data\bin\7zr\7za.exe"

REM Ensure 7za.exe is available (downloads + extracts 7-Zip Extra on first run; cached after).
call :ensure_7za

if not exist "%QAIRT_SEVEN_ZIP%" goto :qairt_extract_psh
echo [INFO] Extracting via 7za.exe ^(fast path, with live progress^)...

call :qairt_try_7za 1
if "%QAIRT_EXTRACT_OK%"=="1" goto :qairt_extract_finalize
timeout /t 3 /nobreak >nul
call :qairt_try_7za 2
if "%QAIRT_EXTRACT_OK%"=="1" goto :qairt_extract_finalize
timeout /t 3 /nobreak >nul
call :qairt_try_7za 3
if "%QAIRT_EXTRACT_OK%"=="1" goto :qairt_extract_finalize

:qairt_extract_psh
echo [INFO] Falling back to PowerShell Expand-Archive ^(slow -- may take 15-40 min^)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; " ^
    "$zip = '%QAIRT_VENDOR_ZIP%'; $tmp = '%QAIRT_EXTRACT_TMP%'; " ^
    "$ok = $false; " ^
    "for ($i = 1; $i -le 3 -and -not $ok; $i++) { " ^
    "  try { Write-Host ('[INFO] Extracting... (attempt ' + $i + '/3, Expand-Archive)'); if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }; Expand-Archive -Path $zip -DestinationPath $tmp -Force; $ok = $true } " ^
    "  catch { Write-Host ('[WARN] Extract attempt ' + $i + ' failed: ' + $_.Exception.Message); if ($i -lt 3) { Start-Sleep -Seconds 3 } } " ^
    "}; " ^
    "if (-not $ok) { Write-Host '[WARN] Expand-Archive failed after 3 attempts (zip still locked?).'; exit 1 }"
if errorlevel 1 goto :qairt_extract_failed
set "QAIRT_EXTRACT_OK=1"

:qairt_extract_finalize
REM --- Move version subfolder into final location ---
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$tmp = '%QAIRT_EXTRACT_TMP%'; " ^
    "$src = Get-ChildItem $tmp -Recurse -Directory | Where-Object { $_.Name -eq '%QAIRT_VERSION%' } | Select-Object -First 1; " ^
    "if ($src) { if (Test-Path '%QAIRT_SDK_ROOT%') { Remove-Item '%QAIRT_SDK_ROOT%' -Recurse -Force }; Move-Item $src.FullName '%QAIRT_SDK_ROOT%' -Force; Write-Host '[INFO] Moved to %QAIRT_SDK_ROOT%' } " ^
    "else { Write-Host '[WARN] Version subfolder not found, leaving content in %QAIRT_EXTRACT_TMP%' }; " ^
    "if (Test-Path '%QAIRT_EXTRACT_TMP%') { Remove-Item '%QAIRT_EXTRACT_TMP%' -Recurse -Force }; " ^
    "Write-Host '[INFO] Extraction complete.'"
goto :qairt_extract_done

:qairt_extract_failed
echo [WARN] All extraction attempts failed.

:qairt_extract_done
if exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    echo [OK]   QAIRT SDK installed: %QAIRT_SDK_ROOT%
) else (
    echo [WARN] QAIRT SDK extracted but expected tools not found at %QAIRT_SDK_ROOT%
)
goto :sb_keep_qairt_zip

:sb_keep_qairt_zip
REM ------------------------------------------------------------------
REM KEEP the original QAIRT SDK .zip so any later corruption (a stray
REM write truncating an SDK file, a model editing an SDK launcher script,
REM a damaged binary) can be repaired by re-extracting the single affected
REM file from the archive -- WITHOUT re-downloading ~1.7 GB. User decision
REM (2026-06-20): keep the zip; do NOT mirror the whole bin/lib tree.
REM
REM Two zip sources:
REM   * Setup-downloaded temp zip (data\downloads\_qairt_tmp.zip): MOVE it to
REM     the stable kept location data\sdk\qairt\v<version>.zip (it would
REM     otherwise be deleted as a temp file).
REM   * vendor-preplaced zip (vendor\qairt\v<version>.zip): already a stable
REM     kept copy -- leave it where it is.
REM Only keep the zip when the SDK actually extracted (else a bad/partial zip
REM would be preserved as if good). Best-effort: never fails Setup.
set "QAIRT_KEPT_ZIP=%ROOT_DIR%data\sdk\qairt\v%QAIRT_VERSION%.zip"
if not exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    echo [INFO] QAIRT SDK not present; not keeping zip.
    goto :sb_backup_qairt_scripts
)
if "%QAIRT_VENDOR_ZIP%"=="%QAIRT_ZIP%" (
    REM Setup's own download -> move it to the kept location (do NOT delete).
    if exist "%QAIRT_ZIP%" (
        if exist "%QAIRT_KEPT_ZIP%" (
            echo [SKIP] QAIRT SDK zip already kept: %QAIRT_KEPT_ZIP%
            del /f /q "%QAIRT_ZIP%" 2>nul
        ) else (
            if not exist "%ROOT_DIR%data\sdk\qairt" mkdir "%ROOT_DIR%data\sdk\qairt" 2>nul
            move /y "%QAIRT_ZIP%" "%QAIRT_KEPT_ZIP%" >nul 2>&1
            if exist "%QAIRT_KEPT_ZIP%" (
                echo [OK]   Kept QAIRT SDK zip for repair: %QAIRT_KEPT_ZIP%
            ) else (
                echo [WARN] Could not move QAIRT zip to %QAIRT_KEPT_ZIP%; leaving it in %DL_DIR%.
            )
        )
    )
) else (
    REM Not a fresh Setup download -- SDK was already installed, or a vendor
    REM zip was used. Report the actual kept-zip situation by checking what
    REM really exists on disk, NOT the stale QAIRT_VENDOR_ZIP value which on the
    REM "SDK already installed" skip path still holds the default vendor name
    REM regardless of whether that file exists.
    if exist "%QAIRT_KEPT_ZIP%" (
        echo [SKIP] QAIRT SDK repair zip already present: %QAIRT_KEPT_ZIP%
    ) else (
        if exist "%QAIRT_VENDOR_ZIP%" (
            echo [INFO] Using vendor-preplaced QAIRT zip ^(kept^): %QAIRT_VENDOR_ZIP%
        ) else (
            echo [INFO] No kept QAIRT SDK zip found. To enable single-file repair
            echo [INFO] of the SDK, keep v%QAIRT_VERSION%.zip in vendor\qairt\ or
            echo [INFO] re-run Setup.bat after deleting the SDK to re-download it.
        )
    )
)

:sb_backup_qairt_scripts
REM ------------------------------------------------------------------
REM FINAL INSTALL VERDICT: before the (best-effort) script backup, assert the
REM pinned SDK actually landed on disk. If the download succeeded but the
REM extract/move failed (or the zip was incomplete), the target tools are
REM absent -> return NON-ZERO so the caller aborts Setup instead of silently
REM continuing with a missing / mismatched SDK. We do NOT fall back to another
REM installed QAIRT version: it may be incompatible with this toolchain.
if not exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    echo [ERROR] QAIRT SDK %QAIRT_VERSION% is not usable after install: expected
    echo [ERROR] tool missing at %QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe
    exit /b 1
)
REM ------------------------------------------------------------------
REM Back up the SDK's launcher SCRIPTS (the no-extension Python entry points
REM under bin\, e.g. qnn-onnx-converter / qairt-converter / qairt-quantizer /
REM qnn-model-lib-generator) AND critical binaries that are prone to accidental
REM corruption (e.g. qnn-context-binary-generator.exe — real incident 2026-06-16:
REM overwritten to 0 bytes by a misdirected command output).
REM
REM Rationale (user 2026-06-20, extended 2026-06-22):
REM   * The model (agent) may EDIT launcher scripts -- so they need a fast
REM     file-level restore source.
REM   * qnn-context-binary-generator.exe is easily corrupted by accidental
REM     output redirection (CWD landing inside SDK, relative-path > redirect).
REM     Keeping a ~4 MB backup avoids a ~2 GB SDK reinstall.
REM   * Other binaries (.dll/.so/.cat) and lib\python are NOT backed up here --
REM     those are repaired from the kept zip instead.
REM Backup target: data\sdk\qairt-scripts\<arch>\<name> (OUTSIDE C:\Qualcomm).
REM Skipped per-file when already backed up (dedupe: first copy is good).
REM Best-effort: a failure NEVER fails Setup.
set "QAIRT_SCRIPT_BACKUP=%ROOT_DIR%data\sdk\qairt-scripts"
if not exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc" (
    echo [INFO] QAIRT SDK bin not present; skipping script backup.
    exit /b 0
)
echo [INFO] Backing up QAIRT SDK launcher scripts to %QAIRT_SCRIPT_BACKUP% ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='SilentlyContinue'; " ^
    "$root = '%QAIRT_SDK_ROOT%'; $dst = '%QAIRT_SCRIPT_BACKUP%'; $n = 0; " ^
    "foreach ($arch in @('aarch64-windows-msvc','x86_64-windows-msvc')) { " ^
    "  $binDir = Join-Path (Join-Path $root 'bin') $arch; " ^
    "  if (-not (Test-Path $binDir)) { continue }; " ^
    "  $outDir = Join-Path $dst $arch; New-Item -ItemType Directory -Force -Path $outDir ^| Out-Null; " ^
    "  Get-ChildItem $binDir -File ^| Where-Object { $_.Extension -eq '' } ^| ForEach-Object { " ^
    "    $target = Join-Path $outDir $_.Name; " ^
    "    if (-not (Test-Path $target)) { Copy-Item $_.FullName $target -Force; $n++ } " ^
    "  } " ^
    "}; " ^
    "Write-Host ('[OK]   QAIRT SDK launcher scripts backed up (' + $n + ' new file(s)) at ' + $dst)"
REM ------------------------------------------------------------------
REM Additionally back up qnn-context-binary-generator.exe (critical binary,
REM prone to 0-byte corruption via misdirected output redirection).
REM Only from aarch64-windows-msvc (the only arch that has the generator).
set "CTX_GEN_SRC=%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe"
set "CTX_GEN_DST=%QAIRT_SCRIPT_BACKUP%\aarch64-windows-msvc\qnn-context-binary-generator.exe"
if not exist "%CTX_GEN_SRC%" (
    echo [INFO] qnn-context-binary-generator.exe not found in SDK; skipping.
    exit /b 0
)
if exist "%CTX_GEN_DST%" (
    echo [INFO] qnn-context-binary-generator.exe backup already exists; skipping.
    exit /b 0
)
REM Verify source is healthy (non-zero size) before backing up
for %%F in ("%CTX_GEN_SRC%") do set CTX_GEN_SIZE=%%~zF
if "%CTX_GEN_SIZE%"=="0" (
    echo [WARN] qnn-context-binary-generator.exe is 0 bytes ^(already corrupt^); skipping backup.
    exit /b 0
)
mkdir "%QAIRT_SCRIPT_BACKUP%\aarch64-windows-msvc" 2>nul
copy /Y "%CTX_GEN_SRC%" "%CTX_GEN_DST%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Failed to backup qnn-context-binary-generator.exe; non-fatal.
) else (
    echo [OK]   qnn-context-binary-generator.exe backed up ^(%CTX_GEN_SIZE% bytes^).
)
exit /b 0


REM ---------------------------------------------------------------------------
REM  Subroutine: try one 7za.exe extraction attempt for the QAIRT SDK zip.
REM  Args: %1 = attempt number (used in the log line only).
REM  Reads:  %QAIRT_SEVEN_ZIP%      (path to data\bin\7zr\7za.exe)
REM          %QAIRT_VENDOR_ZIP%     (path to the QAIRT zip on disk)
REM          %QAIRT_EXTRACT_TMP%    (temp extraction dir)
REM  Writes: %QAIRT_EXTRACT_OK%=1 on success (left at 0 on failure).
REM
REM  Behaviour: wipes %QAIRT_EXTRACT_TMP% first (so a prior partial extract
REM  cannot leave stale files), then runs:
REM      7za.exe x -y -o<tmp> <zip> -bso1 -bsp1 -bse2
REM  -bsp1 is the key flag -- it streams a LIVE progress line to stdout
REM  (percentage + current file), so the user sees concrete activity instead
REM  of a frozen-looking screen. ``-bso1`` keeps the per-file log line on
REM  stdout, ``-bse2`` keeps real errors on stderr.
REM
REM  Implementation note: we use ``goto :qairt_try_7za_fail`` (not an
REM  ``if errorlevel 1 ( ... %errorlevel% ... )`` parens block) to bail out,
REM  because cmd.exe expands %errorlevel% at block-entry time (giving stale
REM  0). The simpler form keeps the real exit code visible AND avoids the
REM  parens-block delayed-expansion pitfall.
REM ---------------------------------------------------------------------------
:qairt_try_7za
echo [INFO] Extracting... ^(attempt %~1/3, 7za.exe^)
if exist "%QAIRT_EXTRACT_TMP%" rmdir /s /q "%QAIRT_EXTRACT_TMP%" 2>nul
mkdir "%QAIRT_EXTRACT_TMP%" 2>nul
"%QAIRT_SEVEN_ZIP%" x -y "-o%QAIRT_EXTRACT_TMP%" "%QAIRT_VENDOR_ZIP%" -bso1 -bsp1 -bse2
if errorlevel 1 goto :qairt_try_7za_fail
set "QAIRT_EXTRACT_OK=1"
exit /b 0
:qairt_try_7za_fail
echo [WARN] 7za.exe extract attempt %~1 failed ^(exit code %errorlevel%^).
exit /b 1


REM ---------------------------------------------------------------------------
REM  Subroutine: ensure data\bin\7zr\7za.exe is available.
REM
REM  Why a separate full 7-Zip build (7za) on top of the bundled 7zr.exe?
REM    * 7zr.exe is the "7z-format-only" reduced standalone (~600 KB) -- it
REM      handles .7z archives only and rejects .zip input with
REM      "Cannot open the file as archive" (exit 2). Sufficient for
REM      PortableGit (a 7z-format SFX) and vendor-deps.7z, but NOT for
REM      the QAIRT SDK .zip.
REM    * 7za.exe is the FULL standalone console build (~1 MB) shipped in
REM      7-Zip's "Extra" package (a .7z file). Console-only, NO installer,
REM      NO GUI -- it's just a single .exe you run from the command line.
REM      Supports zip / 7z / tar / gz / bz2 / xz / wim / iso / cab / etc.
REM
REM  Source: https://www.7-zip.org/a/7z2301-extra.7z (7-Zip 23.01 Extra,
REM  the long-stable 2023 release; URL is permanent on 7-zip.org). The
REM  archive is ~530 KB. We pin to 23.01 instead of the latest "26.01" so
REM  the URL never changes when 7-Zip releases a new version.
REM
REM  Strategy: download once into data\downloads\_7za_extra.7z, extract
REM  with our existing data\bin\7zr\7zr.exe (which CAN open .7z), copy
REM  7za.exe out of the extracted tree into data\bin\7zr\7za.exe, then
REM  delete the temp archive + extract dir. Idempotent: if 7za.exe is
REM  already present this is a no-op.
REM
REM  Best-effort: a failure leaves QAIRT_SEVEN_ZIP pointing at a missing
REM  file, which the caller's ``if not exist ... goto :qairt_extract_psh``
REM  uses to fall back to Expand-Archive. Setup is never aborted here.
REM ---------------------------------------------------------------------------
:ensure_7za
if exist "data\bin\7zr\7za.exe" (
    REM Already cached from a prior run; nothing to do.
    exit /b 0
)
if not exist "data\bin\7zr\7zr.exe" (
    REM 7zr.exe is normally guaranteed by Step 5 (PortableGit) and Step 0b
    REM (vendor-deps), both of which check for it unconditionally before their
    REM own [SKIP] guards. This branch is a last-resort safety net for any
    REM edge case where both earlier steps were bypassed (e.g. --no-builder
    REM combined with a manual data\bin\ wipe). Try to download it now.
    echo [INFO] 7zr.exe not found; attempting to download it now ^(~600 KB^)...
    if not exist "data\bin\7zr" mkdir "data\bin\7zr"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
        -Url "https://www.7-zip.org/a/7zr.exe" ^
        -OutFile "data\bin\7zr\7zr.exe" ^
        -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
        -MinSize 100000 ^
        -MaxRetries 3 ^
        -StallTimeoutSec 30 ^
        -AttemptTimeoutSec 60
    if exist "data\bin\7zr\7zr.exe" (
        echo [OK]   7zr.exe downloaded: data\bin\7zr\7zr.exe
    ) else (
        echo [WARN] Could not download 7zr.exe; cannot bootstrap 7za.exe ^(falls back to Expand-Archive^).
        exit /b 0
    )
)
echo [INFO] Bootstrapping 7za.exe ^(full 7-Zip console, needed for .zip^) ...
set "EXTRA_URL=https://www.7-zip.org/a/7z2301-extra.7z"
set "EXTRA_ARCHIVE=%DL_DIR%\_7za_extra.7z"
set "EXTRA_TMP=%DL_DIR%\_7za_extra_tmp"

if not exist "%EXTRA_ARCHIVE%" (
    echo [INFO] Downloading 7-Zip Extra ^(~530 KB^) from 7-zip.org ...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\download_with_aria2c.ps1" ^
        -Url "%EXTRA_URL%" ^
        -OutFile "%EXTRA_ARCHIVE%" ^
        -Aria2cExe "data\bin\aria2c\aria2c.exe" ^
        -MinSize 300000 ^
        -MaxRetries 3 ^
        -StallTimeoutSec 30 ^
        -AttemptTimeoutSec 60
)
if not exist "%EXTRA_ARCHIVE%" (
    echo [WARN] Could not download 7-Zip Extra; QAIRT extraction will use Expand-Archive ^(slower^).
    exit /b 0
)

REM Use the bundled 7zr.exe to extract the .7z (7zr handles .7z natively).
if exist "%EXTRA_TMP%" rmdir /s /q "%EXTRA_TMP%" 2>nul
mkdir "%EXTRA_TMP%" 2>nul
"data\bin\7zr\7zr.exe" x -y "-o%EXTRA_TMP%" "%EXTRA_ARCHIVE%" -bso0 -bsp0 >nul
if errorlevel 1 (
    echo [WARN] 7zr.exe failed to extract %EXTRA_ARCHIVE%.
    rmdir /s /q "%EXTRA_TMP%" 2>nul
    del /f /q "%EXTRA_ARCHIVE%" 2>nul
    exit /b 0
)

REM Copy 7za.exe out of the extracted tree (it lives at the root of Extra).
if exist "%EXTRA_TMP%\7za.exe" (
    copy /y "%EXTRA_TMP%\7za.exe" "data\bin\7zr\7za.exe" >nul
)
if not exist "data\bin\7zr\7za.exe" (
    REM Some Extra archive layouts place 7za.exe in a subdir; do a recursive find.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "$src = Get-ChildItem -Path '%EXTRA_TMP%' -Recurse -Filter '7za.exe' -File | Select-Object -First 1; if ($src) { Copy-Item $src.FullName 'data\bin\7zr\7za.exe' -Force }"
)

REM Cleanup temp extract dir + archive (we keep only 7za.exe).
rmdir /s /q "%EXTRA_TMP%" 2>nul
del /f /q "%EXTRA_ARCHIVE%" 2>nul

if exist "data\bin\7zr\7za.exe" (
    echo [OK]   7za.exe ready: data\bin\7zr\7za.exe
) else (
    echo [WARN] 7za.exe not found after extraction; QAIRT extraction will use Expand-Archive ^(slower^).
)
exit /b 0


