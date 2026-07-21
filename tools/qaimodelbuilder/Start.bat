@echo off

REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------

REM Start.bat - Windows launcher for QAIModelBuilder (v2.7 / S9).
REM
REM Features:
REM   1. PortableGit PATH injection (if installed)
REM   2. Stale endpoint cleanup (kills a previous server that died without
REM      clearing its runtime endpoint file)
REM   3. Browser auto-open after server starts (reads the actual port from
REM      the endpoint file the API writes once it is serving traffic)
REM   4. System Python fallback if venv is absent
REM   5. Supervisor reboot loop (exit code 75 = restart)
REM
REM Usage:
REM   Start.bat             normal mode
REM   Start.bat --reload    hot-reload mode (development)
REM
REM Run Setup.bat first to create the venv and install deps (recommended).
REM
REM Port selection: NO PORT IS HARD-CODED HERE. The supervisor
REM (apps.cli.serve) probes a list of fallback ports at startup and binds
REM the first one the OS accepts. The chosen port + URL are written to
REM ``data/runtime/server.endpoint.json`` and read by the helper below to
REM open the browser at the right place. This makes the launcher safe on
REM machines where Hyper-V / WSL2 / Docker have reserved the documented
REM default port (8989) inside a Windows excluded port range — see
REM ``netsh int ipv4 show excludedportrange protocol=tcp``.
setlocal EnableDelayedExpansion

echo.
echo  +------------------------------------------+
echo  ^|   QAI ModelBuilder  -  Starting...       ^|
echo  +------------------------------------------+
echo.

set "ROOT_DIR=%~dp0"
set "VENV=%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313"
set "PYTHON=%VENV%\Scripts\python.exe"

REM Route Python bytecode caches out of the source tree into data\caches\pycache
REM (keeps the source tree clean; data\ is the per-user runtime root and is
REM git-ignored). %~dp0 has a trailing backslash so no extra separator is needed.
set "PYTHONPYCACHEPREFIX=%~dp0data\caches\pycache"

REM -- 1. PortableGit PATH injection ------------------------------------------
set "PORTABLE_GIT_DIR=%LOCALAPPDATA%\QAIModelBuilder\git"
if exist "%PORTABLE_GIT_DIR%\cmd\git.exe" (
    set "PATH=%PORTABLE_GIT_DIR%\cmd;%PORTABLE_GIT_DIR%\usr\bin;%PATH%"
    echo [INFO] PortableGit added to PATH from %PORTABLE_GIT_DIR%\cmd
) else if exist "%PORTABLE_GIT_DIR%\bin\git.exe" (
    set "PATH=%PORTABLE_GIT_DIR%\bin;%PORTABLE_GIT_DIR%\usr\bin;%PATH%"
    echo [INFO] PortableGit added to PATH from %PORTABLE_GIT_DIR%\bin
)

REM -- 2. Determine Python interpreter ----------------------------------------
if exist "%PYTHON%" (
    echo [INFO] Using venv Python: %PYTHON%
    goto :set_env
)

REM System Python fallback
echo [INFO] Venv not found at %VENV%, trying system Python...
where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python"
    echo [INFO] Using system python.
    goto :set_env
)
where python3 >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python3"
    echo [INFO] Using system python3.
    goto :set_env
)

echo [ERROR] No Python found. Install Python 3.13+ or run Setup.bat first.
pause
exit /b 1

:set_env
REM -- Set PYTHONPATH for src layout ------------------------------------------
set "PYTHONPATH=src;."
cd /d "%ROOT_DIR%"

REM -- 3. Stale endpoint / orphan server cleanup ------------------------------
REM Replaces the old "netstat | findstr :8989 | taskkill" heuristic, which
REM (a) hard-coded a port that is now dynamic and (b) never verified the PID
REM actually owned the port. The helper reads ``data/runtime/server.endpoint.json``,
REM checks whether the recorded PID is still alive, and if so terminates it
REM and clears the file. Idempotent — succeeds silently when the file is
REM absent (the common case).
echo [INFO] Cleaning up any stale endpoint from a previous run...
"%PYTHON%" -m apps.cli._endpoint_helper cleanup-stale >nul 2>&1

REM -- 4. Browser auto-open (background; waits for the endpoint file) ---------
REM The supervisor may pick a fallback port if the documented default is
REM inside a Windows reserved range, so we MUST NOT hard-code the URL here.
REM ``wait-and-open`` polls ``data/runtime/server.endpoint.json`` for up to
REM 30s and opens whatever URL the API actually bound. Runs in the
REM background so this .bat returns immediately.
echo [INFO] Browser will auto-open once the server is ready...
start "" /b cmd /c ""%PYTHON%" -m apps.cli._endpoint_helper wait-and-open --timeout 60 >nul 2>&1"

REM -- 5. Launch the server in THIS console (foreground) ---------------------
REM CRITICAL FIX (orphan daemon on window close):
REM   Previously this used ``start "QAI ModelBuilder" cmd /k <command>`` which
REM   spawned the supervisor in a SEPARATE, NEW console window and let this
REM   .bat exit immediately. That detached the supervisor from the window the
REM   user actually launched from: closing the original/launch window sent NO
REM   CTRL_CLOSE_EVENT to the supervisor (it lived in the other console), so
REM   the supervisor never ran its shutdown, its KILL_ON_JOB_CLOSE Job Object
REM   handle stayed open, and the daemon (apps.api) + runner_bootstrap + all
REM   child processes kept running in the background = the reported "daemon
REM   still alive after closing the window" orphan.
REM
REM   Running the supervisor in the FOREGROUND of THIS console makes the
REM   window the user sees / closes be the exact console the supervisor is
REM   attached to. Then EVERY close path reaches the supervisor:
REM     * click [X] / close tab / close whole terminal  -> Windows delivers
REM       CTRL_CLOSE_EVENT to the supervisor's console handler
REM       (serve.py `_on_console_close`), which forwards CTRL_BREAK to the
REM       daemon and waits for it to exit; on supervisor exit the Job Object
REM       closes and reaps anything left.
REM     * Ctrl+C -> the supervisor's ConsoleCtrlInterceptor intercepts it
REM       (returns handled=TRUE) and shows the Yes/No menu; on exit it calls
REM       os._exit(0) which ALSO bypasses cmd's "Terminate batch job (Y/N)?"
REM       prompt, so no Y/N appears even though we are inside a .bat.
REM
REM   We use ``call`` (not ``start``) so control stays in this batch and the
REM   supervisor shares this console. The reboot loop (exit code 75) is handled
REM   INSIDE the Python supervisor (apps.cli.serve._Supervisor respawns the
REM   child internally and never returns 75 to the shell), so no .bat-level
REM   ``goto`` loop is needed.
REM
REM   --port 4099 is pinned for Okta SSO (auth.enabled=true): Okta only accepts
REM   the registered redirect_uri http://localhost:4099/callback, so the server
REM   MUST bind 4099 or every login round-trip fails with "redirect_uri
REM   mismatch". It is placed BEFORE %* so an explicit ``Start.bat -- --port
REM   <n>`` still overrides it (argparse takes the last occurrence).
echo [INFO] Launching server on port 4099 (Okta SSO redirect_uri) ...
echo [INFO] Keep this window open. Close it (or press Ctrl+C) to stop the server.
call "%PYTHON%" -m apps.cli.serve --port 4099 %*

endlocal
exit /b 0
