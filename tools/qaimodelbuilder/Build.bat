@echo off
REM ===========================================================================
REM Build.bat - Compile the V2 WebUI frontend into the production SPA bundle
REM             (frontend/dist) that Start.bat / apps.api serves at runtime.
REM
REM The Python backend is interpreted (no compile step); only the Vue/Vite
REM frontend needs building. After changing frontend source, run this script
REM once to refresh frontend/dist, then (re)launch Start.bat. After changing
REM ONLY backend code, you can skip this script and just restart Start.bat.
REM
REM Usage:
REM   Build.bat            Fast build: vite build only (skips typecheck /
REM                        lint / unit tests). Best for the iteration loop.
REM   Build.bat --full     Full verified build: gen:types + typecheck + lint +
REM                        unit tests + build. Use before sharing / releasing.
REM   Build.bat --install  Force pnpm install first (e.g. after deps change).
REM                        By default install is skipped when node_modules is
REM                        already present (fastest iteration).
REM   Build.bat --clean    Wipe node_modules + pnpm-lock.yaml integrity, then
REM                        do a fresh pnpm install. Use when node_modules is
REM                        corrupt (e.g. ERR_MODULE_NOT_FOUND for a transitive
REM                        dep, or when copied across OS/arch boundaries).
REM   Build.bat --desktop  ALSO compile the Tauri desktop app (Rust shell) after
REM                        refreshing frontend/dist, producing the installer(s)
REM                        (.msi/.exe) under
REM                        desktop\src-tauri\target\release\bundle\. Required
REM                        whenever you change the Rust shell (desktop\src-tauri
REM                        \src\*.rs) OR the frontend, so the bundled desktop app
REM                        picks up both. Plain Build.bat only refreshes
REM                        frontend/dist (Web/Start.bat mode); the desktop shell
REM                        is NOT recompiled without this flag.
REM   Build.bat --desktop-dev
REM                        Refresh frontend/dist then launch `cargo tauri dev`
REM                        (debug desktop window, hot Rust rebuild). Does NOT
REM                        produce an installer; for local desktop verification.
REM
REM   Flags compose, e.g. `Build.bat --full --desktop` does a fully-verified
REM   frontend build then a release desktop bundle.
REM ===========================================================================
setlocal EnableDelayedExpansion

set "ROOT_DIR=%~dp0"
set "MODE=fast"
set "DO_INSTALL=0"
set "DO_CLEAN=0"
set "DO_DESKTOP=0"
set "DESKTOP_MODE=build"
:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--help" goto print_help
if /I "%~1"=="-h" goto print_help
if /I "%~1"=="/?" goto print_help
if /I "%~1"=="--full" set "MODE=full"
if /I "%~1"=="--install" set "DO_INSTALL=1"
if /I "%~1"=="--clean" ( set "DO_CLEAN=1" & set "DO_INSTALL=1" )
if /I "%~1"=="--desktop" set "DO_DESKTOP=1"
if /I "%~1"=="--desktop-dev" ( set "DO_DESKTOP=1" & set "DESKTOP_MODE=dev" )
shift
goto parse_args
:args_done

echo.
echo  +------------------------------------------+
echo  ^|   QAI ModelBuilder  -  Building WebUI    ^|
echo  +------------------------------------------+
echo.

REM -- Node.js / pnpm PATH injection -----------------------------------------
REM Setup.bat installs a portable ARM64 Node into %LOCALAPPDATA%\QAIModelBuilder
REM \node and enables pnpm there via corepack. Build.bat runs standalone (not
REM through Setup.bat), so prepend that dir to PATH here too. If absent, fall
REM back to whatever node/pnpm is already on PATH.
set "NODE_DIR=%LOCALAPPDATA%\QAIModelBuilder\node"
if exist "%NODE_DIR%\node.exe" set "PATH=%NODE_DIR%;%PATH%"

REM Disable corepack's interactive download prompt. The very first time
REM corepack actually has to download a pnpm tarball from npmjs (which
REM happens lazily, sometimes on the first real `pnpm install` rather than
REM at `corepack enable` / `prepare` time), it asks "Do you want to
REM continue? [Y/n]" and hangs the build. Setting this env var makes
REM corepack just download silently. Setup.bat pre-warms the download so
REM in normal flow this is a belt-and-suspenders fallback.
set "COREPACK_ENABLE_DOWNLOAD_PROMPT=0"

REM Verify pnpm is reachable before doing anything; give a clear hint otherwise.
where pnpm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pnpm not found. Node.js / pnpm is missing.
    echo [ERROR] Run Setup.bat first ^(it installs portable ARM64 Node + pnpm into
    echo [ERROR] %NODE_DIR%^), then re-run Build.bat.
    exit /b 1
)

pushd "%ROOT_DIR%frontend" || (echo [ERROR] frontend not found & exit /b 1)

REM 1. Install dependencies (skipped when node_modules already present).
REM    A non-frozen install is used so a drifted lockfile does not block.
REM
REM Detection of corrupted node_modules: even when node_modules\.bin exists,
REM transitive deps may be missing (e.g. esbuild absent under
REM node_modules/vite/node_modules/, leading to "ERR_MODULE_NOT_FOUND" when
REM vite starts). We probe for a few known-critical packages — if any are
REM missing, force a fresh install.
if not exist "node_modules\.bin" set "DO_INSTALL=1"
if not exist "node_modules\vite" set "DO_INSTALL=1"
if not exist "node_modules\esbuild" set "DO_INSTALL=1"
if not exist "node_modules\vue" set "DO_INSTALL=1"

REM --clean: nuke node_modules entirely before installing. Use this when a
REM regular `pnpm install` cannot heal the tree (e.g. node_modules copied
REM across OS/arch, hard-linking confused, integrity-mismatch).
if "%DO_CLEAN%"=="1" (
    if exist "node_modules" (
        echo [INFO] --clean: removing node_modules ^(this may take a minute^)
        rmdir /s /q "node_modules" 2>nul
    )
)

if "%DO_INSTALL%"=="1" (
    echo [INFO] pnpm install
    REM --config.confirmModulesPurge=false suppresses pnpm's interactive
    REM "The modules directory ... will be removed and reinstalled from
    REM scratch. Proceed? (Y/n)" prompt. pnpm asks this whenever node_modules
    REM was created by a DIFFERENT pnpm major (e.g. after Setup.bat bumped the
    REM pinned pnpm version), which would otherwise HANG this non-interactive
    REM build script waiting for keyboard input. Auto-accepting is correct here:
    REM a from-scratch reinstall is exactly what we want when the store layout
    REM is incompatible.
    call pnpm install --config.confirmModulesPurge=false || goto err
) else (
    echo [INFO] node_modules present; skipping install ^(use --install to force, --clean to wipe+reinstall^)
)

if "%MODE%"=="full" goto full

REM Fast path: vite build only (frontend/dist refreshed).
echo [INFO] Fast build (vite build only; skipping typecheck/lint/test)
call pnpm exec vite build || goto err
goto done

:full
REM Full path: regenerate types + full verification + build.
echo [INFO] Full verified build (gen:types + typecheck + lint + test + build)
call pnpm gen:types || goto err
call pnpm typecheck || goto err
call pnpm lint || goto err
call pnpm test || goto err
call pnpm build || goto err

:done
popd
echo.
echo [INFO] Build complete. frontend\dist refreshed.

REM -- Optional: compile the Tauri desktop app (Rust shell) -------------------
REM Plain Build.bat stops here (Web/Start.bat mode). With --desktop / --desktop-dev
REM we ALSO (re)compile the desktop shell so a change in desktop\src-tauri\src\*.rs
REM (or the just-rebuilt frontend/dist it embeds) makes it into the desktop app.
if "%DO_DESKTOP%"=="1" goto desktop

echo [INFO] Now (re)launch Start.bat to serve the new bundle (Start.bat will
echo        auto-open your browser at the chosen URL once the server is ready).
endlocal
exit /b 0

REM ===========================================================================
REM Desktop (Tauri) build
REM ===========================================================================
:desktop
echo.
echo  +------------------------------------------+
echo  ^|   QAI ModelBuilder  -  Building Desktop  ^|
echo  +------------------------------------------+
echo.

REM -- Rust / cargo PATH injection -------------------------------------------
REM rustup installs cargo into %USERPROFILE%\.cargo\bin. Build.bat may run from
REM a shell that does not have it on PATH (e.g. a fresh GUI-launched cmd), so
REM prepend it here, mirroring the Node PATH injection above. If absent, fall
REM back to whatever cargo is already on PATH.
set "CARGO_BIN=%USERPROFILE%\.cargo\bin"
if exist "%CARGO_BIN%\cargo.exe" set "PATH=%CARGO_BIN%;%PATH%"

REM Verify cargo is reachable.
where cargo >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cargo not found. The Rust toolchain is required to build the
    echo [ERROR] desktop app. Install it from https://rustup.rs ^(adds cargo to
    echo [ERROR] %CARGO_BIN%^), then re-run Build.bat --desktop.
    endlocal
    exit /b 1
)

REM Verify the `cargo tauri` subcommand (tauri-cli) is installed. It is NOT a
REM project dependency in Cargo.toml; it is a global cargo subcommand. Give a
REM precise install hint rather than letting `cargo tauri` fail cryptically.
cargo tauri --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] `cargo tauri` subcommand not found ^(tauri-cli is not installed^).
    echo [ERROR] Install it once with:
    echo [ERROR]     cargo install tauri-cli --version ^^^>=2.0.0 --locked
    echo [ERROR] then re-run Build.bat --desktop. ^(First run also needs the
    echo [ERROR] Visual Studio C++ build tools + WebView2 runtime on Windows.^)
    endlocal
    exit /b 1
)

pushd "%ROOT_DIR%desktop\src-tauri" || (echo [ERROR] desktop\src-tauri not found & endlocal & exit /b 1)

if "%DESKTOP_MODE%"=="dev" goto desktop_dev

echo [INFO] Building release desktop bundle ^(this can take several minutes^)
cargo tauri build
set "DESKTOP_EXIT=%ERRORLEVEL%"
popd
if not "%DESKTOP_EXIT%"=="0" goto err_desktop
echo.
echo [INFO] Desktop build complete. Installer(s) under:
echo        desktop\src-tauri\target\release\bundle\

REM Note: WebView2 Fixed Version Runtime (~811 MB) is NOT copied into the
REM build output. The exe reads it from data\bin\webview2\ at runtime
REM (installed by Setup.bat --desktop). For standalone distribution,
REM ensure the target machine runs Setup.bat --desktop first.
if exist "%ROOT_DIR%data\bin\webview2" (
    echo [INFO] WebView2 Fixed Version available at data\bin\webview2\
    echo [INFO] ^(exe reads it at runtime; not bundled into installer output^)
) else (
    echo [WARN] data\bin\webview2\ not found. Run Setup.bat --desktop to install.
    echo [WARN] The desktop exe will fall back to system WebView2 Evergreen Runtime.
)
endlocal
exit /b 0

:desktop_dev
echo [INFO] Launching `cargo tauri dev` ^(debug desktop window; Ctrl+C to stop^)
REM Empty beforeDevCommand in tauri.conf.json means Tauri serves the
REM existing frontend/dist we just rebuilt (no separate vite dev server).
cargo tauri dev
set "DESKTOP_EXIT=%ERRORLEVEL%"
popd
if not "%DESKTOP_EXIT%"=="0" goto err_desktop
endlocal
exit /b 0

:err_desktop
echo.
echo [ERROR] Desktop build failed (exit code %DESKTOP_EXIT%).
endlocal ^& exit /b %DESKTOP_EXIT%

:err
set "EXIT_CODE=%ERRORLEVEL%"
popd
echo.
echo [ERROR] Build failed (exit code %EXIT_CODE%).
endlocal ^& exit /b %EXIT_CODE%

REM ===========================================================================
REM  --help / -h / /?  (does nothing else; pure documentation)
REM ===========================================================================
:print_help
echo.
echo  QAIModelBuilder - Build.bat
echo  Compiles the V2 WebUI frontend ^(Vue/Vite -^> frontend\dist^) and, with
echo  --desktop, the Tauri desktop shell ^(desktop\src-tauri -^> installer^).
echo  The Python backend is interpreted and needs NO build step; restart
echo  Start.bat to pick up backend-only changes.
echo.
echo  USAGE:
echo      Build.bat [options]
echo.
echo  OPTIONS:
echo      ^(no flag^)      Fast frontend build: `vite build` only ^(skips
echo                     typecheck / lint / unit tests^). Best for the
echo                     iteration loop. Refreshes frontend\dist.
echo.
echo      --full         Full verified frontend build:
echo                         gen:types + typecheck + lint + test + build.
echo                     Use before sharing / releasing.
echo.
echo      --install      Force `pnpm install` first ^(e.g. after deps change^).
echo                     By default install is skipped when node_modules is
echo                     already present ^(fastest iteration^).
echo.
echo      --clean        Wipe node_modules + reinstall from scratch. Use when
echo                     node_modules is corrupt ^(e.g. ERR_MODULE_NOT_FOUND
echo                     for a transitive dep, or copied across OS/arch^).
echo                     Implies --install.
echo.
echo      --desktop      ALSO compile the Tauri desktop app ^(Rust shell^)
echo                     after refreshing frontend\dist, producing the
echo                     installer^(s^) ^(.msi/.exe^) under
echo                         desktop\src-tauri\target\release\bundle\.
echo                     Required whenever you change desktop\src-tauri\src\*.rs
echo                     OR the frontend, so the bundled desktop app picks
echo                     up both. Plain Build.bat only refreshes frontend\dist
echo                     ^(Web/Start.bat mode^); the desktop shell is NOT
echo                     recompiled without this flag.
echo.
echo      --desktop-dev  Refresh frontend\dist then launch `cargo tauri dev`
echo                     ^(debug desktop window, hot Rust rebuild^). Does NOT
echo                     produce an installer; for local desktop verification.
echo.
echo      --help, -h, /? Show this help and exit ^(builds nothing^).
echo.
echo  NOTES:
echo      * Flags compose. `Build.bat --full --desktop` does a fully-verified
echo        frontend build then a release desktop bundle.
echo      * --desktop / --desktop-dev need the Rust toolchain + tauri-cli.
echo        Setup.bat does NOT install them by default ^(opt-in to keep the
echo        base install slim^). Enable them once with:
echo            Setup.bat --desktop
echo        which runs Step 5c ^(rustup + `cargo install tauri-cli --locked`,
echo        ~600MB download + 5-10min compile on ARM64^). Or install manually:
echo            https://rustup.rs    ^(Rust toolchain^)
echo            cargo install tauri-cli --version "^>=2.0.0" --locked
echo.
echo  EXAMPLES:
echo      Build.bat                     Fast iteration build of the Web UI.
echo      Build.bat --full              Fully-verified Web UI build.
echo      Build.bat --desktop           Web UI + release desktop installer.
echo      Build.bat --desktop-dev       Web UI + live debug desktop window.
echo      Build.bat --full --desktop    Fully-verified + release desktop.
echo      Build.bat --clean             Heal a corrupt node_modules.
echo.
echo  After a frontend build:
echo      Start.bat ^(serve the new bundle^)  ^|  Run the desktop installer
echo      under desktop\src-tauri\target\release\bundle\
echo.
endlocal
exit /b 0
