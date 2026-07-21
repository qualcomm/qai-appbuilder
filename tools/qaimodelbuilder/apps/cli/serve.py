# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``apps.cli.serve`` — reboot supervisor (S7.5 PR-901, lane L9).

Parent-process reboot loop replacing the legacy ``start_server.py:597-636``
behaviour. The supervisor spawns ``python -m apps.api`` as a subprocess,
waits for it, and:

* if the child exits with ``REBOOT_EXIT_CODE = 75`` (v2.7 §3.1 contract),
  respawns the child;
* if the child exits with any other code, the supervisor exits with that
  same code (so a systemd / shell wrapper observes it as the final code);
* if the supervisor itself receives ``SIGINT`` / ``SIGTERM`` (Ctrl+C on
  POSIX, ``CTRL_BREAK_EVENT`` on Windows), it forwards a graceful stop
  signal to the child, waits ``GRACEFUL_TIMEOUT`` seconds for it to exit,
  then escalates to ``terminate()`` / ``kill()`` if needed.

CANCELLED scope (S7.5 lane L9 §6):

* P2-3 three-thread interactive Ctrl+C menu (the legacy
  ``start_server.py`` keystroke / arrow-key menu) — explicitly cancelled
  by the user; the supervisor simply forwards SIGINT for graceful stop.
* P2-5 PortableGit auto-install — out of scope; system git is assumed.

Out of scope (handled elsewhere):

* Port-busy auto-kill: the legacy ``_check_port`` / ``_kill_port`` netstat
  + taskkill heuristic is **not** ported. Modern uvicorn surfaces a clear
  ``[Errno 10048]`` / ``EADDRINUSE`` and the operator runs
  ``scripts/init/uninstall`` (PR-902) or stops the conflicting process.
* Browser auto-open: handled by the desktop launcher / first-run setup;
  not the supervisor's job.
* Hot reload: ``python -m apps.api --reload`` is forwarded to the child;
  uvicorn's reloader handles its own subprocesses.

Console script registration (PR-901 §10 hand-off to I1):

    [project.scripts]
    qai-serve = "apps.cli.serve:main"

Usage::

    python -m apps.cli.serve                # default host/port from settings
    python -m apps.cli.serve --host 0.0.0.0 --port 4099
    qai-serve --reload                      # after I1 wires the script

The supervisor passes all unknown arguments through to ``apps.api``.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence

from apps.cli._console_ctrl import (
    ConsoleCtrlInterceptor,
    show_exit_menu,
)
from qai.platform.net.port_allocator import (
    NoBindablePortError,
    PortInUseError,
)
from qai.platform.net.port_allocator import (
    can_bind as _shared_can_bind,
)
from qai.platform.net.port_allocator import (
    resolve_bindable_port as _shared_resolve_bindable_port,
)

# v2.7 §3.1 invariant — must equal the child-side
# ``apps.api._reboot_scheduler._RebootScheduler.exit_code`` default and
# the ``settings.server.reboot_exit_code`` default. Tests guard against
# drift (``tests/unit/qai/platform/test_config.py``).
REBOOT_EXIT_CODE = 75

# Time the supervisor waits for the child to honour a graceful stop
# signal before escalating to ``terminate()`` / ``kill()``. Widened from
# the legacy 8s window to 35s so a slow lifespan shutdown (draining chat
# SSE/WS turns, flushing logs, closing channel transports) can finish
# cleanly before escalation. Sits just above uvicorn's own
# ``timeout_graceful_shutdown=30`` (apps/api/main.py) so the child gets a
# chance to self-terminate gracefully before the supervisor forces it.
GRACEFUL_TIMEOUT_SECONDS = 35.0
TERMINATE_TIMEOUT_SECONDS = 3.0

# How often the supervisor's main loop wakes to check whether the child has
# exited or a Ctrl+C was intercepted. The main thread must NOT block in a bare
# ``proc.wait()`` on Windows, otherwise the Python SIGINT handler cannot run
# while the C-level wait holds the GIL (V1 ``start_server.py`` polled at 0.2s
# for the same reason; this is why a bare ``proc.wait()`` made Ctrl+C appear
# unresponsive).
POLL_INTERVAL_SECONDS = 0.2

# Maximum number of reboot cycles before the supervisor gives up and
# exits, to avoid a tight respawn loop if something is fundamentally
# broken (e.g. settings parser raising at startup). The legacy launcher
# had no such bound; we add one to make CI smoke tests deterministic.
MAX_REBOOT_CYCLES = 32

# Crash self-heal bound. The legacy ``start_server.py`` exited the launcher
# the moment the service crashed with any non-reboot code, permanently
# stopping the service until an operator intervened. We instead respawn on
# an abnormal crash so a transient fault (uncaught exception, OOM kill,
# signal) self-heals — but only up to ``MAX_CRASH_RESTARTS`` within a
# rolling ``CRASH_WINDOW_SECONDS`` window, so a child that crashes on every
# boot (e.g. a fatal config error) does not spin forever burning CPU. A
# user-initiated reboot (exit code 75) does NOT count toward this bound.
MAX_CRASH_RESTARTS = 5
CRASH_WINDOW_SECONDS = 300.0


# Default bind host when the operator did not pass ``--host`` explicitly.
# Mirrors :data:`qai.platform.config.settings.ServerSettings.host` /
# ``SecuritySettings.bind_host`` defaults so the supervisor can probe the
# same address the child will bind without importing the settings stack
# (which would pull pydantic / config loading into the supervisor).
DEFAULT_PROBE_HOST = "127.0.0.1"

# Default candidate ports tried (in order) when the operator did not pass
# ``--port`` explicitly. The first candidate that ``bind()`` accepts wins.
#
# Why a fallback list at all: on Windows, the OS dynamically reserves
# random TCP port ranges at boot (Hyper-V / WSL2 / Docker / WinNAT —
# visible via ``netsh int ipv4 show excludedportrange protocol=tcp``).
# Any port inside such a range fails ``bind()`` with ``WinError 10013``
# (``EACCES``) — NOT 10048 (``EADDRINUSE``) — even though no process is
# listening on it. The reserved ranges differ between machines and even
# between reboots of the same machine, so a single hard-coded default
# cannot be safe everywhere; the supervisor must probe.
#
# Selection criteria for the list:
#
# * 8989 first — legacy packaged default, retained here for backward
#   compatibility with existing dev workflows / CORS allow-list entries
#   (see ``qai.platform.config.settings.SecuritySettings.allowed_origins``
#   which still lists ``http://localhost:8989`` alongside 4099). NOTE:
#   this is NO LONGER the documented default port — the documented
#   default is ``ServerSettings.port = 4099`` (pinned by the Okta
#   redirect_uri ``http://localhost:4099/callback``). ``Start.bat`` passes
#   ``--port 4099`` explicitly so the SSO-critical bind path never
#   consults this fallback list. FALLBACK_PORTS only runs when ``--port``
#   is omitted (bare ``python -m apps.cli.serve``); the first entry is
#   deliberately left at 8989 to avoid silently changing that dev
#   scenario's behaviour.
# * Stay above 1024 (well-known) and below 49152 (ephemeral / dynamic
#   range that Windows itself draws from for outbound connections, and
#   that overlaps heavily with Hyper-V reservations).
# * Spread across multiple decades — if 8000-9000 is fully reserved, the
#   18000 / 28000 entries are very unlikely to collide.
# * Avoid widely-used dev ports (3000 / 5000 / 5173 / 8000 / 8080 / 8888)
#   so we do not collide with another local project the user is running.
#
# The first entry 8989 is paired with same-tail offsets (12989 / 18989 /
# 28989) so the fallback URL stays recognisable for users familiar with
# the legacy packaged default.
FALLBACK_PORTS: tuple[int, ...] = (8989, 8088, 7799, 12989, 18989, 28989)


SpawnCallable = Callable[[list[str]], "subprocess.Popen[bytes]"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Supervisor entry point. Returns the final exit code."""

    parser = _build_parser()
    args, child_args = parser.parse_known_args(argv)

    if args.no_supervisor:
        # Diagnostic mode — replace this process with the child so the
        # operator can inspect the API server directly without the
        # supervisor wrapping its lifetime. Useful when attaching a
        # debugger.
        #
        # We still resolve a bindable port up front so the diagnostic
        # path enjoys the same Hyper-V-reserved-range graceful fallback
        # as the supervised path; otherwise --no-supervisor would crash
        # with the raw WinError 10013 the supervisor exists to hide.
        probe_host = args.host or DEFAULT_PROBE_HOST
        try:
            resolved_port = _resolve_bindable_port(
                probe_host, requested=args.port
            )
        except RuntimeError as exc:
            _stderr(f"[serve] {exc}\n")
            return 1
        os.environ["QAI_RUNTIME_PORT"] = str(resolved_port)
        cmd = _build_child_command(
            child_args, host=args.host, port=resolved_port, reload=args.reload
        )
        os.execv(cmd[0], cmd)  # pragma: no cover — exec replaces process
        return 0  # unreachable

    runner = _Supervisor(
        child_args=child_args,
        host=args.host,
        port=args.port,
        reload=args.reload,
        max_cycles=args.max_reboots,
        graceful_timeout=args.graceful_timeout,
    )
    return runner.run()


# ---------------------------------------------------------------------------
# Supervisor implementation
# ---------------------------------------------------------------------------


class _Supervisor:
    """Spawns ``python -m apps.api``, waits, restarts on exit-code 75.

    Split out from :func:`main` so tests can construct it with an
    injected ``spawn_callable`` and avoid actually launching uvicorn.
    """

    def __init__(
        self,
        *,
        child_args: Sequence[str],
        host: str | None,
        port: int | None,
        reload: bool,
        max_cycles: int = MAX_REBOOT_CYCLES,
        graceful_timeout: float = GRACEFUL_TIMEOUT_SECONDS,
        max_crash_restarts: int = MAX_CRASH_RESTARTS,
        crash_window_seconds: float = CRASH_WINDOW_SECONDS,
        spawn_callable: SpawnCallable | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        install_signal_handlers: bool = True,
    ) -> None:
        self._child_args = list(child_args)
        self._host = host
        self._port = port
        self._reload = reload
        self._max_cycles = max_cycles
        self._graceful_timeout = graceful_timeout
        self._max_crash_restarts = max_crash_restarts
        self._crash_window_seconds = crash_window_seconds
        self._spawn_callable = spawn_callable or _default_spawn
        self._monotonic = monotonic
        self._install_handlers = install_signal_handlers

        # Mutated by signal handlers + the spawn loop.
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop_requested = threading.Event()
        self._stop_lock = threading.Lock()
        # Ctrl+C (Windows console handler) sets this; the main loop drains it
        # to show the interactive exit menu. Separate from ``_stop_requested``
        # because Ctrl+C may be answered with "keep running".
        self._ctrl_c_event = threading.Event()
        self._menu_lock = threading.Lock()
        self._console_ctrl = ConsoleCtrlInterceptor(
            self._on_console_ctrl_c,
            on_close=self._on_console_close,
        )
        # Set once the user (or a signal) has decided to exit, so the main
        # loop stops respawning and breaks out.
        self._exit_after_child = False

        # ── Orphan-kill rail 1: Job Object (AGENTS.md 🔴 铁律 5) ──────────────
        # State-Truth-First 铁律 5 (异常退出路径必须兜底): a spawned worker must
        # have an OS-level fallback that reaps it even when THIS supervisor is
        # force-killed (Task Manager / power loss / the launcher window closed
        # ungracefully) and never runs its graceful ``_await_child_after_stop``.
        # We hold a ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` job for the
        # supervisor's lifetime and assign each spawned worker to it: when the
        # supervisor dies for ANY reason the kernel closes the last job handle
        # and terminates every worker still in it. This is orthogonal to the
        # reboot (exit 75) / crash-restart loops — during those the supervisor
        # stays alive, the job stays open, and the next worker is assigned to
        # the SAME job. Best-effort: a failed job creation degrades to the
        # graceful path alone (never blocks startup). Lazy import keeps the
        # supervisor's import surface minimal + cross-platform (no-op off
        # Windows). See ``qai.platform.process.kill_group``.
        self._kill_group: object | None = None
        try:
            from qai.platform.process.kill_group import ProcessKillGroup

            self._kill_group = ProcessKillGroup()
        except Exception:  # noqa: BLE001 — orphan rail must never block boot
            self._kill_group = None

    # ----- lifecycle -----------------------------------------------------

    def run(self) -> int:
        if self._install_handlers:
            self._install_signal_handlers()
            # On Windows, also intercept Ctrl+C at the console layer so the
            # main thread (which polls below rather than blocking in wait())
            # can show the interactive exit menu and cmd.exe does not pop the
            # "Terminate batch job (Y/N)?" prompt. No-op on POSIX.
            self._console_ctrl.install()
        code = self._run_loop()
        # V1 parity (start_server.py:362,666): when the user chose to exit via
        # the Ctrl+C menu, terminate the process with ``os._exit(0)`` — a
        # deliberate user-initiated stop is a *normal* exit (V1 uses
        # ``_exit_process(0)``), NOT the child's signal-killed code (uvicorn
        # exits non-zero when terminated by CTRL_BREAK_EVENT). ``os._exit`` also
        # bypasses cmd.exe's batch-interrupt check so the launching .bat does
        # not pop a second "Terminate batch job (Y/N)?" prompt. Only done for
        # the real run (handlers installed); tests use ``return`` so they never
        # kill the test process.
        if self._install_handlers and self._exit_after_child:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except OSError:
                pass
            os._exit(0)
        return code

    def _run_loop(self) -> int:
        cycles = 0
        # Timestamps (monotonic) of recent abnormal crashes, pruned to the
        # rolling ``CRASH_WINDOW_SECONDS`` window. Reboots (code 75) and
        # clean exits (code 0) never append here.
        crash_times: list[float] = []
        # Whether the operator passed ``--port`` explicitly. We capture it
        # ONCE at loop entry so a successful auto-pick on cycle 1 does
        # not get re-locked as "explicit" on cycle 2 (re-probing must
        # still walk the fallback list if the previous pick is gone).
        explicit_port = self._port
        # The host we probe against. Empty / None ``--host`` means use the
        # documented loopback default; we deliberately do NOT import
        # ``qai.platform.config.settings`` from the supervisor to keep
        # its import surface minimal (the child still loads settings and
        # honours bind_host fully — see ``apps/api/main.py``).
        probe_host = self._host or DEFAULT_PROBE_HOST
        while True:
            cycles += 1
            if cycles > self._max_cycles:
                _stderr(
                    f"[supervisor] max reboot cycles ({self._max_cycles}) "
                    "reached; giving up.\n"
                )
                return 1

            # Resolve a bindable port BEFORE building / spawning the
            # child. Doing this every cycle (rather than once at startup)
            # is intentional: between crashes the OS state can change —
            # another process may have grabbed the port, or Windows
            # may have re-shuffled its excluded ranges. Re-probing per
            # cycle keeps the supervisor self-healing.
            try:
                resolved_port = _resolve_bindable_port(
                    probe_host, requested=explicit_port
                )
            except RuntimeError as exc:
                _stderr(f"[supervisor] {exc}\n")
                return 1
            if explicit_port is None and resolved_port != FALLBACK_PORTS[0]:
                # Auto-fallback path: surface clearly so the operator
                # knows the actual URL (it differs from any hard-coded
                # value in docs / launchers). The endpoint file written
                # by ``apps.api.lifespan`` is the durable source of truth
                # for downstream consumers (Start.bat browser auto-open,
                # Uninstall.bat reboot ping); this stderr line is for
                # the human watching the console.
                _stderr(
                    f"[supervisor] default port {FALLBACK_PORTS[0]} not "
                    f"bindable on {probe_host}; auto-selected fallback "
                    f"port {resolved_port}.\n"
                )
            self._port = resolved_port

            # Inject the resolved port into the environment so the child
            # process (and specifically ``apps.api.lifespan``) can read the
            # ACTUAL port the supervisor chose — even when the FastAPI app
            # factory re-loads Settings without the CLI ``--port`` override
            # (which happens under ``uvicorn.run(factory=True)`` because
            # ``_create_app_for_uvicorn()`` → ``create_app()`` calls
            # ``load_settings()`` fresh, without the CLI overrides that
            # ``main()`` applied). Without this, ``container.settings.
            # server.port`` would still be the *default* (4099) while
            # uvicorn actually bound the supervisor-selected fallback port
            # (e.g. 8088). The lifespan reads this env var to write the
            # correct URL in the runtime endpoint file.
            os.environ["QAI_RUNTIME_PORT"] = str(resolved_port)

            cmd = _build_child_command(
                self._child_args,
                host=self._host,
                port=self._port,
                reload=self._reload,
            )
            try:
                self._proc = self._spawn_callable(cmd)
            except OSError as exc:
                _stderr(f"[supervisor] failed to spawn child: {exc}\n")
                return 1

            # Orphan-kill rail 1: assign the freshly-spawned worker to the
            # KILL_ON_JOB_CLOSE job so a hard supervisor death reaps it.
            # Best-effort + idempotent across reboot/crash respawns (each new
            # worker joins the same job). No-op when the job is unavailable.
            self._assign_to_kill_group(self._proc.pid)

            if cycles == 1:
                _stderr(
                    f"[supervisor] api server started (pid={self._proc.pid}).\n"
                )
            else:
                _stderr(
                    f"[supervisor] api server restarted "
                    f"(cycle {cycles}, pid={self._proc.pid}).\n"
                )

            exit_code = self._wait_child()

            # Stop requested by the supervisor itself (Ctrl+C / signal / menu
            # "Yes"). Forward the child's exit code so the wrapper sees it.
            if self._stop_requested.is_set():
                _stderr(
                    f"[supervisor] shutdown complete "
                    f"(child exit={exit_code}).\n"
                )
                return _normalise_exit_code(exit_code, default=0)

            if exit_code == REBOOT_EXIT_CODE:
                _stderr(
                    f"[supervisor] reboot signal received "
                    f"(exit={REBOOT_EXIT_CODE}); respawning.\n"
                )
                continue

            # Clean exit (code 0) — a deliberate, graceful stop (e.g. the
            # desktop shell window closing requests exit code 0 via
            # ``POST /api/system/exit``). Do NOT restart; forward the code.
            normalised = _normalise_exit_code(exit_code, default=0)
            if normalised == 0:
                _stderr("[supervisor] api server exited cleanly (code=0).\n")
                return 0

            # Any other code = an abnormal crash (uncaught exception,
            # signal-kill, OOM, etc.). The legacy launcher exited the
            # moment the service crashed, permanently stopping it; we
            # instead self-heal by respawning so a transient fault
            # recovers. Bounded by ``max_crash_restarts`` within a rolling
            # ``crash_window_seconds`` window so a child that crashes on
            # every boot (e.g. a fatal config error) does not spin forever.
            now = self._monotonic()
            crash_times.append(now)
            crash_times[:] = [
                t for t in crash_times
                if now - t <= self._crash_window_seconds
            ]
            if len(crash_times) > self._max_crash_restarts:
                _stderr(
                    f"[supervisor] api server crashed ({_describe_exit_code(exit_code)}); "
                    f"{len(crash_times)} crashes within "
                    f"{int(self._crash_window_seconds)}s exceeds the limit "
                    f"of {self._max_crash_restarts} — giving up. Check the "
                    "server logs and restart manually once fixed.\n"
                )
                return _normalise_exit_code(exit_code, default=1)
            _stderr(
                f"[supervisor] api server crashed ({_describe_exit_code(exit_code)}); "
                f"restarting (crash {len(crash_times)}/"
                f"{self._max_crash_restarts} within "
                f"{int(self._crash_window_seconds)}s).\n"
            )
            continue

    # ----- child wait / stop --------------------------------------------

    def _wait_child(self) -> int:
        """Wait for the child, polling so Ctrl+C stays responsive.

        The main thread must not block in a bare ``proc.wait()`` on Windows:
        the Win32 console Ctrl+C handler fires on a separate OS thread and
        sets ``_ctrl_c_event``; this poll loop drains it to show the exit
        menu. (V1 ``start_server.py:598-604`` polled at 0.2s for the same
        reason.) If the platform still routes a ``KeyboardInterrupt`` to us
        (POSIX without the console handler), we treat it as a stop request.
        """

        proc = self._proc
        assert proc is not None, "child process must be spawned"
        try:
            while True:
                # Drain a pending Ctrl+C (Windows console handler) → menu.
                if self._ctrl_c_event.is_set():
                    self._ctrl_c_event.clear()
                    self._handle_ctrl_c()
                # If a stop was requested (menu "Yes" / signal), wait out the
                # graceful window and escalate if needed.
                if self._stop_requested.is_set():
                    return self._await_child_after_stop()
                try:
                    return proc.wait(timeout=POLL_INTERVAL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            # Defensive fallback — the platform routed KeyboardInterrupt to
            # the main thread instead of our handler (e.g. POSIX without the
            # console interceptor). Treat as a graceful stop.
            self._request_stop("KeyboardInterrupt")
            return self._await_child_after_stop()

    def _handle_ctrl_c(self) -> None:
        """React to an intercepted Ctrl+C: show the Yes/No exit menu.

        Only one menu at a time (a mashed Ctrl+C while the menu is up is
        ignored). "Yes" requests a graceful stop; "No" keeps the server
        running and tells the user how to stop later.
        """

        if not self._menu_lock.acquire(blocking=False):
            return
        try:
            want_exit = show_exit_menu()
        finally:
            self._menu_lock.release()
        if want_exit:
            self._exit_after_child = True
            self._request_stop("ctrl-c menu: exit")
            _stderr("\n[supervisor] stopping server...\n")
        else:
            _stderr(
                "\n[supervisor] server is still running. "
                "Press Ctrl+C again to stop.\n\n"
            )

    def _await_child_after_stop(self) -> int:
        proc = self._proc
        if proc is None:
            return 0
        try:
            return proc.wait(timeout=self._graceful_timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                return proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    return proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    return 1

    # ----- signal handling ----------------------------------------------

    def _install_signal_handlers(self) -> None:
        # SIGINT (Ctrl+C) and SIGTERM are universal. SIGBREAK only
        # exists on Windows; we install it best-effort.
        try:
            signal.signal(signal.SIGINT, self._on_signal)
        except (AttributeError, ValueError):  # pragma: no cover
            pass
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
        except (AttributeError, ValueError):  # pragma: no cover
            pass
        if hasattr(signal, "SIGBREAK"):  # pragma: no cover - Windows-only
            try:
                signal.signal(signal.SIGBREAK, self._on_signal)
            except (AttributeError, ValueError):
                pass

    def _on_signal(self, signum: int, _frame: object) -> None:
        # When the Windows console interceptor is active it owns the Ctrl+C
        # UX (interactive menu). Route SIGINT through the same event so we do
        # not bypass the menu with an immediate stop. SIGTERM (and POSIX
        # without the interceptor) still stops directly.
        if self._console_ctrl.active and signum == getattr(
            signal, "SIGINT", None
        ):
            self._ctrl_c_event.set()
            return
        self._request_stop(f"signal:{signum}")

    def _on_console_ctrl_c(self) -> None:
        """Win32 console handler callback (runs on a dedicated OS thread).

        Just flags the event; the main poll loop drains it and shows the menu
        on the main thread (console IO must not happen on the handler thread).
        """

        self._ctrl_c_event.set()

    def _on_console_close(self) -> None:
        """Win32 console CLOSE/LOGOFF/SHUTDOWN handler (dedicated OS thread).

        The console window is going away (user closed it / OS logoff/shutdown).
        Unlike Ctrl+C there is NO interactive menu — we must stop the worker
        immediately. Request the graceful stop (forwards CTRL_BREAK to the
        worker) and BLOCK briefly until the worker has actually exited, because
        once this handler returns Windows may terminate the supervisor — and we
        want the worker torn down first rather than orphaned. The Job Object
        (rail 1) is the final backstop: whatever is left dies when this
        supervisor process exits. Best-effort + bounded wait so we never hang
        the OS shutdown sequence.

        Runs on the handler OS thread (NOT the main thread), so it must not
        touch console IO; it only signals + polls ``proc``.
        """
        self._request_stop("console-close")
        proc = self._proc
        if proc is None:
            return
        # Bounded wait: Windows grants a console process ~5s after
        # CTRL_CLOSE_EVENT before hard-killing it; stay under that so we don't
        # get killed mid-teardown. Escalate to terminate()/kill() if the worker
        # ignores the graceful CTRL_BREAK within the window.
        deadline = self._monotonic() + 4.0
        try:
            while self._monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.1)
            # Still alive after the grace window — force it down so it is not
            # orphaned when the OS reaps this supervisor.
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 — handler must never raise
            pass

    def _request_stop(self, reason: str) -> None:
        # Idempotent. Multiple SIGINTs (the user may mash Ctrl+C) only
        # escalate the urgency: the first call asks the child politely;
        # the second within the graceful window kills it immediately.
        with self._stop_lock:
            already = self._stop_requested.is_set()
            self._stop_requested.set()
        if not already:
            _stderr(
                f"[supervisor] stop requested ({reason}); "
                "forwarding to child.\n"
            )
            self._forward_stop_to_child()
        else:
            _stderr(
                f"[supervisor] second stop received ({reason}); "
                "killing child.\n"
            )
            self._kill_child_now()

    def _forward_stop_to_child(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        # On Windows, children spawned with CREATE_NEW_PROCESS_GROUP
        # ignore Ctrl+C from the parent's console; we must send
        # CTRL_BREAK_EVENT explicitly. On POSIX, SIGTERM gives uvicorn
        # a chance to run lifespan shutdown hooks before exiting.
        #
        # ⚠️ Do NOT "prefer" SIGTERM on Windows: the child is spawned with
        # ``CREATE_NEW_PROCESS_GROUP`` (see ``_default_spawn``), and on
        # Windows ``Popen.send_signal(SIGTERM)`` maps to ``TerminateProcess``
        # — a hard kill that bypasses uvicorn's graceful lifespan shutdown.
        # ``CTRL_BREAK_EVENT`` is the only signal that reaches the child's
        # console handler and lets uvicorn drain in-flight turns / run
        # shutdown hooks, so it stays the Windows path.
        try:
            if sys.platform == "win32":
                # ``signal.CTRL_BREAK_EVENT`` is Windows-only.
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                proc.send_signal(signal.SIGTERM)
        except (OSError, ValueError) as exc:
            _stderr(f"[supervisor] failed to forward stop signal: {exc}\n")
            self._kill_child_now()

    def _kill_child_now(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        # Don't wait synchronously — the main thread is blocked in
        # ``proc.wait()`` and will return once the child is gone.

    def _assign_to_kill_group(self, pid: int) -> None:
        """Best-effort: add the worker ``pid`` to the KILL_ON_JOB_CLOSE job.

        Orphan-kill rail 1 (AGENTS.md 🔴 铁律 5). Never raises and never blocks:
        a failure just means the OS-level fallback is inactive and only the
        graceful stop path protects against orphans. The kill-group object is
        a no-op off Windows, so this is cross-platform safe.
        """
        kg = self._kill_group
        if kg is None:
            return
        try:
            assigned = kg.assign(pid)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — orphan rail must never crash spawn
            return
        if assigned:
            _stderr(
                f"[supervisor] worker pid={pid} assigned to kill-group "
                "(orphan-kill on supervisor death active).\n"
            )


# ---------------------------------------------------------------------------
# Helpers (module-level so tests can monkey-patch / reuse)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qai-serve",
        description=(
            "QAIModelBuilder reboot supervisor — spawns `python -m apps.api`, "
            "respawns it on exit-code 75, exits with the child's code "
            "otherwise."
        ),
    )
    parser.add_argument("--host", help="bind host (forwarded to apps.api)")
    parser.add_argument(
        "--port", type=int, help="bind port (forwarded to apps.api)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="forward --reload to apps.api (uvicorn reloader; dev only)",
    )
    parser.add_argument(
        "--max-reboots",
        type=int,
        default=MAX_REBOOT_CYCLES,
        help=(
            "maximum number of restart cycles before the supervisor "
            f"gives up. Defaults to {MAX_REBOOT_CYCLES}."
        ),
    )
    parser.add_argument(
        "--graceful-timeout",
        type=float,
        default=GRACEFUL_TIMEOUT_SECONDS,
        help=(
            "seconds to wait for the child to honour a graceful stop "
            f"signal before escalating. Defaults to {GRACEFUL_TIMEOUT_SECONDS}."
        ),
    )
    parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help=(
            "diagnostic mode: replace the supervisor process with the "
            "child via os.execv (no respawn, no signal forwarding). "
            "Useful when attaching a debugger to apps.api."
        ),
    )
    return parser


def _build_child_command(
    extra_args: Sequence[str],
    *,
    host: str | None,
    port: int | None,
    reload: bool,
) -> list[str]:
    """Construct the child argv: ``python -m apps.api [...]``.

    Uses :data:`sys.executable` so the supervisor inherits the active
    venv interpreter (critical for ARM64 venv on Windows where the
    PATH-resolved ``python`` may be a different architecture).
    """

    cmd: list[str] = [sys.executable, "-m", "apps.api"]
    if host:
        cmd.extend(["--host", host])
    if port is not None:
        cmd.extend(["--port", str(port)])
    if reload:
        cmd.append("--reload")
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _default_spawn(cmd: list[str]) -> "subprocess.Popen[bytes]":
    """Spawn the child process with platform-appropriate flags.

    On Windows we set ``CREATE_NEW_PROCESS_GROUP`` so the child does not
    receive the console's Ctrl+C automatically — the supervisor is in
    charge of forwarding the signal (via ``CTRL_BREAK_EVENT``) so it can
    enforce the graceful-timeout window. On POSIX we put the child in
    its own process group so SIGTERM forwarding affects only the child
    and its descendants, not the supervisor itself.
    """

    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        # ``subprocess.CREATE_NEW_PROCESS_GROUP`` is Windows-only and is
        # required for ``proc.send_signal(CTRL_BREAK_EVENT)`` to work.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)  # noqa: S603


def _normalise_exit_code(code: int | None, *, default: int) -> int:
    """Coerce ``code`` into a valid POSIX exit code (0-255)."""

    if code is None:
        return default
    if code < 0:
        # POSIX: a negative value means the child was killed by signal
        # ``-code``; surface it as 128 + signum (shell convention).
        return 128 + (-code)
    if code > 255:
        return code & 0xFF
    return code


# Well-known Windows exception / status codes (NTSTATUS) that a crashing
# child most often exits with. Mapping the raw code to a name makes the
# supervisor's crash line self-diagnosing so an operator (or a later triage
# pass) does not have to hand-decode ``code=3221225477`` into an access
# violation. Only the handful we realistically see are listed; anything else
# is reported with its hex form so it can be looked up.
_WINDOWS_STATUS_NAMES: dict[int, str] = {
    0xFFFFFFFF: "TERMINATED/-1 (TerminateProcess or native exit(-1); "
    "often a native abort() in a C extension - e.g. onnxruntime / QNN "
    "model teardown)",
    0xC0000005: "EXCEPTION_ACCESS_VIOLATION (native segfault - dereferenced "
    "a freed/invalid pointer; classic use-after-free in a native model)",
    0xC000001D: "EXCEPTION_ILLEGAL_INSTRUCTION",
    0xC0000094: "EXCEPTION_INT_DIVIDE_BY_ZERO",
    0xC00000FD: "EXCEPTION_STACK_OVERFLOW",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN (/GS or __fastfail - often a "
    "native abort())",
    0xC0000374: "STATUS_HEAP_CORRUPTION (native heap corrupted - double-free "
    "/ buffer overrun in a C extension)",
    0x40010004: "DBG_TERMINATE_PROCESS",
    0xC000013A: "STATUS_CONTROL_C_EXIT (Ctrl+C / CTRL_BREAK)",
}


def _describe_exit_code(code: int | None) -> str:
    """Return a human-readable, self-diagnosing label for a child exit code.

    Turns an opaque ``code=4294967295`` into
    ``code=4294967295 (0xFFFFFFFF, TERMINATED/-1 ...)`` so the crash line
    itself points at the likely root cause. On POSIX a negative code means
    "killed by signal N" — we surface the signal name where we can.

    Best-effort and never raises: diagnostics must not themselves crash the
    supervisor.
    """

    if code is None:
        return "code=None (child never reported an exit code)"
    try:
        # POSIX signal-kill convention: Popen surfaces ``-N`` for signal N.
        if code < 0:
            signame = ""
            try:
                signame = signal.Signals(-code).name
            except (ValueError, AttributeError):
                signame = ""
            suffix = f", killed by signal {signame}" if signame else ""
            return f"code={code} (signal {-code}{suffix})"

        # Windows surfaces the raw 32-bit NTSTATUS/exit code (unsigned).
        as_u32 = code & 0xFFFFFFFF
        name = _WINDOWS_STATUS_NAMES.get(as_u32)
        if name is not None:
            return f"code={code} (0x{as_u32:08X}, {name})"
        # Unknown but "looks like" an NTSTATUS (high bit set) — surface hex
        # so it can be looked up against the NTSTATUS table.
        if as_u32 >= 0xC0000000:
            return (
                f"code={code} (0x{as_u32:08X}, unrecognised NTSTATUS — "
                "look up against the Windows NTSTATUS table)"
            )
        return f"code={code} (0x{as_u32:08X})"
    except Exception:  # pragma: no cover - diagnostics must never raise
        return f"code={code}"


# ---------------------------------------------------------------------------
# Port probing (Layer 1 of the port-fallback design — see module docstring
# of FALLBACK_PORTS above for the why).
# ---------------------------------------------------------------------------


def _can_bind(host: str, port: int) -> bool:
    """Return ``True`` iff a fresh TCP socket can ``bind((host, port))``.

    Thin delegator to the shared, project-level truth source
    :func:`qai.platform.net.port_allocator.can_bind` (extracted so the
    daemon supervisor and the App Builder run manager share one bind
    probe with identical Windows ``SO_EXCLUSIVEADDRUSE`` semantics). See
    that module for the full State-Truth-First rationale.
    """

    return _shared_can_bind(host, port)


def _resolve_bindable_port(
    host: str,
    *,
    requested: int | None,
    fallbacks: Sequence[int] = FALLBACK_PORTS,
    can_bind: Callable[[str, int], bool] = _can_bind,
) -> int:
    """Pick a port that ``host`` can really bind right now.

    Thin delegator to
    :func:`qai.platform.net.port_allocator.resolve_bindable_port`.
    Translates the shared module's :class:`PortAllocationError` subclasses
    back into ``RuntimeError`` so the supervisor's existing
    ``except RuntimeError`` handlers (serve.py call sites) keep working
    unchanged. Priority is preserved: an explicit ``requested`` port is
    tried alone (raises on failure); otherwise ``fallbacks`` are probed
    in order. The current documented default is ``4099`` (pinned by the
    Okta redirect_uri — see
    ``qai.platform.config.settings.ServerSettings.port``); the fallback
    list's first entry (``8989``) is the legacy packaged default retained
    for backward compatibility and only consulted when ``--port`` is
    omitted.

    Tests inject ``can_bind`` to simulate excluded / occupied ports.
    """

    try:
        return _shared_resolve_bindable_port(
            host,
            requested=requested,
            fallbacks=fallbacks,
            can_bind_fn=can_bind,
        )
    except PortInUseError as exc:
        raise RuntimeError(
            f"--port {exc.port} cannot be bound on {exc.host} "
            "(another process is listening, or the port falls inside a "
            "Windows reserved range — `netsh int ipv4 show "
            "excludedportrange protocol=tcp`). Pass a different --port or "
            "let the supervisor pick one automatically by omitting --port."
        ) from exc
    except NoBindablePortError as exc:
        raise RuntimeError(
            "no bindable port found in fallback list "
            f"{list(exc.tried)} on {exc.host}. All candidates failed "
            "bind() — likely they are all inside a Windows reserved range "
            "or all occupied. Run `netsh int ipv4 show excludedportrange "
            "protocol=tcp` to see the current OS-level reservations and "
            "pass --port <n> with a port outside every excluded segment."
        ) from exc


def _stderr(msg: str) -> None:
    """Write ``msg`` to stderr without buffering, swallowing IO errors."""

    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except OSError:
        pass


__all__ = [
    "CRASH_WINDOW_SECONDS",
    "DEFAULT_PROBE_HOST",
    "FALLBACK_PORTS",
    "GRACEFUL_TIMEOUT_SECONDS",
    "MAX_CRASH_RESTARTS",
    "MAX_REBOOT_CYCLES",
    "REBOOT_EXIT_CODE",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())