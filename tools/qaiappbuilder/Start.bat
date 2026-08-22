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
REM   Start.bat                    normal mode (auto-fallback across
REM                                factory\config\ports.json ``fallbacks``)
REM   Start.bat --port 8989        pin the primary Okta redirect_uri port
REM   Start.bat --port 28688       pin the secondary Okta redirect_uri port
REM   Start.bat --reload           hot-reload mode (development)
REM   Start.bat --arch <x64|arm64> force host architecture
REM
REM Run Setup.bat first to create the venv and install deps (recommended).
REM
REM Port selection: NO PORT LITERAL IS AUTHORED HERE. All candidates come
REM from factory\config\ports.json ``fallbacks`` (currently ``[8989, 28688]``)
REM — each MUST be an Okta-registered redirect_uri. When ``--port <n>`` is
REM omitted, the supervisor (apps.cli.serve) probes the fallback list and
REM binds the first port the OS accepts, so a busy 8989 recovers into 28688
REM without breaking SSO. The chosen port + URL are written to
REM ``data/runtime/server.endpoint.json`` and read by the helper below to
REM open the browser at the right place. This makes the launcher safe on
REM machines where Hyper-V / WSL2 / Docker have reserved the documented
REM default port inside a Windows excluded port range — see
REM ``netsh int ipv4 show excludedportrange protocol=tcp``.
REM
REM ``--port`` accepts ONLY 8989 or 28688 — any other value hard-errors
REM before we spawn Python. Adding a new value here means adding the
REM matching http://localhost:<n>/callback on the Okta side too.
setlocal EnableDelayedExpansion

echo.
echo  +------------------------------------------+
echo  ^|   QAI ModelBuilder  -  Starting...       ^|
echo  +------------------------------------------+
echo.

set "ROOT_DIR=%~dp0"
REM --- Host architecture selection (three-tier) -----------------------------
REM Priority: 1) --arch <value> CLI flag  2) data\config\host_arch file
REM           3) Auto-detect via %PROCESSOR_ARCHITECTURE% / PROCESSOR_ARCHITEW6432
REM
REM --- SSO-approved port pinning (--port <8989|28688>) ----------------------
REM Optional. Both 8989 (primary) and 28688 (secondary) are registered with
REM Okta as valid loopback redirect_uris (see factory/config/ports.json
REM ``fallbacks``). No other value is permitted here — an unregistered port
REM would silently break SSO with "redirect_uri mismatch".
REM
REM Behaviour:
REM   * ``--port 8989`` / ``--port 28688`` → PIN that port; supervisor skips
REM     the fallback probe (matches the pre-2026-07-28 SSO-hard-lock path).
REM   * flag omitted → supervisor auto-probes the ``fallbacks`` list in
REM     ``factory/config/ports.json`` (currently ``[8989, 28688]``), binding
REM     the first one that is free. Auto-fallback to 28688 lets a busy 8989
REM     recover without breaking SSO because Okta accepts BOTH redirect_uris.
REM   * any other value → hard error before spawning Python.
REM
REM PASS_ARGS is the arg list with any --arch <v> / --port <v> pair removed
REM so it can be forwarded downstream without duplicate flags leaking to
REM Python (the launcher itself decides what --port to pass, if any).
set "FORCED_ARCH="
set "FORCED_PORT="
set "PASS_ARGS="
set "_NEXT_IS_ARCH="
set "_NEXT_IS_PORT="
for %%A in (%*) do (
    if defined _NEXT_IS_ARCH (
        set "FORCED_ARCH=%%~A"
        set "_NEXT_IS_ARCH="
    ) else if defined _NEXT_IS_PORT (
        set "FORCED_PORT=%%~A"
        set "_NEXT_IS_PORT="
    ) else if /i "%%~A"=="--arch" (
        set "_NEXT_IS_ARCH=1"
    ) else if /i "%%~A"=="--port" (
        set "_NEXT_IS_PORT=1"
    ) else (
        set "PASS_ARGS=!PASS_ARGS! %%A"
    )
)
if defined FORCED_PORT (
    if not "!FORCED_PORT!"=="8989" (
        if not "!FORCED_PORT!"=="28688" (
            echo [ERROR] --port !FORCED_PORT! is not permitted.
            echo [ERROR] Only 8989 and 28688 are Okta-registered redirect_uri
            echo [ERROR] loopback ports; any other value would silently break
            echo [ERROR] SSO login. Omit --port to let the supervisor pick one
            echo [ERROR] automatically from factory\config\ports.json fallbacks.
            exit /b 1
        )
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
echo [INFO] Application source root: %ROOT_DIR%
"%PYTHON%" -c "import qai; print('[INFO] Application qai source: ' + qai.__file__)"

REM -- Resolve backend port(s) from the single source of truth ---------------
REM NO port literal is authored here: the backend bind candidates are read at
REM launch from ``factory/config/ports.json`` (``fallbacks`` array) via the
REM stdlib port-registry reader, so this launcher never drifts from the
REM registry. Both candidates are SSO-critical: each MUST be an Okta-registered
REM redirect_uri — see ``factory/config/ports.json`` _notes.fallbacks.
REM
REM Two paths:
REM   * User pinned a port via ``Start.bat --port <8989|28688>`` → we forward
REM     ``--port <n>`` to the supervisor. The supervisor treats an explicit
REM     ``--port`` as "try this exact port, no fallback" (see serve.py
REM     _resolve_bindable_port), which matches the pre-2026-07-28 hard-lock.
REM   * No ``--port`` flag → we OMIT ``--port`` from the supervisor call so
REM     it auto-probes the ``fallbacks`` list and binds the first free port.
REM     Both candidates are Okta-registered, so a fallback bind still yields
REM     a valid ``redirect_uri`` for SSO.
REM
REM ``for /f`` needs the whole back-quoted command wrapped in escaped quotes
REM (``^"..."^"``) because %PYTHON% is a space-containing path AND the -c
REM argument is itself quoted; without the outer ^"...^" cmd mis-splits the
REM line at the first inner quote ("...python.exe" is not recognized).
set "FALLBACK_PORTS_STR="
for /f "usebackq tokens=* delims=" %%P in (`^""%PYTHON%" -c "from qai.platform.config.ports import fallback_ports as f; print(','.join(str(x) for x in f()))"^"`) do set "FALLBACK_PORTS_STR=%%P"
if not defined FALLBACK_PORTS_STR (
  echo [ERROR] Could not resolve fallback ports from factory\config\ports.json.
  echo [ERROR] Is the venv set up? Run Setup.bat first.
  endlocal
  exit /b 1
)
if defined FORCED_PORT (
  set "PORT_ARG=--port !FORCED_PORT!"
  set "PORT_ANNOUNCE=port !FORCED_PORT! (pinned via --port; Okta-registered)"
) else (
  set "PORT_ARG="
  set "PORT_ANNOUNCE=auto-fallback across [!FALLBACK_PORTS_STR!] (both Okta-registered)"
)

REM -- 3. Stale endpoint / orphan server cleanup ------------------------------
REM Replaces the old "netstat | findstr :<port> | taskkill" heuristic, which
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
REM   SSO port handling (auth.enabled=true):
REM     Okta accepts TWO registered loopback redirect_uris —
REM     ``http://localhost:8989/callback`` and ``http://localhost:28688/callback``.
REM     Either bound port yields a valid SSO round-trip; any other port fails
REM     with "redirect_uri mismatch".
REM
REM     * ``PORT_ARG`` = ``--port <n>`` when the user pinned via
REM       ``Start.bat --port <8989|28688>`` (validated at the top of this
REM       script).
REM     * ``PORT_ARG`` is empty when the user did not pin: the supervisor
REM       auto-probes the ``fallbacks`` list from factory\config\ports.json
REM       (currently ``[8989, 28688]``) and binds the first free port —
REM       so a busy 8989 recovers into 28688 without breaking SSO.
REM
REM     ``PORT_ARG`` is placed BEFORE %PASS_ARGS% so a caller who bypassed
REM     our validation by injecting a raw ``--port`` inside a quoted arg
REM     block still gets the last-occurrence semantics from argparse; the
REM     validated flag we authored wins by default.
echo [INFO] Launching server: !PORT_ANNOUNCE!
echo [INFO] Keep this window open. Close it (or press Ctrl+C) to stop the server.
call "%PYTHON%" -m apps.cli.serve !PORT_ARG! %PASS_ARGS%

endlocal
exit /b 0
