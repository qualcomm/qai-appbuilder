@echo off

REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------

REM ===========================================================================
REM   QAI ModelBuilder v2 -- Uninstaller (Clean Cutover)
REM
REM   Thin wrapper that delegates to ``scripts/init/uninstall.py``.
REM   Rolls back ONLY what Setup.bat installed OUTSIDE the project directory:
REM     1. Stop running services
REM     2. Delete venvs + PortableGit + Node.js (%LOCALAPPDATA%\QAIModelBuilder\)
REM     3. Delete temp files (%TEMP%\QAIModelBuilder\)
REM     4. Clean install temp archives in data\downloads\ (whitelist)
REM     5. Remove data\bin (uv / aria2c / 7zr Setup-installed tooling)
REM     6. Remove uv-managed Python interpreters (%APPDATA%\uv\python\)
REM
REM   Note: previous versions also cleaned AppContainer sandbox ACEs and
REM   orphaned AppContainer registry profiles. The Windows AppContainer /
REM   LPAC sandbox launcher chain has been deleted (Phase 3 cleanup,
REM   2026-07-01), so those stages were removed in Phase 8. Any residual
REM   ACEs left over from older installs are harmless and can be cleaned
REM   manually via ``icacls`` if desired.
REM
REM   Usage:
REM     Uninstall.bat                Interactive default uninstall
REM     Uninstall.bat --help / -h    Show full help (lists every option)
REM     Uninstall.bat --yes / -y     Non-interactive default uninstall
REM     Uninstall.bat --quiet        Same as --yes
REM     Uninstall.bat --clean-uv     Default + uv package cache (SHARED)
REM     Uninstall.bat --all          FULL uninstall: default + uv cache +
REM                                  QAIRT SDK + Playwright Chromium +
REM                                  vendor caches + %TEMP%\jieba.cache.
REM                                  Implies --yes. Does NOT touch VS or Rust.
REM     Uninstall.bat --vs           ALSO uninstall Visual Studio 2022 via the
REM                                  VS Installer. Interactive YES gate unless
REM                                  combined with --yes. NOT implied by --all
REM                                  (VS is shared with other projects).
REM     Uninstall.bat --desktop      ALSO remove tauri-cli (Setup Step 5c
REM                                  `cargo install` artifact). Project-
REM                                  specific and safe; does NOT touch the
REM                                  Rust toolchain.
REM     Uninstall.bat --desktop-rust ALSO uninstall the Rust toolchain
REM                                  (rustup + ~/.cargo + ~/.rustup).
REM                                  Interactive YES gate unless --yes.
REM                                  SHARED with every other Rust project.
REM                                  Implies --desktop. NOT implied by --all.
REM     Uninstall.bat --all --vs     Everything: --all PLUS uninstall VS 2022.
REM
REM   IMPORTANT: This script does NOT touch the project directory or its
REM   data/ folder (qai.db, logs, sandboxes, downloads, config) -- that is
REM   USER DATA and is left fully intact. Delete the project folder yourself
REM   only if you want to remove the application entirely.
REM ===========================================================================

setlocal EnableDelayedExpansion

REM -- Early-out for --help / -h / /? (no Python needed) ----------------------
REM Scan args before doing anything else; help should be instant regardless
REM of whether Python / the venv exist.
for %%a in (%*) do (
    if /i "%%~a"=="--help" goto :print_help
    if /i "%%~a"=="-h"     goto :print_help
    if /i "%%~a"=="/?"     goto :print_help
)

echo.
echo  +--------------------------------------------------+
echo  ^|   QAI ModelBuilder v2 - Uninstaller             ^|
echo  +--------------------------------------------------+
echo  ^|   Removes what Setup.bat installed OUTSIDE the  ^|
echo  ^|   project (venvs, PortableGit, temp). Your      ^|
echo  ^|   project folder and data/ (qai.db, logs,       ^|
echo  ^|   config) are left untouched.                   ^|
echo  +--------------------------------------------------+
echo.

REM -- Locate Python -----------------------------------------------------------
REM Lookup order (most-likely-to-be-present first):
REM   1. The ARM64 venv python (gone after default uninstall though).
REM   2. The uv-managed cpython interpreter Setup.bat installed
REM      (%APPDATA%\uv\python\cpython-3.13-windows-aarch64-none\python.exe).
REM      This survives a default uninstall (only the venv link inside it is
REM      removed), so a follow-up `Uninstall.bat --all` / `--vs` can still
REM      use Python to drive the heavier cleanup.
REM   3. System python on PATH.
REM   4. None of the above -> fall through to :manual_cleanup, which now
REM      handles --all and --vs in pure batch / PowerShell.

set "LOCALAPP_QAI=%LOCALAPPDATA%\QAIModelBuilder"
set "VENV_PYTHON=%LOCALAPP_QAI%\envs\.venv_arm64_313\Scripts\python.exe"
set "UV_PYTHON_ROOT=%APPDATA%\uv\python"
set "UV_CPY313=%UV_PYTHON_ROOT%\cpython-3.13-windows-aarch64-none\python.exe"
set "PYTHON="
set "PYTHON_IN_TREE="

if exist "%VENV_PYTHON%" (
    set "PYTHON=%VENV_PYTHON%"
    REM The interpreter lives INSIDE the tree we are about to delete. A
    REM running EXE image is locked on Windows, so uninstall.py cannot
    REM rmtree it from within (WinError 5). We tell Python to SKIP the
    REM LOCALAPPDATA tree and delete it ourselves below, after Python exits.
    set "PYTHON_IN_TREE=1"
    goto :python_ready
)
if exist "%UV_CPY313%" (
    set "PYTHON=%UV_CPY313%"
    goto :python_ready
)
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON=python"
    goto :python_ready
)

if not defined PYTHON (
    echo  [WARN] Python not found ^(venv removed and uv-managed cpython absent^).
    echo         Falling back to pure-batch cleanup.
    echo.
    goto :manual_cleanup
)

:python_ready

REM -- Parse arguments and delegate to Python ----------------------------------
REM Six orthogonal flags:
REM   --yes / -y / --quiet : skip interactive confirmation prompt
REM   --clean-uv           : also remove uv package cache (SHARED resource)
REM   --all                : FULL uninstall (uv cache + QAIRT + Playwright +
REM                          vendor caches + jieba.cache); also implies --yes
REM                          (used non-interactively in the all-in handler).
REM   --vs                 : ALSO uninstall Visual Studio 2022 Community via
REM                          the VS Installer. WARNING: VS is a general-purpose
REM                          IDE; other projects on this machine may rely on
REM                          it. Interactive YES confirmation required unless
REM                          combined with --yes. NOT implied by --all.
REM   --desktop            : ALSO remove tauri-cli (project-specific cargo
REM                          subcommand Setup Step 5c installs). Safe; does
REM                          NOT touch the Rust toolchain itself.
REM   --desktop-rust       : ALSO uninstall the Rust toolchain (rustup +
REM                          ~/.cargo + ~/.rustup). WARNING: SHARED with any
REM                          other Rust project. Interactive YES gate unless
REM                          --yes. Implies --desktop. NOT implied by --all.

set "PY_ARGS="
set "PY_HAS_YES="
set "PY_HAS_CLEAN_UV="
set "PY_HAS_ALL="
set "PY_HAS_VS="
set "PY_HAS_DESKTOP="
set "PY_HAS_DESKTOP_RUST="
for %%a in (%*) do (
    if /i "%%~a"=="--yes"           set "PY_HAS_YES=1"
    if /i "%%~a"=="-y"              set "PY_HAS_YES=1"
    if /i "%%~a"=="--quiet"         set "PY_HAS_YES=1"
    if /i "%%~a"=="--clean-uv"      set "PY_HAS_CLEAN_UV=1"
    if /i "%%~a"=="--all"           set "PY_HAS_ALL=1"
    if /i "%%~a"=="--vs"            set "PY_HAS_VS=1"
    if /i "%%~a"=="--desktop"       set "PY_HAS_DESKTOP=1"
    if /i "%%~a"=="--desktop-rust"  set "PY_HAS_DESKTOP_RUST=1"
)
REM --all implies non-interactive (matches v1 expectation that it removes
REM everything without prompting). --vs / --desktop-rust do NOT imply --yes:
REM each has its own irreversible YES gate that --yes is the right way to
REM skip globally.
if defined PY_HAS_ALL set "PY_HAS_YES=1"
REM --desktop-rust implies --desktop (the Python stage already enforces this,
REM but we set both here so the help/preview text in the Python script lists
REM both extras).
if defined PY_HAS_DESKTOP_RUST set "PY_HAS_DESKTOP=1"

if defined PY_HAS_YES          set "PY_ARGS=!PY_ARGS! --yes"
if defined PY_HAS_CLEAN_UV     set "PY_ARGS=!PY_ARGS! --clean-uv"
if defined PY_HAS_ALL          set "PY_ARGS=!PY_ARGS! --all"
if defined PY_HAS_VS           set "PY_ARGS=!PY_ARGS! --vs"
if defined PY_HAS_DESKTOP      set "PY_ARGS=!PY_ARGS! --desktop"
if defined PY_HAS_DESKTOP_RUST set "PY_ARGS=!PY_ARGS! --desktop-rust"

REM When running from the venv interpreter, defer the LOCALAPPDATA tree
REM removal to this .bat (after Python exits) to avoid the self-lock.
if defined PYTHON_IN_TREE set "PY_ARGS=!PY_ARGS! --skip-localappdata"

set "SCRIPT_DIR=%~dp0"
REM Strip trailing backslash
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"

echo  [INFO] Running uninstall.py via: %PYTHON%
echo.

"%PYTHON%" -m scripts.init.uninstall %PY_ARGS% --repo-root "!SCRIPT_DIR!"
set "PY_EXIT=%errorlevel%"

REM -- Post-exit: remove the venv tree the in-tree interpreter couldn't --------
REM Python has now exited, so the venv python.exe is no longer locked. Only
REM do this when (a) we ran the in-tree interpreter AND (b) Python completed
REM successfully (exit 0) -- never on user-cancel (exit 1) or error (exit 2).
if defined PYTHON_IN_TREE if "!PY_EXIT!"=="0" (
    if exist "%LOCALAPP_QAI%" (
        echo  [INFO] Removing venvs tree post-exit: %LOCALAPP_QAI%
        rd /s /q "%LOCALAPP_QAI%" 2>nul
        if exist "%LOCALAPP_QAI%" (
            echo  [WARN] Could not fully remove %LOCALAPP_QAI%.
            echo         Some files may still be locked; reboot and delete it manually.
        ) else (
            echo  [OK]   Venvs tree removed.
        )
    )
)

if "!PY_EXIT!"=="0" (
    echo.
    echo  +--------------------------------------------------+
    echo  ^|   Uninstallation complete.                      ^|
    echo  +--------------------------------------------------+
    echo.
    echo  Removed what Setup.bat installed OUTSIDE the project: venvs,
    echo  PortableGit, temp files. The project directory -- including
    echo  your data\ -- is left fully intact.
    echo.
) else if "!PY_EXIT!"=="1" (
    echo.
    echo  Uninstallation cancelled by user.
    echo.
) else (
    echo.
    echo  [WARN] Uninstaller exited with code !PY_EXIT!.
    echo.
)

goto :done

REM ===========================================================================
REM  Manual cleanup fallback (when Python is unavailable)
REM
REM  Mirrors uninstall.py's behaviour using only batch + PowerShell so that
REM  --all and --vs still work after a previous default uninstall has wiped
REM  the venv (and the system has no other Python).
REM ===========================================================================
:manual_cleanup

echo -- Manual fallback: stopping processes ------------------------------------
REM The API server's bound port is dynamic (see apps/cli/serve.py
REM FALLBACK_PORTS) — Hyper-V / WSL2 can reserve the documented default
REM 8989 inside a Windows excluded port range, forcing the supervisor to
REM fall back to another candidate. Read the runtime endpoint URL the API
REM wrote on startup and POST to its actual /api/system/reboot path; if
REM the file is missing (server already stopped or pre-endpoint-file
REM install) fall back to localhost:8989. Both paths are best-effort —
REM curl errors are silently swallowed and the kill-by-image-path step
REM below catches anything that did not stop gracefully.
set "REBOOT_URL=http://localhost:8989/api/system/reboot"
if exist "%~dp0data\runtime\server.endpoint.json" (
    for /f "usebackq delims=" %%U in (`powershell -NoProfile -Command "try { (Get-Content '%~dp0data\runtime\server.endpoint.json' -Raw -Encoding UTF8 | ConvertFrom-Json).url } catch { '' }"`) do (
        if not "%%U"=="" set "REBOOT_URL=%%U/api/system/reboot"
    )
)
curl -s -X POST "!REBOOT_URL!" >nul 2>&1
timeout /t 3 /nobreak >nul
for /f "tokens=2" %%p in ('wmic process where "ExecutablePath like '%%%LOCALAPPDATA:\=\\%%%\\QAIModelBuilder%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /f /pid %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

REM NOTE: we deliberately do NOT touch %~dp0data\ files outside data\bin\ or
REM data\downloads\ install temp archives. data\ otherwise holds USER DATA
REM (qai.db, logs, runtime model weights, config) and must survive.

echo -- Manual fallback: removing venvs ----------------------------------------
if exist "%LOCALAPPDATA%\QAIModelBuilder" (
    rd /s /q "%LOCALAPPDATA%\QAIModelBuilder" 2>nul
    echo    [OK] Venvs removed.
) else (
    echo    [SKIP] Not present.
)

echo -- Manual fallback: removing temp files -----------------------------------
if exist "%TEMP%\QAIModelBuilder" (
    rd /s /q "%TEMP%\QAIModelBuilder" 2>nul
    echo    [OK] Temp files removed.
) else (
    echo    [SKIP] Not present.
)

echo -- Manual fallback: removing data\bin (uv / aria2c / 7zr) -----------------
if exist "%~dp0data\bin" (
    rd /s /q "%~dp0data\bin" 2>nul
    echo    [OK] data\bin removed.
) else (
    echo    [SKIP] Not present.
)

echo -- Manual fallback: removing install temp archives in data\downloads ------
REM Whitelist (mirror clean_install_temp_downloads in uninstall.py):
REM   PortableGit-*.7z.exe, node-v*-win-arm64.zip, _uv_tmp.zip, _qairt_tmp.zip,
REM   vendor-deps.7z, _aria2c_tmp.zip (and their .aria2 control files);
REM   plus subdirs: log/, _aria2c_tmp/, _node_tmp/, _vendor_deps_tmp/.
if exist "%~dp0data\downloads" (
    pushd "%~dp0data\downloads" >nul
    for %%P in (PortableGit-*.7z.exe PortableGit-*.7z.exe.aria2 ^
                node-v*-win-arm64.zip node-v*-win-arm64.zip.aria2 ^
                _uv_tmp.zip _uv_tmp.zip.aria2 ^
                _qairt_tmp.zip _qairt_tmp.zip.aria2 ^
                vendor-deps.7z vendor-deps.7z.aria2 ^
                _aria2c_tmp.zip _aria2c_tmp.zip.aria2) do (
        if exist "%%P" del /f /q "%%P" >nul 2>&1
    )
    for %%D in (log _aria2c_tmp _node_tmp _vendor_deps_tmp) do (
        if exist "%%D" rd /s /q "%%D" >nul 2>&1
    )
    popd >nul
    echo    [OK] install temp archives whitelisted-cleaned.
) else (
    echo    [SKIP] data\downloads not present.
)

echo -- Manual fallback: removing uv-managed Python interpreters ---------------
REM uv places Python interpreters under %APPDATA%\uv\python\ as directory
REM SYMLINKS or JUNCTIONS pointing into uv's cache. PowerShell's
REM Remove-Item -Force unlinks the link without recursing into the target,
REM preserving uv cache integrity.
if exist "%APPDATA%\uv\python" (
    powershell -NoProfile -Command "$root = '%APPDATA%\uv\python'; $patterns = @('cpython-3.13-windows-aarch64*','cpython-3.13.*-windows-aarch64*','cpython-3.10-windows-x86_64*','cpython-3.10.*-windows-x86_64*'); $count = 0; foreach ($p in $patterns) { Get-ChildItem -LiteralPath $root -Directory -Filter $p -ErrorAction SilentlyContinue | ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Force -Recurse -ErrorAction Stop; $count++ } catch { Write-Host ('   [WARN] could not remove ' + $_.FullName + ': ' + $_.Exception.Message) } } }; Write-Host ('   [OK] removed ' + $count + ' uv python interpreter(s).')"
) else (
    echo    [SKIP] %APPDATA%\uv\python not present.
)

REM ---- --all extras (manual fallback) ---------------------------------------
if not defined PY_HAS_ALL goto :mc_after_all

echo.
echo -- Manual fallback (--all): uv package cache ------------------------------
REM Try `uv cache clean` first if uv is on PATH; otherwise rmtree the cache dir.
where uv >nul 2>&1
if %errorlevel% equ 0 (
    uv cache clean >nul 2>&1
    if errorlevel 1 (
        if exist "%LOCALAPPDATA%\uv\cache" rd /s /q "%LOCALAPPDATA%\uv\cache" 2>nul
    )
    echo    [OK] uv cache cleaned.
) else if exist "%LOCALAPPDATA%\uv\cache" (
    rd /s /q "%LOCALAPPDATA%\uv\cache" 2>nul
    echo    [OK] uv cache removed.
) else (
    echo    [SKIP] uv cache not present.
)

echo -- Manual fallback (--all): QAIRT SDK -------------------------------------
REM Config file is the ONLY source of truth. Any pre-set env vars are ignored:
REM   scripts\qairt_release.json  (field "qairt_version")
REM Abort if it can't be read — refuse to guess which SDK to remove.
set "QAIRT_VERSION="
set "QAIRT_SDK_ROOT="
if not exist "%SCRIPT_DIR%\scripts\qairt_release.json" (
    echo    [ERROR] scripts\qairt_release.json not found.
    echo    [ERROR] This file is the single source of truth for the QAIRT SDK version.
    exit /b 1
)
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Get-Content -Raw -LiteralPath '%SCRIPT_DIR%\scripts\qairt_release.json' | ConvertFrom-Json).qairt_version } catch { exit 1 }" 2^>nul`) do set "QAIRT_VERSION=%%V"
if not defined QAIRT_VERSION (
    echo    [ERROR] Could not read qairt_version from scripts\qairt_release.json
    echo    [ERROR] Fix that file ^(must contain a "qairt_version" field^) and re-run Uninstall.bat --all.
    exit /b 1
)
set "QAIRT_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\%QAIRT_VERSION%"
if exist "%QAIRT_SDK_ROOT%" (
    rd /s /q "%QAIRT_SDK_ROOT%" 2>nul
    if exist "%QAIRT_SDK_ROOT%" (
        echo    [WARN] %QAIRT_SDK_ROOT% partially removed; some files may be locked.
    ) else (
        echo    [OK] QAIRT SDK %QAIRT_VERSION% removed.
        REM Walk up and prune empty Qualcomm parents. `rd` (no /s) only
        REM succeeds on empty dirs, so a sibling SDK / version aborts the
        REM walk safely. Order: QAIRT -> AIStack -> Qualcomm.
        for %%P in ("C:\Qualcomm\AIStack\QAIRT" "C:\Qualcomm\AIStack" "C:\Qualcomm") do (
            if exist %%P (
                rd %%P 2>nul && echo    [OK] removed empty parent %%P
            )
        )
    )
) else (
    echo    [SKIP] QAIRT SDK not present at %QAIRT_SDK_ROOT%.
)

echo -- Manual fallback (--all): Playwright Chromium cache ---------------------
if exist "%LOCALAPPDATA%\ms-playwright" (
    rd /s /q "%LOCALAPPDATA%\ms-playwright" 2>nul
    echo    [OK] Playwright Chromium cache removed.
) else (
    echo    [SKIP] Playwright Chromium cache not present.
)

echo -- Manual fallback (--all): vendor/ runtime caches ------------------------
if exist "%~dp0vendor" (
    for %%S in (nltk_data g2p_data whl tiktoken) do (
        if exist "%~dp0vendor\%%S" (
            rd /s /q "%~dp0vendor\%%S" 2>nul
            echo    [OK] vendor\%%S removed.
        )
    )
    REM Recursive __pycache__ cleanup under vendor/ via PowerShell.
    powershell -NoProfile -Command "$root = '%~dp0vendor'; $count = 0; Get-ChildItem -LiteralPath $root -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop; $count++ } catch {} }; if ($count -gt 0) { Write-Host ('   [OK] removed ' + $count + ' __pycache__ dir(s) under vendor/.') }"
) else (
    echo    [SKIP] vendor/ not present.
)

echo -- Manual fallback (--all): %%TEMP%%\jieba.cache --------------------------
if exist "%TEMP%\jieba.cache" (
    del /f /q "%TEMP%\jieba.cache" 2>nul
    echo    [OK] %TEMP%\jieba.cache removed.
) else (
    echo    [SKIP] jieba.cache not present.
)

:mc_after_all

REM ---- --desktop / --desktop-rust extras (manual fallback) ------------------
if not defined PY_HAS_DESKTOP goto :mc_after_desktop

echo.
echo -- Manual fallback (--desktop): tauri-cli ---------------------------------
REM tauri-cli = ~/.cargo/bin/cargo-tauri.exe + .pdb. Project-specific; safe.
set "CARGO_BIN_DIR=%USERPROFILE%\.cargo\bin"
set "TAURI_REMOVED=0"
if exist "%CARGO_BIN_DIR%\cargo-tauri.exe" (
    del /f /q "%CARGO_BIN_DIR%\cargo-tauri.exe" 2>nul
    if not exist "%CARGO_BIN_DIR%\cargo-tauri.exe" set "TAURI_REMOVED=1"
)
if exist "%CARGO_BIN_DIR%\cargo-tauri.pdb" (
    del /f /q "%CARGO_BIN_DIR%\cargo-tauri.pdb" 2>nul
)
if "!TAURI_REMOVED!"=="1" (
    echo    [OK] tauri-cli removed from %CARGO_BIN_DIR%.
) else (
    echo    [SKIP] tauri-cli not present.
)

if not defined PY_HAS_DESKTOP_RUST goto :mc_after_desktop

echo -- Manual fallback (--desktop-rust): Rust toolchain -----------------------
REM Detect presence of rustup / cargo / ~/.rustup / ~/.cargo. If none present
REM at all, skip silently. Otherwise show the same SHARED-resource warning
REM the Python path uses, then gate on YES (unless --yes was passed).
set "RUSTUP_EXE=%USERPROFILE%\.cargo\bin\rustup.exe"
set "CARGO_EXE=%USERPROFILE%\.cargo\bin\cargo.exe"
set "RUSTUP_HOME=%USERPROFILE%\.rustup"
set "CARGO_HOME=%USERPROFILE%\.cargo"

set "RUST_PRESENT="
if exist "%RUSTUP_EXE%"  set "RUST_PRESENT=1"
if exist "%CARGO_EXE%"   set "RUST_PRESENT=1"
if exist "%RUSTUP_HOME%" set "RUST_PRESENT=1"
if not defined RUST_PRESENT (
    echo    [SKIP] Rust toolchain not present.
    goto :mc_after_desktop
)

REM YES gate (skipped when --yes is passed).
if defined PY_HAS_YES goto :mc_rust_run
echo.
echo ======================================================================
echo   About to UNINSTALL the Rust toolchain ^(rustup + cargo + all toolchains^).
echo   This will remove ^~/.rustup and ^~/.cargo ^(including %CARGO_BIN_DIR%^).
echo.
echo   WARNING: SHARED resource. Any OTHER Rust project on this machine
echo   that uses cargo / rustc / rustup will BREAK. We cannot tell whether
echo   Setup.bat installed rustup or you installed it manually before
echo   Setup.bat ever ran.
echo.
echo   Any tools you `cargo install`-ed ^(ripgrep, fd, etc.^) will also be
echo   removed.
echo ======================================================================
set "RUST_CONFIRM="
set /p "RUST_CONFIRM=Type YES to proceed (any other input cancels): "
if /i not "!RUST_CONFIRM!"=="YES" (
    echo    Rust toolchain uninstall: cancelled by operator.
    goto :mc_after_desktop
)

:mc_rust_run
REM Phase 1 -- official rustup self uninstall (handles ~/.rustup + ~/.cargo).
if exist "%RUSTUP_EXE%" (
    echo    [INFO] Running rustup self uninstall ^(may take a minute^)...
    "%RUSTUP_EXE%" self uninstall -y >nul 2>&1
    if errorlevel 1 (
        echo    [WARN] rustup self uninstall reported an error; falling
        echo           through to rmtree of residual paths.
    )
) else (
    echo    [INFO] rustup.exe missing -- purging residual paths only.
)

REM Phase 2 -- defensive rmtree of leftovers.
if exist "%RUSTUP_HOME%" (
    rd /s /q "%RUSTUP_HOME%" 2>nul
    if exist "%RUSTUP_HOME%" (
        echo    [WARN] %RUSTUP_HOME% partially removed; some files may be locked.
    ) else (
        echo    [OK] %RUSTUP_HOME% removed.
    )
)
if exist "%CARGO_HOME%" (
    rd /s /q "%CARGO_HOME%" 2>nul
    if exist "%CARGO_HOME%" (
        echo    [WARN] %CARGO_HOME% partially removed; some files may be locked.
    ) else (
        echo    [OK] %CARGO_HOME% removed.
    )
)

REM Phase 3 -- final state check.
if exist "%RUSTUP_EXE%" (
    echo    [WARN] Rust toolchain: some files still present. Reboot and retry.
) else if exist "%CARGO_EXE%" (
    echo    [WARN] Rust toolchain: some files still present. Reboot and retry.
) else (
    echo    [OK] Rust toolchain uninstall complete.
)

:mc_after_desktop

REM ---- --vs extra (manual fallback) -----------------------------------------
if not defined PY_HAS_VS goto :mc_after_vs

echo.
echo -- Manual fallback (--vs): Visual Studio 2022 -----------------------------
set "VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
set "VS_INSTALLER=C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe"

REM Detect installed VS productId (empty when half-broken / not registered).
set "VS_PRODUCT_ID="
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -property productId 2^>nul`) do set "VS_PRODUCT_ID=%%I"
)

REM Detect residual: if any one of the well-known VS paths still exists, we
REM have something to clean even when no product is registered.
set "VS_HAS_RESIDUAL="
if exist "C:\Program Files\Microsoft Visual Studio\2022\Community" set "VS_HAS_RESIDUAL=1"
if exist "C:\ProgramData\Microsoft\VisualStudio" set "VS_HAS_RESIDUAL=1"
if exist "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Visual Studio 2022" set "VS_HAS_RESIDUAL=1"
if exist "%LOCALAPPDATA%\Microsoft\VisualStudio" set "VS_HAS_RESIDUAL=1"
if exist "%APPDATA%\Microsoft\VisualStudio" set "VS_HAS_RESIDUAL=1"
if exist "C:\Program Files (x86)\Microsoft Visual Studio\Installer" set "VS_HAS_RESIDUAL=1"

if not defined VS_PRODUCT_ID if not defined VS_HAS_RESIDUAL (
    echo    [SKIP] VS not present (no installer, no product, no residual).
    goto :mc_after_vs
)

REM YES gate (skipped when --yes is passed).
if defined PY_HAS_YES goto :mc_vs_run
echo.
echo ======================================================================
if defined VS_PRODUCT_ID (
    echo   About to UNINSTALL Visual Studio 2022 ^(!VS_PRODUCT_ID!^) and any residual files.
) else (
    echo   No VS product is registered, but VS residual files were detected.
    echo   About to remove those residual files ^(install dirs, Start Menu shortcuts,
    echo   ProgramData / AppData VisualStudio caches^).
)
echo   WARNING: VS is a general-purpose IDE. Other projects on this
echo   machine may depend on it. We cannot tell whether VS was installed
echo   by Setup.bat or by you beforehand.
echo   This will take 5-15 minutes and is irreversible.
echo ======================================================================
set "VS_CONFIRM="
set /p "VS_CONFIRM=Type YES to proceed (any other input cancels): "
if /i not "!VS_CONFIRM!"=="YES" (
    echo    VS uninstall: cancelled by operator.
    goto :mc_after_vs
)

:mc_vs_run
REM Phase 1 -- invoke setup.exe uninstall if a product is currently registered.
if not defined VS_PRODUCT_ID goto :mc_vs_residual
if not exist "%VS_INSTALLER%" (
    echo    [WARN] product '!VS_PRODUCT_ID!' registered but setup.exe missing;
    echo           skipping product uninstall, will purge residual paths only.
    goto :mc_vs_residual
)
echo    [INFO] Invoking VS Installer for !VS_PRODUCT_ID!
echo    [INFO] (this can take 5-15 minutes; the VS Installer may self-update first,
echo           then spawn a newer installer to do the actual uninstall)
"%VS_INSTALLER%" uninstall --productId !VS_PRODUCT_ID! --channelId VisualStudio.17.Release --quiet --norestart --force
set "VS_SPAWN_RC=!errorlevel!"
if "!VS_SPAWN_RC!"=="740" (
    echo    [ERR] VS uninstall: launcher requires administrator elevation ^(exit=740^).
    echo          Re-run Uninstall.bat from an elevated shell.
    goto :mc_after_vs
)

REM Wait for VS Installer processes to fully exit (covers self-update spawn).
echo    [INFO] Waiting for VS Installer processes to finish...
powershell -NoProfile -Command "$deadline = (Get-Date).AddMinutes(20); $last = -1; while ((Get-Date) -lt $deadline) { $procs = Get-Process -Name 'setup','vs_installer','vs_installerservice' -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -like 'C:\Program Files (x86)\Microsoft Visual Studio\Installer*' }; $count = if ($procs) { $procs.Count } else { 0 }; if ($count -eq 0) { break }; if ($count -ne $last) { Write-Host ('   ('+$count+' VS Installer process(es) still running...)'); $last = $count }; Start-Sleep -Seconds 5 }; if ((Get-Date) -ge $deadline) { Write-Host '   [WARN] VS Installer still running after 20 min; checking final state anyway.' }"

REM Authoritative success check via vswhere.
set "VS_FINAL_PRODUCT="
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -property productId 2^>nul`) do set "VS_FINAL_PRODUCT=%%I"
)
if defined VS_FINAL_PRODUCT (
    echo    [ERR] VS uninstall: product still registered as '!VS_FINAL_PRODUCT!'
    echo          ^(initial spawn exit=!VS_SPAWN_RC!^). Uninstall did NOT succeed.
    echo          Open the Visual Studio Installer GUI and click Uninstall, then
    echo          re-run this script.
    goto :mc_after_vs
)
echo    [OK] VS 2022 uninstall: confirmed via vswhere -- product '!VS_PRODUCT_ID!' removed.

:mc_vs_residual
REM Phase 2 -- purge residual VS paths (install dirs, Start Menu shortcuts,
REM caches). Runs whether or not Phase 1 ran, so a half-broken state
REM (Installer metadata gone but install tree + shortcuts left behind) gets
REM cleaned by a follow-up `--vs` invocation.
echo    [INFO] Removing VS residual paths...
for %%R in (
    "C:\Program Files\Microsoft Visual Studio\2022\Community"
    "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Visual Studio 2022"
    "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Visual Studio 2022"
    "C:\ProgramData\Microsoft\VisualStudio"
    "%LOCALAPPDATA%\Microsoft\VisualStudio"
    "%APPDATA%\Microsoft\VisualStudio"
    "C:\Program Files (x86)\Microsoft Visual Studio\Installer"
) do (
    if exist %%R (
        rd /s /q %%R 2>nul
        if exist %%R (
            echo    [WARN] could not fully remove %%R
        ) else (
            echo    [OK] removed %%R
        )
    )
)

REM Prune empty VS parents -- `rd` (no /s) only succeeds on empty dirs, so a
REM sibling install (VS 2019, VS 2026, etc.) anchors the parent and we leave
REM it alone.
for %%P in (
    "C:\Program Files\Microsoft Visual Studio\2022"
    "C:\Program Files\Microsoft Visual Studio"
    "C:\Program Files (x86)\Microsoft Visual Studio"
) do (
    if exist %%P (
        rd %%P 2>nul && echo    [OK] removed empty parent %%P
    )
)

:mc_after_vs

echo.
echo  Removed what Setup.bat installed OUTSIDE the project (venvs, PortableGit,
echo  Node.js, temp, data\bin, uv pythons, install temp archives^). The project
echo  directory -- including data\ -- is left fully intact.
if not defined PY_HAS_ALL if not defined PY_HAS_VS if not defined PY_HAS_DESKTOP (
    echo.
    echo  Optional: deeper cleanup
    echo    --all           Also remove uv cache, QAIRT SDK, Playwright Chromium,
    echo                    vendor/ runtime caches, and %%TEMP%%\jieba.cache.
    echo    --vs            Also UNINSTALL Visual Studio 2022 Community.
    echo    --desktop       Also remove tauri-cli ^(Setup Step 5c artifact, safe^).
    echo    --desktop-rust  Also uninstall Rust toolchain ^(SHARED resource^).
    echo    --help          Show full help.
)
echo.

:done
REM Pause for the user when interactive (no --yes / -y / --quiet / --all).
if not defined PY_HAS_YES pause
endlocal
exit /b 0

REM ===========================================================================
REM  --help / -h / /?  (does nothing else; pure documentation)
REM ===========================================================================
:print_help
echo.
echo  QAIModelBuilder - Uninstall.bat
echo  Rolls back what Setup.bat installed OUTSIDE the project directory
echo  (venvs, PortableGit, Node.js, temp, data\bin tooling, uv pythons).
echo  Your project folder and its data\ ^(qai.db, logs, config,
echo  runtime-downloaded model weights^) are NEVER touched.
echo.
echo  USAGE:
echo      Uninstall.bat [options]
echo.
echo  OPTIONS:
echo      ^(no flag^)         Interactive default uninstall ^(prompts y/N^).
echo                        Rolls back ONLY what is safe to remove.
echo.
echo      --yes, -y         Non-interactive: skip the default y/N prompt.
echo      --quiet           Alias for --yes.
echo.
echo      --clean-uv        Default + uv package cache
echo                        ^(%%LOCALAPPDATA%%\uv\cache^).
echo                        WARNING: SHARED with other uv projects.
echo                        Implied by --all.
echo.
echo      --all             FULL uninstall: default + uv cache + QAIRT SDK
echo                        ^(only the Setup-installed version^) + Playwright
echo                        Chromium cache + vendor\ runtime caches +
echo                        %%TEMP%%\jieba.cache. Implies --yes.
echo                        Does NOT touch Visual Studio 2022, Rust toolchain,
echo                        or tauri-cli ^(all SHARED with other projects;
echo                        pass --vs / --desktop-rust / --desktop explicitly^).
echo.
echo      --vs              ALSO uninstall Visual Studio 2022 Community via
echo                        the VS Installer ^(setup.exe uninstall^).
echo                        WARNING: VS is a general-purpose IDE; other
echo                        projects on this machine may rely on it.
echo                        Interactive YES gate unless --yes is set.
echo                        Independent of --all.
echo.
echo      --desktop         ALSO remove tauri-cli ^(the `cargo tauri`
echo                        subcommand Setup Step 5c installs^). Removes only
echo                        cargo-tauri.exe + .pdb under ^~\.cargo\bin\.
echo                        Project-specific and SAFE; does NOT touch the
echo                        Rust toolchain itself. Independent of --all.
echo.
echo      --desktop-rust    ALSO uninstall the Rust toolchain ^(rustup +
echo                        cargo + all toolchains + ^~\.rustup + ^~\.cargo^)
echo                        via `rustup self uninstall -y`.
echo                        WARNING: SHARED with every other Rust project
echo                        on this machine. Any tools you `cargo install`-ed
echo                        ^(ripgrep, fd, etc.^) will also be removed.
echo                        Interactive YES gate unless --yes is set.
echo                        Implies --desktop. Independent of --all.
echo.
echo      --help, -h, /?    Show this help and exit ^(removes nothing^).
echo.
echo  EXAMPLES:
echo      Uninstall.bat                          Default interactive uninstall.
echo      Uninstall.bat --yes                    Default, non-interactive.
echo      Uninstall.bat --all                    Full uninstall ^(NOT VS / Rust^).
echo      Uninstall.bat --all --vs               Full + Visual Studio 2022.
echo      Uninstall.bat --all --desktop-rust     Full + Rust toolchain.
echo      Uninstall.bat --yes --all --vs --desktop-rust
echo                                             EVERYTHING, non-interactive.
echo      Uninstall.bat --desktop                Default + remove tauri-cli.
echo.
echo  NOTES:
echo      * The project directory and data\ are NEVER deleted. Delete the
echo        project folder manually if you want to remove the app entirely.
echo      * --vs / --desktop-rust each have their own irreversible YES gate
echo        ^(they remove SHARED resources^). Pass --yes to skip both.
echo      * --all does NOT imply --vs or --desktop-rust ^(both SHARED^).
echo        Add them explicitly when you want the heavier cleanup.
echo.
endlocal
exit /b 0
