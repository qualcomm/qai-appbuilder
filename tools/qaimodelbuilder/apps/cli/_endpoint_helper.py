# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Launcher helper — wait for runtime endpoint, open browser, clean stale state.

This is a tiny CLI used by ``Start.bat`` (and conceptually by any
launcher / desktop shell) to bridge the gap between supervisor startup
and "user is looking at the SPA in their browser".

Why a separate helper rather than inlining in ``Start.bat``:

* The endpoint port is no longer hard-coded in any launcher (see
  ``apps/cli/serve.py`` ``FALLBACK_PORTS`` and
  ``apps/api/_runtime_endpoint.py``); the launcher must *read* it from
  the runtime endpoint file. Doing that robustly in batch + PowerShell
  one-liners is brittle (JSON parsing, polling loop, cross-platform).
* Same logic is reused by ``cleanup-stale`` to reap a previous server
  before a fresh start. It runs two complementary mechanisms: (1) an
  authoritative, endpoint-file-independent sweep of every fallback port
  (``apps/cli/serve.py:FALLBACK_PORTS``) that terminates whatever is
  LISTENING — this is what guarantees a fresh ``Start.bat`` always wins
  even after the supervisor started auto-picking a *different* fallback
  port on a collision (the regression that left two servers running
  side by side); and (2) endpoint-file PID housekeeping for a process
  that died without clearing its file (uncatchable kill, BSOD,
  task-manager force-stop). This replaces the old, now-wrong
  ``netstat | findstr :8989 | taskkill`` heuristic that hard-coded a
  single static port.

Subcommands::

    python -m apps.cli._endpoint_helper cleanup-stale [--data-root PATH]
    python -m apps.cli._endpoint_helper wait-and-open [--timeout 30] [--data-root PATH]
    python -m apps.cli._endpoint_helper print-url    [--timeout 30] [--data-root PATH]

All three exit 0 on success, non-zero on failure. ``Start.bat`` ignores
the exit code (helpers must never block the user from launching) but
returning meaningful codes makes the helpers testable and useful in
diagnostic scripts.
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

from apps.api._runtime_endpoint import (
    clear_endpoint,
    endpoint_path,
    read_endpoint,
)
from apps.cli.serve import FALLBACK_PORTS


# Default polling interval / total timeout for "wait for endpoint to appear".
# Server cold-start on a fresh install can take 5-10 s (DB migrations,
# factory model seed, sticky worker spawn), so the default timeout has
# to be generous; 30 s comfortably covers a slow first boot.
_DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.25

# Require the readiness probe to succeed this many times IN A ROW before
# opening the browser. One success only proves "accepting at this instant";
# consecutive successes across the poll gap prove the event loop is stably
# free to serve the burst of lazy chunk requests the SPA fires on open (see
# ``_wait_for_url`` phase 3 — the fix for the cold-start ``ERR_NETWORK_CHANGED``
# chunk-burst race).
_REQUIRED_CONSECUTIVE_PROBES = 2


def _default_data_root() -> Path:
    """Locate ``<repo_root>/data`` from this file's location.

    ``apps/cli/_endpoint_helper.py`` is two levels under the repo root.
    Mirrors ``apps/api/main.py:_detect_repo_root`` so launcher and API
    agree on what "data root" means without env vars.
    """

    return Path(__file__).resolve().parents[2] / "data"


def _is_pid_alive(pid: int) -> bool:
    """Return True iff a process with ``pid`` is still running.

    Cross-platform best-effort: signal-0 on POSIX, OpenProcess on
    Windows. Errors err on the side of "alive" so we do NOT
    accidentally label a still-running server as stale and kill it.
    """

    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:  # pragma: no cover
            return True
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # ERROR_INVALID_PARAMETER = 87 means PID does not exist.
            # Any other error (access denied) means it does — we are
            # conservatively treating that as alive.
            err = kernel32.GetLastError()
            return err != 87
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    else:
        import os as _os

        try:
            _os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def _parent_pid(pid: int) -> int | None:
    """Return the parent PID of ``pid`` on Windows, or ``None`` if unknown.

    Why this exists
    ---------------
    ``Start.bat`` does NOT launch the API server directly — it launches
    the *supervisor* (``python -m apps.cli.serve``), which in turn spawns
    the real API child (``python -m apps.api``). Only the **child** binds
    a port, so the port sweep below finds the child's PID. Killing only
    the child is not enough: the supervisor parent observes the child
    exit with a non-zero (signal-killed) code and, per its crash
    self-heal logic (``apps/cli/serve.py`` ``_run_loop``), immediately
    respawns a fresh API child — so the "stopped" server appears to
    restart itself. To guarantee "a fresh Start.bat always wins" we must
    also reap the supervisor parent; this helper walks the parent chain
    so :func:`_sweep_fallback_ports` can include it in the kill set.

    Implemented with the CIM/WMI ``ParentProcessId`` property via
    ``wmic`` (present on all supported Windows builds; falls back to a
    PowerShell ``Get-CimInstance`` query if ``wmic`` is absent on newer
    Windows where it is being deprecated). Returns ``None`` on any
    failure so the caller degrades to killing just the child (no worse
    than the previous behaviour).
    """

    if sys.platform != "win32":
        return None

    import subprocess

    # Primary: wmic (fast, ubiquitous on Win10/11 today).
    try:
        result = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "ParentProcessId",
                "/value",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.upper().startswith("PARENTPROCESSID="):
                value = line.split("=", 1)[1].strip()
                ppid = int(value)
                return ppid if ppid > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    # Fallback: PowerShell CIM query (wmic is deprecated on newer builds).
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "(Get-CimInstance Win32_Process -Filter "
                    f"\"ProcessId={pid}\").ParentProcessId"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        value = result.stdout.strip()
        if value:
            ppid = int(value)
            return ppid if ppid > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    return None


def _is_supervisor_pid(pid: int) -> bool:
    """Return ``True`` iff ``pid`` looks like our reboot supervisor.

    Guards :func:`_sweep_fallback_ports` from walking the parent chain
    into an unrelated process (e.g. the ``cmd.exe`` / terminal that
    launched ``Start.bat``, or some other tool that happens to be an
    ancestor). We only want to escalate the kill to the parent when the
    parent is genuinely ``python -m apps.cli.serve``.

    Matches on the process command line containing ``apps.cli.serve``.
    Returns ``False`` on any lookup failure so we never kill an
    ancestor we are not certain about.
    """

    if sys.platform != "win32":
        return False

    import subprocess

    # Try wmic first, then PowerShell, mirroring _parent_pid.
    cmdline = ""
    try:
        result = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "CommandLine",
                "/value",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("COMMANDLINE="):
                cmdline = stripped.split("=", 1)[1]
                break
    except (OSError, subprocess.SubprocessError):
        cmdline = ""

    if not cmdline:
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "(Get-CimInstance Win32_Process -Filter "
                        f"\"ProcessId={pid}\").CommandLine"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            cmdline = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            cmdline = ""

    return "apps.cli.serve" in cmdline


def _kill_pid(pid: int) -> bool:
    """Best-effort terminate ``pid`` and its descendants. Returns True iff accepted.

    On Windows we use ``taskkill /F /T`` so the **entire process tree**
    rooted at ``pid`` is terminated in one call. This matters when
    ``pid`` is the supervisor (``apps.cli.serve``): ``/T`` reaps the
    supervisor *and* its spawned API child together, so the child cannot
    be left behind and the supervisor cannot survive to respawn it. When
    ``pid`` is a leaf (e.g. an orphaned API child with no live
    supervisor), ``/T`` simply has no descendants to also kill and
    behaves like a plain ``/F``.
    """

    if sys.platform == "win32":
        import subprocess

        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        import os as _os
        import signal as _signal

        try:
            _os.kill(pid, _signal.SIGTERM)
            return True
        except (OSError, ProcessLookupError):
            return False


# ---------------------------------------------------------------------------
# Port-based sweep (V1 ``start_server.py`` ``_check_port`` / ``_kill_port``
# parity, generalised to the whole fallback-port list).
# ---------------------------------------------------------------------------


def _pids_listening_on_ports(ports: "tuple[int, ...]") -> set[int]:
    """Return the PIDs of processes LISTENING on any of ``ports`` (Windows).

    Why this exists
    ---------------
    Before the supervisor learned to auto-pick a fallback port (see
    ``apps/cli/serve.py:FALLBACK_PORTS``), the launcher could free the
    single fixed port and a fresh start always replaced the old server.
    Now a previous run may be LISTENING on *any* of the fallback ports
    (e.g. it bound 8088 because 8989 was reserved), and a new start, far
    from killing it, deliberately *avoids* that port and picks another —
    so two servers end up running side by side. To guarantee "a fresh
    Start.bat always wins" (the user-confirmed policy) we must reap every
    previous instance regardless of which fallback port it grabbed.

    This is the State-Truth-First rule (AGENTS.md §3.10 rule 1): the
    authoritative answer to "who owns this port" comes from the OS, not
    from the (possibly stale / missing) endpoint file. We parse
    ``netstat -ano`` — the same mechanism V1's ``_kill_port`` used — and
    keep only rows in the ``LISTENING`` state (TIME_WAIT / ESTABLISHED
    sockets are transient and must not trigger a kill).

    Non-Windows: ``netstat -ano`` is Windows-specific; returns an empty
    set so the caller falls back to the PID path (the launcher is a
    Windows-only entry point in practice, but we keep it from raising).
    """

    if sys.platform != "win32":
        return set()

    import subprocess

    wanted = {f":{p}" for p in ports}
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    pids: set[int] = set()
    for line in result.stdout.splitlines():
        # Only LISTENING rows own the port for binding purposes; ignore
        # TIME_WAIT / ESTABLISHED so we never kill a process merely
        # holding a transient connection to one of our ports.
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[1]  # e.g. ``0.0.0.0:8088`` / ``127.0.0.1:8989``
        # Match the ``:<port>`` suffix exactly so ``:8989`` does not also
        # match ``:18989`` (substring trap).
        if not any(local_addr.endswith(suffix) for suffix in wanted):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid <= 4:  # skip System / Idle (PID 0 / 4)
            continue
        pids.add(pid)
    return pids


def _kill_targets_for_listeners(listening_pids: "set[int]") -> set[int]:
    """Expand listening PIDs to the set of PIDs that must actually be killed.

    For each process LISTENING on a fallback port (the API *child*), walk
    up the parent chain looking for the reboot supervisor
    (``python -m apps.cli.serve``). If found, the supervisor PID replaces
    the child in the kill set — killing the supervisor with ``/T`` reaps
    the child too AND prevents the supervisor from respawning a fresh
    child (its crash self-heal). If no supervisor ancestor is found (an
    orphaned child, or the supervisor already gone), the child PID stays
    in the set so we still kill the leaf.

    We bound the walk to a few hops so a pathological parent chain can
    never loop forever. On non-Windows the parent walk is a no-op and the
    listening PIDs pass through unchanged.
    """

    targets: set[int] = set()
    for pid in listening_pids:
        supervisor = _find_supervisor_ancestor(pid)
        targets.add(supervisor if supervisor is not None else pid)
    return targets


def _find_supervisor_ancestor(pid: int, *, max_hops: int = 4) -> int | None:
    """Walk up from ``pid`` and return the supervisor ancestor PID, else ``None``.

    Stops at the first ancestor whose command line contains
    ``apps.cli.serve``. Bounded by ``max_hops`` so a cyclic / very deep
    chain cannot wedge the launcher.
    """

    current = pid
    seen: set[int] = {pid}
    for _ in range(max_hops):
        parent = _parent_pid(current)
        if parent is None or parent <= 4 or parent in seen:
            return None
        if _is_supervisor_pid(parent):
            return parent
        seen.add(parent)
        current = parent
    return None


def _sweep_fallback_ports(ports: "tuple[int, ...]") -> int:
    """Terminate every previous server LISTENING on any of ``ports``.

    Returns the number of process trees terminated.

    The processes actually killed are NOT just the ones holding the
    sockets: ``Start.bat`` launches the *supervisor*
    (``apps.cli.serve``), which spawns the API *child* that binds the
    port. Killing only the child lets the supervisor respawn it (crash
    self-heal), so the "stopped" server appears to restart itself. We
    therefore escalate each listening child to its supervisor ancestor
    (see :func:`_kill_targets_for_listeners`) and kill that whole tree
    with ``taskkill /F /T``.

    Idempotent: when no process is listening (the common clean case) it
    does nothing and returns 0.
    """

    listening = _pids_listening_on_ports(ports)
    targets = _kill_targets_for_listeners(listening)
    killed = 0
    for pid in targets:
        print(
            f"[endpoint-helper] terminating previous server tree pid={pid} "
            f"(supervisor and/or listener on a fallback port)...",
            flush=True,
        )
        if _kill_pid(pid):
            killed += 1
        else:
            print(
                f"[endpoint-helper] WARN: failed to kill pid={pid}; "
                "continuing anyway.",
                flush=True,
            )
    if killed:
        # ``taskkill /F /T`` returns before the OS has fully torn the
        # tree down and released its listening socket. Wait (bounded)
        # until the swept ports are no longer LISTENING, so the
        # supervisor's subsequent ``_resolve_bindable_port`` probe sees
        # the freed default port (4099 when ``Start.bat`` pins it via
        # ``--port``; otherwise the first bindable entry of
        # ``FALLBACK_PORTS``) instead of racing the teardown and picking
        # yet another fallback — which would re-create the very "two
        # servers running" symptom we are fixing.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not _pids_listening_on_ports(ports):
                break
            time.sleep(0.2)
    return killed


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_cleanup_stale(data_root: Path) -> int:
    """Reap any previous server before a fresh start (a new start always wins).

    Two complementary mechanisms, run in order:

    1. **Port sweep (authoritative).** Scan every fallback port (see
       ``apps/cli/serve.py:FALLBACK_PORTS``) and terminate whatever is
       LISTENING on it. This is endpoint-file-independent and is the
       fix for "the old server is not stopped": once the supervisor
       gained the ability to auto-pick a *different* fallback port on a
       collision, a stale instance on (say) 8088 was never reaped — the
       new run just avoided that port and both ran at once. Sweeping all
       fallback ports guarantees the previous instance dies regardless of
       which port it grabbed or whether its endpoint file is accurate.
    2. **Endpoint-file PID cleanup (housekeeping).** Even after the port
       sweep, clear the recorded ``server.endpoint.json`` (and kill its
       PID if — unusually — it is still alive but was not caught by the
       port scan, e.g. it crashed mid-bind and holds no listening socket).

    Idempotent — succeeds (exit 0) on a clean machine (nothing listening,
    no endpoint file) and on every partial-state combination.
    """

    # --- 1. Authoritative port sweep (independent of the endpoint file) ---
    killed = _sweep_fallback_ports(FALLBACK_PORTS)
    if killed:
        print(
            f"[endpoint-helper] reaped {killed} previous server "
            f"instance(s) listening on a fallback port.",
            flush=True,
        )

    # --- 2. Endpoint-file housekeeping ------------------------------------
    info = read_endpoint(data_root)
    if info is None:
        # No recorded endpoint — the port sweep (if any) is all that was
        # needed. Common clean-start case.
        return 0

    pid = info.get("pid")
    url = info.get("url", "<unknown>")
    if not isinstance(pid, int) or pid <= 0:
        # Endpoint file present but unparseable — sweep it anyway.
        clear_endpoint(data_root)
        print(
            f"[endpoint-helper] removed endpoint file with invalid pid "
            f"(url={url}).",
            flush=True,
        )
        return 0

    if not _is_pid_alive(pid):
        # The server died (or was just killed by the port sweep) without
        # clearing its endpoint. Sweep the file.
        if clear_endpoint(data_root):
            print(
                f"[endpoint-helper] cleared stale endpoint "
                f"(pid {pid} dead, url={url}).",
                flush=True,
            )
        return 0

    # PID still alive but the port sweep did not catch it (e.g. it crashed
    # mid-bind and holds no listening socket, or it bound a non-fallback
    # port). Kill it too — a fresh ``Start.bat`` always wins. Escalate to
    # the supervisor ancestor first (same reason as the port sweep): the
    # recorded PID is the API *child*; killing only it lets the supervisor
    # respawn a fresh one. ``_kill_pid`` uses ``taskkill /F /T`` so killing
    # the supervisor reaps the child with it.
    supervisor = _find_supervisor_ancestor(pid)
    target = supervisor if supervisor is not None else pid
    print(
        f"[endpoint-helper] terminating previous server tree pid={target} "
        f"(endpoint child pid={pid}, url={url})...",
        flush=True,
    )
    if not _kill_pid(target):
        print(
            f"[endpoint-helper] WARN: failed to kill pid={target}; "
            "continuing anyway.",
            flush=True,
        )
    # Give the process a moment to clear its endpoint file via lifespan
    # shutdown. If it does not (hard kill), sweep the file ourselves.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not endpoint_path(data_root).exists():
            return 0
        time.sleep(0.1)
    clear_endpoint(data_root)
    return 0


def cmd_wait_and_open(data_root: Path, timeout: float) -> int:
    """Block until ``server.endpoint.json`` appears, then open the URL in a browser.

    Returns 0 on success (URL opened or already-open browser focused),
    1 on timeout — non-fatal: the server is still starting and the
    user can navigate manually.
    """

    url = _wait_for_url(data_root, timeout)
    if url is None:
        print(
            f"[endpoint-helper] timed out after {timeout:.0f}s waiting for "
            f"endpoint file under {data_root}; you can open the URL "
            "manually once the server window prints its bind address.",
            flush=True,
        )
        return 1
    open_url = url.rstrip("/") + "/chat"
    try:
        webbrowser.open(open_url, new=2)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[endpoint-helper] failed to open browser for {open_url}: {exc}; "
            "open it manually.",
            flush=True,
        )
        return 1
    print(f"[endpoint-helper] opened {open_url}", flush=True)
    return 0


def cmd_print_url(data_root: Path, timeout: float) -> int:
    """Print the API URL once the endpoint file appears (machine-readable).

    Used by scripts that want the URL but should not open a browser
    (CI smoke tests, programmatic clients). Exit 0 on success, 1 on
    timeout.
    """

    url = _wait_for_url(data_root, timeout)
    if url is None:
        return 1
    print(url)
    return 0


def _wait_for_url(data_root: Path, timeout: float) -> str | None:
    """Poll the endpoint file AND verify the server is *stably* serving before returning.

    Three-phase readiness check:

    1. Wait for ``server.endpoint.json`` to appear and parse — confirms
       the API process has completed lifespan startup and knows its
       bound port.
    2. HTTP GET the **health endpoint** (``/api/system/health``) until it
       responds — confirms uvicorn has finished bind+listen AND the app can
       actually service a request that touches the DB (a stronger readiness
       signal than the bare ``/`` static route, which can return ``index.html``
       while the event loop is still busy with lifespan-tail work).
    3. Require the health probe to succeed **twice in a row** (with a short
       gap) before returning — the browser, the moment it loads ``index.html``,
       fires a *burst* of lazily-loaded chunk requests (``locale-*.js`` etc.).
       During cold start the event loop is briefly monopolised by lifespan-tail
       work (voice warmup loading a model, the blocking internal usage-report
       upload to its configured endpoints); a single in-flight static request
       that lands in that window is cut short, surfacing in the browser as
       ``net::ERR_NETWORK_CHANGED`` for a whole batch of chunks (the user must
       then refresh). One health 200 only proves "accepting at this instant";
       two consecutive successes across a gap prove the loop is *stably* free,
       so the chunk burst that follows the browser open will not be starved.

    Phase 2/3 are critical because uvicorn's internal sequencing is:
    lifespan startup → endpoint file written (at yield) → "Application startup
    complete" → bind → accept → (lifespan-tail tasks still running). The
    endpoint file's presence does NOT guarantee the socket is accepting yet,
    and accepting does NOT guarantee the loop is free to serve a burst.

    This is the State-Truth-First rule (AGENTS.md §🔴 铁律 1/3): gate the browser
    open on the server's *real* ability to stably serve, not on the proxy signal
    "endpoint file exists" / "one request happened to succeed".

    Returns the URL string when the server is ready, or ``None`` on
    overall timeout.
    """

    deadline = time.monotonic() + max(0.0, timeout)

    # Phase 1: wait for the endpoint file.
    url: str | None = None
    while True:
        info = read_endpoint(data_root)
        if info is not None:
            candidate = info.get("url")
            if isinstance(candidate, str) and candidate:
                url = candidate
                break
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_INTERVAL_SECONDS)

    # Phase 2/3: probe the health endpoint until it responds twice in a row.
    probe_url = _health_probe_url(url)
    consecutive_ok = 0
    while True:
        if _http_probe(probe_url):
            consecutive_ok += 1
            if consecutive_ok >= _REQUIRED_CONSECUTIVE_PROBES:
                return url
        else:
            # A miss resets the streak — we want CONSECUTIVE successes, so a
            # blip in the lifespan-tail window must restart the count.
            consecutive_ok = 0
        if time.monotonic() >= deadline:
            # Health endpoint not stably ready in time. Return the URL anyway —
            # the browser may succeed by the time the user context-switches to
            # it, and that is better than refusing to open when the file exists
            # and the server is nearly ready.
            return url
        time.sleep(_POLL_INTERVAL_SECONDS)


def _health_probe_url(base_url: str) -> str:
    """Build the health-endpoint URL from the API base URL.

    The endpoint file records the SPA base (e.g. ``http://127.0.0.1:4099/``);
    the readiness probe targets ``/api/system/health`` (liveness + DB pragma
    snapshot) on the same origin. Tolerant of a trailing slash on the base.
    """

    return base_url.rstrip("/") + "/api/system/health"


def _http_probe(url: str) -> bool:
    """Return ``True`` iff a TCP connection + HTTP GET to ``url`` succeeds.

    Uses a very short timeout (1s) because we are polling in a loop;
    we just need to distinguish "connection refused" / "timed out" from
    "any HTTP response at all" (even 404 / 500 means the server is
    accepting traffic and the SPA will load).
    """

    try:
        import urllib.request

        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1):  # noqa: S310
            pass
        return True
    except Exception:  # noqa: BLE001 — any failure = not ready yet
        return False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.cli._endpoint_helper",
        description=(
            "Launcher helper — bridges Start.bat / desktop shell with the "
            "runtime endpoint file written by apps.api lifespan."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help=(
            "Path to the data/ directory. Defaults to <repo_root>/data "
            "computed from this file's location."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "cleanup-stale",
        help=(
            "Reap a previous server that died without clearing its "
            "endpoint file (idempotent)."
        ),
    )

    p_wait = sub.add_parser(
        "wait-and-open",
        help=(
            "Wait for the endpoint file to appear and open the URL in "
            "the default browser."
        ),
    )
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds to wait for the endpoint file. Defaults to "
            f"{_DEFAULT_TIMEOUT_SECONDS:.0f}."
        ),
    )

    p_print = sub.add_parser(
        "print-url",
        help=(
            "Print the API URL to stdout once the endpoint file appears "
            "(no browser)."
        ),
    )
    p_print.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds to wait for the endpoint file. Defaults to "
            f"{_DEFAULT_TIMEOUT_SECONDS:.0f}."
        ),
    )

    args = parser.parse_args(argv)
    data_root = args.data_root or _default_data_root()

    if args.cmd == "cleanup-stale":
        return cmd_cleanup_stale(data_root)
    if args.cmd == "wait-and-open":
        return cmd_wait_and_open(data_root, args.timeout)
    if args.cmd == "print-url":
        return cmd_print_url(data_root, args.timeout)
    return 2  # unreachable — argparse enforces choices


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
