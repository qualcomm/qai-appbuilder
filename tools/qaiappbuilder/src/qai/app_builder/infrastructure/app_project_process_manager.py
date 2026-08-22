# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Managed-run lifecycle for standalone App Builder app projects.

This adapter implements
:class:`qai.app_builder.application.ports.AppProjectProcessPort`. It does
NOT re-implement process management: it drives the shared
:class:`qai.platform.background_process` manager (PID tracking, stdout/
stderr ring buffer, process-tree kill, Win32 Job Object orphan-reap) and
the shared bind-based allocator
:mod:`qai.platform.net.port_allocator`. On top of those primitives it adds
the App-Builder-specific concerns the generic manager intentionally does
not carry:

* **Port allocation + TOCTOU retry** — pick a bindable port, spawn, and if
  the child dies immediately from a port collision (the probe→bind race),
  retry with the next candidate up to a small cap.
* **HTTP ``/health`` readiness, two consecutive successes** — the generic
  manager only does a bare TCP connect; a fullstack app is only *ready*
  once its ``/health`` route answers 200 twice in a row (guards against
  the socket being briefly accept()-able before the ASGI app is serving).
* **Runtime env injection** — ``APP_ROOT`` / ``APP_PROJECT_ROOT`` /
  ``APP_BUILDER_MODEL_ROOT`` / ``APP_BUILDER_PACK_ROOT`` + the QAIRT SDK
  ``PATH`` / ``QAIRT_ROOT`` + a ``PYTHONPATH`` carrying the app dir,
  ``<repo_root>/src`` and the pack ``shared/`` helpers, so the generated
  app can ``import qai_appbuilder`` and its own ``backend`` package. These
  are passed through ``StartInput.extra_env`` (non-secret overlay applied
  after the manager's credential strip).
* **Single-instance-per-app + real status** — one managed process per
  ``app_id``; status is always read back from the manager (never an
  optimistic cache).

Everything here is loopback-only: the app binds ``127.0.0.1`` and the host
opens the browser after readiness. The adapter never calls
``webbrowser.open`` (the HTTP route / frontend does that).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from qai.app_builder.domain.app_project import (
    AppProjectDefinition,
    AppProjectModelNotInstalledError,
    AppProjectModelRef,
    AppProjectNoBindablePortError,
    AppProjectNotRunningError,
    AppProjectPortInUseError,
    AppProjectRunInfo,
    AppProjectStartFailedError,
    AppProjectStatus,
)
from qai.platform.background_process.ports import (
    TERMINAL_STATUSES,
    BackgroundProcessManagerPort,
    Info,
    Ready,
    StartInput,
)
from qai.platform.net.port_allocator import (
    DEFAULT_FALLBACK_PORTS,
    NoBindablePortError,
    PortInUseError,
    can_bind,
    describe_port_holder,
    resolve_bindable_port,
)

__all__ = ["AppProjectProcessManager"]


#: Host every managed app binds. Loopback only — the app is a local
#: preview, never an outward listener.
_HOST = "127.0.0.1"

#: Max spawn attempts when the OS keeps handing us a port that another
#: process grabs in the probe→bind TOCTOU window (auto-port mode only).
_MAX_SPAWN_ATTEMPTS = 4

#: ``/health`` readiness: require this many *consecutive* HTTP 200s before
#: declaring the app ready (a socket can accept() before the ASGI app is
#: actually serving requests).
_REQUIRED_CONSECUTIVE_HEALTH = 2

#: Overall readiness budget (seconds) and per-probe cadence.
_READY_TIMEOUT_S = 30.0
_HEALTH_PROBE_INTERVAL_S = 0.4
_HEALTH_PROBE_TIMEOUT_S = 1.0

#: Bounded wait after ``_release_port()`` for the OS socket to actually
#: become bindable again (closes the TIME_WAIT race where a fresh spawn
#: on the same preferred port fails because the previous process's socket
#: has not fully torn down yet). Only consulted for ports we just
#: released — a never-used port is never delayed.
_PORT_RELEASE_SETTLE_S = 3.0
_PORT_RELEASE_POLL_S = 0.1

#: ``_probe_health_stable`` (status()): overall budget + per-probe cadence
#: for the status-poll debounce that stopped the run-button flicker
#: (bug #4). Independent from the spawn readiness poll above so a slow
#: status call cannot stall a fast /health.
_STATUS_PROBE_BUDGET_S = 0.8
_STATUS_PROBE_INTERVAL_S = 0.05


#: HTTP status range treated as a healthy ``/health`` response (2xx).
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


def _sync_http_ok(url: str, timeout: float) -> bool:
    """Return ``True`` iff ``GET url`` returns any HTTP status (2xx-5xx).

    A responding server (even a 404) means the ASGI app is up; but we only
    probe the app's own ``/health`` route, which returns 200 when healthy.
    Runs in a worker thread (blocking urllib) so the event loop is free.

    ``timeout`` is a POSITIONAL parameter (not keyword-only): the readiness
    poll calls this via ``loop.run_in_executor(None, probe, url, timeout)``,
    which forwards both arguments positionally, and the injectable
    ``health_probe`` port is typed ``Callable[[str, float], bool]``.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", _HTTP_OK_MIN)
            return _HTTP_OK_MIN <= status < _HTTP_OK_MAX
    except urllib.error.HTTPError:
        # Reachable but non-2xx — the app is up but /health is unhealthy.
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _dir_has_content(path: Path) -> bool:
    """Return ``True`` iff ``path`` is a directory holding at least one entry.

    An installed model dir carries weight files (``.bin``/``.onnx``/…). A
    present-but-empty dir is the "install started, weights removed" symptom
    and MUST count as not-installed — mirroring V1's
    ``pack_weights_dir_is_present_but_empty`` predicate. Non-existent paths
    and files return ``False``. Any :class:`OSError` (permission / IO)
    degrades to ``False`` — an unreadable dir is not proof of presence.
    """
    try:
        if not path.is_dir():
            return False
        return any(path.iterdir())
    except OSError:
        return False


class AppProjectProcessManager:
    """Drives managed FastAPI runs for standalone app projects.

    Constructor collaborators are all injected so the adapter is testable
    without a real venv / QAIRT SDK:

    * ``manager`` — the shared background-process manager port.
    * ``python_exe`` — the interpreter to launch (the QAI ModelBuilder
      ARM64 venv python in production).
    * ``repo_root`` — the install root (``${APP_ROOT}``).
    * ``model_root`` / ``pack_root`` / ``shared_dirs`` — runtime asset
      dirs injected into the child env / PYTHONPATH.
    * ``qairt_extra_env`` / ``qairt_path_segments`` — QAIRT SDK env + PATH
      prefixes (from the interpreter resolver); empty when no SDK.
    * ``session_id`` — the background-process ownership envelope (all app
      runs share the daemon session so they are reaped on shutdown).
    * ``health_probe`` — injectable ``(url, timeout) -> bool`` for tests.
    """

    def __init__(
        self,
        *,
        manager: BackgroundProcessManagerPort
        | Callable[[], BackgroundProcessManagerPort],
        python_exe: Path,
        repo_root: Path,
        model_root: Path,
        pack_root: Path,
        user_pack_root: Path | None = None,
        user_model_root: Path | None = None,
        shared_dirs: tuple[Path, ...] = (),
        qairt_extra_env: Mapping[str, str] | None = None,
        qairt_path_segments: tuple[str, ...] = (),
        session_id: str = "app-builder-apps",
        fallback_ports: tuple[int, ...] = DEFAULT_FALLBACK_PORTS,
        health_probe: Callable[[str, float], bool] = _sync_http_ok,
        ready_timeout_s: float = _READY_TIMEOUT_S,
    ) -> None:
        # ``manager`` may be the port itself or a zero-arg provider that
        # resolves it lazily. The DI build order wires ``app_builder``
        # BEFORE ``background_process`` (di.py:293 vs :354), so the
        # composition root passes a provider (``lambda:
        # container.background_process.manager``) and we resolve it on
        # first use — State-Truth-First, mirroring the ``_LazyStickyHost``
        # precedent in ``_app_builder_di.py``.
        #
        # ``BackgroundProcessManagerPort`` is a ``@runtime_checkable``
        # Protocol, so an actual manager instance matches ``isinstance`` (it
        # has ``start``/``stop``/…); a bare provider lambda does not. We use
        # that to distinguish "the port itself" from "a provider that returns
        # the port" and give mypy a clean narrowing.
        self._manager_provider: Callable[[], BackgroundProcessManagerPort]
        if isinstance(manager, BackgroundProcessManagerPort):
            resolved: BackgroundProcessManagerPort = manager
            self._manager_provider = lambda: resolved
        else:
            self._manager_provider = manager
        self._python_exe = python_exe
        self._repo_root = repo_root
        self._model_root = model_root
        self._pack_root = pack_root
        # P4 dual-root: expose the user-imported Pack + weights anchors to
        # spawned app subprocesses so an app that loads a user-imported
        # model resolves its files under writable data storage
        # (``<data_dir>/app_builder/user_models/...``) rather than the
        # release-contracted factory tree. ``None`` (lean container / no
        # data_dir) omits the vars entirely so legacy apps see the same
        # env they always did.
        self._user_pack_root = user_pack_root
        self._user_model_root = user_model_root
        self._shared_dirs = tuple(shared_dirs)
        self._qairt_extra_env = dict(qairt_extra_env or {})
        self._qairt_path_segments = tuple(qairt_path_segments)
        self._session_id = session_id
        self._fallback_ports = tuple(fallback_ports)
        self._health_probe = health_probe
        self._ready_timeout_s = ready_timeout_s
        # app_id -> (background-process id, port, manual_command). Single
        # instance per app. The manual command is captured at spawn so
        # status()/run() can surface it without re-resolving the definition.
        self._active: dict[str, tuple[str, int, str]] = {}
        # Ports this manager has handed out and not yet released — the union
        # of in-flight (mid-spawn) AND running app ports. Port allocation
        # excludes this set so two apps launched in quick succession never
        # collide on the same port: the auto-port ``can_bind`` probe only
        # asks the OS "is anyone listening YET", and a freshly-spawned
        # uvicorn takes seconds to actually bind, so without this set app B's
        # probe would happily re-pick app A's not-yet-bound port (the
        # cross-app probe→bind TOCTOU window). Mutated directly (never behind
        # ``_lock``) so it is safe to release from lock-held teardown paths;
        # the resolve+reserve step is the only critical section and takes
        # ``_lock`` (see :meth:`_reserve_bindable_port`). ``set`` add/discard
        # are atomic under the GIL.
        self._reserved_ports: set[int] = set()
        # app_id -> full ``/health`` URL, captured at spawn so status()/the
        # frontend status poll can do a live health probe WITHOUT re-reading
        # the definition. This is the authoritative "is it ready" signal:
        # the background-process manager's own TCP readiness can time out
        # during a long model load (e.g. QNN context load), so we must probe
        # HTTP /health on every status read, not trust the one-shot bgp flag.
        self._health_urls: dict[str, str] = {}
        # app_ids currently mid-spawn (guards double-spawn without holding
        # the lock across the slow /health readiness poll — see run()).
        self._starting: set[str] = set()
        # app_ids for which stop() arrived DURING a spawn (before the
        # process id was registered in ``_active``). The in-flight
        # ``_spawn_and_wait`` observes this and tears the freshly-spawned
        # process down instead of orphaning it (closes the start/stop race
        # opened by releasing the lock across the /health poll).
        self._stop_requested: set[str] = set()
        # Ports whose reservation was released recently — tracked with the
        # release monotonic timestamp so ``_reserve_port`` can wait up to
        # ``_PORT_RELEASE_SETTLE_S`` for the OS socket to actually become
        # bindable again before handing the port back out. Closes the
        # TIME_WAIT race that made "stop → run" fail on the same port even
        # though _we_ had released our reservation. Mutated under the GIL
        # like ``_reserved_ports``; entries older than the settle budget
        # are pruned lazily on read.
        self._released_ports: dict[int, float] = {}
        self._lock = asyncio.Lock()

    @property
    def _manager(self) -> BackgroundProcessManagerPort:
        """Resolve the background-process manager (lazy, State-Truth-First)."""
        return self._manager_provider()

    # ------------------------------------------------------------------
    # Public API (AppProjectProcessPort)
    # ------------------------------------------------------------------
    async def run(
        self,
        definition: AppProjectDefinition,
        *,
        port: int | None,
    ) -> AppProjectRunInfo:
        app_id = definition.id.value
        # Reserve the app under the lock ONLY for the fast single-instance
        # check + marking it "starting". The lock is NOT held across the
        # (up to _READY_TIMEOUT_S) /health poll — otherwise a slow start of
        # app A would serialize every other app's start behind it and, worse,
        # block ``stop(A)`` for the whole readiness window (plan §4.5: apps
        # run concurrently; a user must be able to stop a starting app).
        async with self._lock:
            existing = await self._live_run_info(definition)
            if existing is not None and existing.status in (
                "starting",
                "running",
                "ready",
            ):
                return existing
            if app_id in self._starting:
                # A concurrent run() for the same app is already spawning.
                # Report "starting" rather than double-spawning.
                return AppProjectRunInfo(
                    app_id=app_id,
                    status="starting",
                    port=None,
                    url=None,
                    pid=None,
                    process_id=None,
                    manual_command=None,
                )
            self._starting.add(app_id)

        try:
            app_dir = Path(definition.path)
            # Model-installed pre-check (bug #1): fail fast BEFORE reserving
            # any port when the app's ``app.yaml`` names bundled models whose
            # files are absent on disk. Spawning uvicorn anyway would let
            # the app's own model-load path crash inside the child process
            # with an opaque error; surfacing this at the boundary lets the
            # UI show a clear "install this model first" message. Raised
            # BEFORE ``_build_child_env`` / port reservation so
            # ``_reserved_ports`` stays untouched on the fail path. The
            # ``finally: self._starting.discard`` below still cleans up.
            self._check_models_installed(definition, app_dir)
            extra_env = self._build_child_env(definition, app_dir)
            if port is not None:
                # Explicit user-passed port: honour it or fail loudly
                # (no silent swap — the caller asked for exactly this one).
                chosen = await self._reserve_port(requested=port, hard=True)
                return await self._spawn_and_wait(
                    definition, app_dir, extra_env, chosen
                )

            if definition.preferred_port is not None:
                # ``app.yaml`` preferred_port: TRY IT FIRST but fall back to
                # the auto pool if it is occupied (bug #2). This preserves
                # "whisper always tries 1979" behaviour when the port is
                # free, without leaving the user stranded when a stale
                # process still holds it.
                chosen = await self._reserve_port(
                    requested=definition.preferred_port, hard=False
                )
                return await self._spawn_and_wait(
                    definition, app_dir, extra_env, chosen
                )

            # Auto port: allocate + spawn, retrying on TOCTOU collision. Each
            # attempt reserves its port (so a concurrent run() cannot pick the
            # same one); ``_spawn_and_wait`` releases the reservation on its
            # own failure paths, so a failed attempt frees its port before the
            # next candidate is reserved. ``tried`` still excludes it this run
            # so we advance to a fresh candidate rather than re-picking it.
            last_error: Exception | None = None
            tried: list[int] = []
            for _ in range(_MAX_SPAWN_ATTEMPTS):
                chosen = await self._reserve_port(
                    requested=None, exclude=tuple(tried)
                )
                tried.append(chosen)
                try:
                    return await self._spawn_and_wait(
                        definition, app_dir, extra_env, chosen
                    )
                except AppProjectStartFailedError as exc:
                    # Could be a TOCTOU port collision — retry next candidate.
                    last_error = exc
                    continue
            # Surface the LAST attempt's captured child log tail (and manual
            # command) so the UI shows the real startup error (ImportError,
            # model-load failure, …) instead of just "failed after N
            # attempts". ``_spawn_and_wait`` puts these on its own error's
            # ``details``; the retry loop must not drop them.
            final_details: dict[str, object] = {
                "app_id": app_id,
                "ports_tried": tried,
            }
            last_details = getattr(last_error, "details", None)
            if isinstance(last_details, dict):
                if last_details.get("log_tail"):
                    final_details["log_tail"] = last_details["log_tail"]
                if last_details.get("manual_command"):
                    final_details["manual_command"] = last_details["manual_command"]
            # Include the underlying cause's message inline so a log tail that
            # never materialised (spawn failed before any output) still gives
            # the user a concrete reason rather than a bare attempt count.
            cause_msg = str(last_error) if last_error is not None else ""
            message = (
                f"app {app_id!r} failed to start after "
                f"{_MAX_SPAWN_ATTEMPTS} attempts (ports tried: {tried})"
            )
            if cause_msg:
                message += f"; last error: {cause_msg}"
            raise AppProjectStartFailedError(
                message=message,
                details=final_details,
                cause=last_error,
            )
        finally:
            async with self._lock:
                self._starting.discard(app_id)

    async def stop(self, app_id: str) -> AppProjectRunInfo:
        # Resolve + remove the entry under the lock, then kill the tree
        # OUTSIDE the lock (tree-kill can block briefly) so a concurrent
        # run()/status() of another app is never serialized behind it.
        async with self._lock:
            entry = self._active.get(app_id)
            if entry is None:
                if app_id in self._starting:
                    # stop() raced a still-in-flight start (the process id is
                    # not registered yet). Flag it so _spawn_and_wait tears
                    # the freshly-spawned process down rather than orphaning
                    # it, and report stopped (idempotent from the caller's
                    # view — the app will not be running).
                    self._stop_requested.add(app_id)
                    return AppProjectRunInfo(
                        app_id=app_id,
                        status="stopped",
                        port=None,
                        url=None,
                        pid=None,
                        process_id=None,
                        manual_command=None,
                    )
                raise AppProjectNotRunningError(
                    message=f"app {app_id!r} is not running",
                    details={"app_id": app_id},
                )
            bgp_id, _port, _manual = entry
            # Remove now so a racing run() sees "not running" and re-spawns
            # cleanly rather than adopting a process we are tearing down. Free
            # the port reservation so a fresh run() (or another app) can reuse
            # it immediately.
            self._active.pop(app_id, None)
            self._health_urls.pop(app_id, None)
            self._release_port(_port)
        await self._manager.stop(bgp_id)
        return AppProjectRunInfo(
            app_id=app_id,
            status="stopped",
            port=None,
            url=None,
            pid=None,
            process_id=None,
            manual_command=None,
        )

    async def status(self, app_id: str) -> AppProjectRunInfo:
        entry = self._active.get(app_id)
        if entry is None:
            return AppProjectRunInfo(
                app_id=app_id,
                status="stopped",
                port=None,
                url=None,
                pid=None,
                process_id=None,
                manual_command=None,
            )
        bgp_id, port, manual = entry
        info = await self._manager.get(bgp_id)
        # Live /health probe (State-Truth-First): the bg-process manager's own
        # TCP readiness can time out during a long model load (e.g. QNN
        # context load), so `info.ready` may stay False even though the app
        # IS serving. We probe HTTP /health here so a slow-to-load app still
        # transitions "starting" -> "ready" once /health answers 200.
        ready_override: bool | None = None
        if info is not None and info.status not in (
            "exited",
            "failed",
            "stopped",
        ):
            ready_override = await self._probe_health_stable(app_id)
        return self._to_run_info(
            app_id, port, bgp_id, info, manual, ready_override
        )

    async def logs(self, app_id: str) -> str:
        entry = self._active.get(app_id)
        if entry is None:
            raise AppProjectNotRunningError(
                message=f"app {app_id!r} is not running",
                details={"app_id": app_id},
            )
        bgp_id, _port, _manual = entry
        logs = await self._manager.logs(bgp_id)
        return logs.output if logs is not None else ""

    # ------------------------------------------------------------------
    # Manual command (shown in the UI + returned on failure)
    # ------------------------------------------------------------------
    def manual_command(
        self, definition: AppProjectDefinition, port: int
    ) -> str:
        """Return the copy-pasteable Windows command to run the app.

        Mirrors the host's own spawn: same python, cwd, module, host, port.
        """
        py = str(self._python_exe)
        app_dir = definition.path
        module = definition.app_module
        return (
            f'cd /d "{app_dir}" && '
            f'"{py}" -m uvicorn {module} '
            f"--host {_HOST} --port {port}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _reserve_port(
        self,
        *,
        requested: int | None,
        exclude: tuple[int, ...] = (),
        hard: bool = True,
    ) -> int:
        """Atomically pick AND reserve a bindable port under ``_lock``.

        Excludes both ``exclude`` (this run's own already-tried ports) and
        every port currently reserved for another managed app so two apps
        launched in quick succession can never be handed the same port. The
        reservation closes the *cross-app* probe→bind TOCTOU window: the
        ``can_bind`` probe only tells us "no one is listening on this port
        YET", but a freshly-spawned uvicorn takes seconds to actually bind,
        so without reserving the chosen port here app B's probe would happily
        re-pick app A's not-yet-bound port (the shared-``18420`` symptom).

        The whole resolve+reserve is one critical section (``_lock``) so two
        concurrent ``run()`` calls cannot both observe the same port as free
        before either reserves it. The caller MUST release the port via
        :meth:`_release_port` on every path where the app does NOT end up
        occupying it (spawn failure, startup exit, stop, stale cleanup).

        ``hard=True`` (default) preserves the explicit-port contract: a
        ``requested`` port that cannot bind raises. ``hard=False`` marks
        the port as *preferred*: bind failure quietly falls through to the
        auto-pool fallbacks — used by ``app.yaml`` ``preferred_port`` so a
        stale process holding e.g. 1979 does not strand the user.

        When ``hard=True`` and the requested port was released by us very
        recently (within ``_PORT_RELEASE_SETTLE_S``), we wait for the OS
        socket to actually become bindable again before probing — closes
        the TIME_WAIT race where "stop → run" on the same port failed even
        though we had freed our own reservation.
        """
        # Pre-lock TIME_WAIT settle: only for a HARD requested port that
        # WE just released. Never blocks fresh allocations / auto-port
        # fallbacks (the pool has plenty of alternatives).
        if hard and requested is not None:
            await self._await_port_release(requested)
        async with self._lock:
            if requested is not None and requested in self._reserved_ports:
                if hard:
                    # Explicit port already spoken for by another managed
                    # app — honour the intent by failing loudly.
                    raise AppProjectPortInUseError(
                        message=(
                            f"port {requested} is already reserved by "
                            "another running app"
                        ),
                        details={"port": requested},
                    )
                # Soft mode: drop the preferred port and fall through to
                # the auto pool. The already-reserved port is excluded
                # from the fallbacks below via ``_reserved_ports``.
                requested = None
            excluded = tuple(self._reserved_ports.union(exclude))
            chosen = self._resolve_port(
                requested=requested, exclude=excluded, hard=hard
            )
            self._reserved_ports.add(chosen)
            # Once a port is actually reserved again it is no longer
            # "recently released" — drop the marker so a later hard
            # request does not re-wait unnecessarily.
            self._released_ports.pop(chosen, None)
            return chosen

    def _release_port(self, port: int | None) -> None:
        """Drop a port reservation (idempotent; ``None`` is a no-op).

        Records the release timestamp so ``_reserve_port(hard=True)`` can
        wait for the OS socket to actually become bindable again before
        handing the port back out (closes the TIME_WAIT race).
        """
        if port is not None:
            self._reserved_ports.discard(port)
            self._released_ports[port] = time.monotonic()

    async def _await_port_release(self, port: int) -> None:
        """Wait (bounded) for a recently-released port to become bindable.

        No-op when the port has not been released by us within the settle
        budget, when ``can_bind`` already accepts it, or when the budget
        expires (the caller will surface a clean ``PortInUseError`` via
        ``_resolve_port`` — waiting longer would only delay the error).
        """
        released_at = self._released_ports.get(port)
        if released_at is None:
            return
        loop = asyncio.get_running_loop()
        deadline = released_at + _PORT_RELEASE_SETTLE_S
        while loop.time() < deadline:
            elapsed = time.monotonic() - released_at
            if elapsed >= _PORT_RELEASE_SETTLE_S:
                break
            ok = await loop.run_in_executor(None, can_bind, _HOST, port)
            if ok:
                # Socket freed — drop the marker so we do not re-wait.
                self._released_ports.pop(port, None)
                return
            await asyncio.sleep(_PORT_RELEASE_POLL_S)
        # Budget elapsed — drop the stale marker regardless (a permanently
        # busy port is the port-allocator's problem, not ours to keep
        # re-checking on every subsequent request).
        self._released_ports.pop(port, None)

    def _resolve_port(
        self,
        *,
        requested: int | None,
        exclude: tuple[int, ...] = (),
        hard: bool = True,
    ) -> int:
        fallbacks = tuple(p for p in self._fallback_ports if p not in exclude)
        try:
            return resolve_bindable_port(
                _HOST, requested=requested, fallbacks=fallbacks, hard=hard
            )
        except PortInUseError as exc:
            details: dict[str, object] = {"port": exc.port}
            holder = describe_port_holder(exc.port)
            if holder is not None:
                # UX hint (not a hard fact): tell the user WHO is holding
                # the port so they can act — kill the stray process or
                # pick a different port. Kept under ``details`` so the
                # error envelope schema stays additive.
                if "pid" in holder:
                    details["holder_pid"] = holder["pid"]
                if "name" in holder:
                    details["holder_name"] = holder["name"]
            message = f"port {exc.port} is already in use"
            if "holder_name" in details:
                message += f" (held by {details['holder_name']}"
                if "holder_pid" in details:
                    message += f", pid {details['holder_pid']}"
                message += ")"
            elif "holder_pid" in details:
                message += f" (held by pid {details['holder_pid']})"
            raise AppProjectPortInUseError(
                message=message, details=details
            ) from exc
        except NoBindablePortError as exc:
            raise AppProjectNoBindablePortError(
                message="no bindable port available for the app",
                details={"ports_tried": list(exc.tried)},
            ) from exc

    def _build_child_env(
        self, definition: AppProjectDefinition, app_dir: Path
    ) -> dict[str, str]:
        """Build the non-secret env overlay for the app subprocess.

        Passed through ``StartInput.extra_env``; the background-process
        manager applies it after its credential strip, so only these
        (non-secret) runtime values reach the child.
        """
        env: dict[str, str] = {}
        env["APP_ROOT"] = str(self._repo_root)
        env["APP_PROJECT_ROOT"] = str(app_dir)
        env["APP_BUILDER_MODEL_ROOT"] = str(self._model_root)
        env["APP_BUILDER_PACK_ROOT"] = str(self._pack_root)
        # P4 dual-root: expose the user-imported anchors so the spawned app
        # can locate models the user imported at runtime (whichever anchor
        # actually holds the pack). Vars are omitted when the anchors are
        # ``None`` so legacy apps see the same env they always did.
        if self._user_pack_root is not None:
            env["APP_BUILDER_USER_PACK_ROOT"] = str(self._user_pack_root)
        if self._user_model_root is not None:
            env["APP_BUILDER_USER_MODEL_ROOT"] = str(self._user_model_root)
        # QAIRT SDK roots (QAIRT_ROOT / QNN_SDK_ROOT), empty when no SDK.
        env.update(self._qairt_extra_env)

        # PYTHONPATH: app dir + <repo>/src + pack shared helpers, prepended
        # to whatever the inherited env already has. extra_env is a wholesale
        # override, so we must compose the FULL value here (the child's
        # baseline PYTHONPATH is whatever os.environ carried).
        pp_parts: list[str] = [str(app_dir), str(self._repo_root / "src")]
        pp_parts.extend(str(d) for d in self._shared_dirs)
        inherited_pp = os.environ.get("PYTHONPATH", "")
        if inherited_pp:
            pp_parts.append(inherited_pp)
        env["PYTHONPATH"] = os.pathsep.join(pp_parts)

        # PATH: QAIRT SDK bin/lib prepended so the QNN runtime DLLs load
        # from the SDK install. Compose the full value (override semantics).
        if self._qairt_path_segments:
            path_parts = list(self._qairt_path_segments)
            inherited_path = os.environ.get("PATH", "")
            if inherited_path:
                path_parts.append(inherited_path)
            env["PATH"] = os.pathsep.join(path_parts)
        return env

    def _spawn_command(
        self, definition: AppProjectDefinition, port: int
    ) -> str:
        """The command string handed to the background-process manager.

        ``uvicorn`` is invoked via the resolved venv python's ``-m`` so the
        app runs under the QAI ModelBuilder interpreter. The cwd is set by
        ``StartInput.cwd`` to the app dir, so ``backend.main:app`` resolves.

        The background-process manager embeds this string into a shell:
        ``pwsh``/``powershell`` ``-Command`` on Windows (the WoS default),
        ``bash -c`` on POSIX. **On Windows a bare quoted path at the start
        of a statement is parsed by PowerShell as a string *literal to
        output*, not a command to run** — so we MUST prefix the call
        operator ``& `` and quote the python path with **double** quotes
        (PowerShell does not honour POSIX single-quote escaping). On POSIX
        we use ``shlex.quote``. (Mirrors the shell contract exercised by
        ``tests/unit/qai/platform/background_process/test_manager.py``.)
        """
        py = str(self._python_exe)
        module = definition.app_module
        tail = f"-m uvicorn {module} --host {_HOST} --port {port}"
        if sys.platform == "win32":
            return f'& "{py}" {tail}'
        return f"{shlex.quote(py)} {tail}"

    async def _spawn_and_wait(
        self,
        definition: AppProjectDefinition,
        app_dir: Path,
        extra_env: Mapping[str, str],
        port: int,
    ) -> AppProjectRunInfo:
        app_id = definition.id.value
        command = self._spawn_command(definition, port)
        manual = self.manual_command(definition, port)
        try:
            info = await self._manager.start(
                StartInput(
                    session_id=self._session_id,
                    command=command,
                    cwd=str(app_dir),
                    description=f"app-builder:{app_id}",
                    # Bare TCP-connect probe as a cheap first gate; the real
                    # readiness is the HTTP /health x2 poll below. We do NOT
                    # rely on this for readiness (it can accept() early).
                    ready=Ready(port=port, timeout=5000),
                    extra_env=extra_env,
                )
            )
        except Exception as exc:
            # Spawn failed before the process took the port — free the
            # reservation so this port stays available to other apps / retries.
            self._release_port(port)
            raise AppProjectStartFailedError(
                message=f"failed to spawn app {app_id!r}: {exc}",
                details={
                    "app_id": app_id,
                    "port": port,
                    "manual_command": manual,
                },
                cause=exc,
            ) from exc

        # Register the live process id under the lock, and atomically honour
        # a stop() that raced in during the spawn (before we knew info.id).
        async with self._lock:
            if app_id in self._stop_requested:
                self._stop_requested.discard(app_id)
                cancelled = True
            else:
                self._active[app_id] = (info.id, port, manual)
                self._health_urls[app_id] = self._build_health_url(
                    definition.health_path, port
                )
                cancelled = False
        if cancelled:
            # stop() arrived mid-spawn — tear down the process we just
            # started rather than orphaning it, and report it stopped. The
            # app never ends up occupying the port, so free the reservation.
            self._release_port(port)
            await self._manager.stop(info.id)
            return AppProjectRunInfo(
                app_id=app_id,
                status="stopped",
                port=None,
                url=None,
                pid=None,
                process_id=None,
                manual_command=None,
            )

        # Readiness poll + status re-read. Any UNEXPECTED error here (e.g. a
        # bug in the health probe) must NOT leave the just-spawned process
        # running-but-un-torn-down: tear it down before propagating so we
        # never leak a live uvicorn holding the port. (The shared manager's
        # shutdown()/Job Object would still reap it on host exit, but we
        # clean up eagerly on the failing run instead of waiting for that.)
        try:
            ready = await self._await_health(definition, port, info.id)
            # Re-read real status from the manager (State-Truth-First).
            live = await self._manager.get(info.id)
        except Exception:
            await self._safe_stop(info.id)
            self._active.pop(app_id, None)
            self._health_urls.pop(app_id, None)
            self._release_port(port)
            raise
        if live is None or live.status in TERMINAL_STATUSES:
            # Process died during startup — clean up + fail (caller may retry
            # the next port for the TOCTOU auto-port case). Surface the
            # copy-pasteable manual command so the user can retry / debug
            # outside the host. Free the port so the retry (or another app)
            # can take the next candidate cleanly.
            self._active.pop(app_id, None)
            self._health_urls.pop(app_id, None)
            self._release_port(port)
            exit_status = live.status if live else "gone"
            tail = await self._drain_logs(info.id)
            raise AppProjectStartFailedError(
                message=(
                    f"app {app_id!r} process exited during startup "
                    f"(status={exit_status})"
                ),
                details={
                    "app_id": app_id,
                    "port": port,
                    "manual_command": manual,
                    "log_tail": tail[-4000:],
                },
            )
        status: AppProjectStatus = "ready" if ready else "starting"
        return self._run_info(app_id, port, info.id, live.pid, status, manual)

    @staticmethod
    def _build_health_url(health_path: str, port: int) -> str:
        path = health_path or "/health"
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{_HOST}:{port}{path}"

    async def _await_health(
        self,
        definition: AppProjectDefinition,
        port: int,
        bgp_id: str,
    ) -> bool:
        """Poll ``/health`` until two consecutive 200s or timeout.

        Aborts early if the process reaches a terminal state.
        """
        url = self._build_health_url(definition.health_path, port)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout_s
        consecutive = 0
        while loop.time() < deadline:
            info = await self._manager.get(bgp_id)
            if info is None or info.status in (
                "exited",
                "failed",
                "stopped",
            ):
                return False
            ok = await loop.run_in_executor(
                None,
                self._health_probe,
                url,
                _HEALTH_PROBE_TIMEOUT_S,
            )
            if ok:
                consecutive += 1
                if consecutive >= _REQUIRED_CONSECUTIVE_HEALTH:
                    return True
            else:
                consecutive = 0
            await asyncio.sleep(_HEALTH_PROBE_INTERVAL_S)
        return False

    async def _probe_health_once(self, app_id: str) -> bool:
        """Single ``/health`` probe for a tracked app (for status reads).

        Returns ``True`` on a 200, ``False`` otherwise (unreachable / non-2xx
        / no health URL recorded). Never raises — a probe error is treated as
        "not ready yet".
        """
        url = self._health_urls.get(app_id)
        if url is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, self._health_probe, url, _HEALTH_PROBE_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — a probe failure is "not ready"
            return False

    async def _probe_health_stable(
        self,
        app_id: str,
        *,
        min_consecutive: int = 2,
        budget_s: float = _STATUS_PROBE_BUDGET_S,
        interval_s: float = _STATUS_PROBE_INTERVAL_S,
    ) -> bool:
        """Debounced ``/health`` probe for ``status()`` reads.

        Requires ``min_consecutive`` back-to-back 200s within ``budget_s``
        before returning ``True`` — closes the run-button flicker (bug #4)
        where a single-shot probe caught a stale process still answering
        while the new one had not yet finished binding, so the frontend
        oscillated ``starting`` ↔ ``ready`` between polls.

        Any failure resets the consecutive counter; if the budget expires
        without hitting ``min_consecutive``, returns ``False`` (still
        starting from the caller's view). Never raises — a probe error is
        treated as "not ready yet". A healthy /health answers in a few ms
        so the whole call typically returns in one interval when ready.
        """
        if self._health_urls.get(app_id) is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        consecutive = 0
        # Always take at least one sample so a fast, healthy /health can
        # short-circuit without waiting for the budget when ``min_consecutive``
        # is 1.
        while True:
            if await self._probe_health_once(app_id):
                consecutive += 1
                if consecutive >= min_consecutive:
                    return True
            else:
                consecutive = 0
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval_s)

    def _check_models_installed(
        self,
        definition: AppProjectDefinition,
        app_dir: Path,
    ) -> None:
        """Verify every bundled model has its files on disk BEFORE spawn.

        Iterates ``definition.models`` and, for each ref, expands the raw
        ``model_dir`` / ``pack_dir`` strings against the same env the child
        process would see (``${APP_ROOT}`` → ``repo_root``, plus the
        ``APP_BUILDER_*_ROOT`` anchors + ``APP_PROJECT_ROOT``) and probes
        the filesystem. When ``model_dir`` is set we require the directory
        to be present AND non-empty (a stray-but-empty dir is the "install
        removed the weights" symptom); when ``pack_dir`` is set we require
        it to be a directory (packs ship with metadata even before weight
        install, so presence alone is enough). When neither is declared,
        fall through to the built-in / user weights fallback layout used by
        :class:`FileSystemAppProjectPackager`. Refs with NO on-disk facts
        anywhere are collected and reported in a single
        :class:`AppProjectModelNotInstalledError` naming the app_id, each
        missing model, the paths we tried, and the install action.

        Pure fs reads under the calling task — no lock held, no shared
        state mutated. The caller (``run()``) invokes this INSIDE its
        ``try:`` so ``finally: self._starting.discard`` still runs on the
        fail path but ``_reserved_ports`` stays untouched.
        """
        missing: list[dict[str, object]] = []
        for ref in definition.models:
            problem = self._probe_model_ref(ref, app_dir)
            if problem is not None:
                missing.append(problem)
        if not missing:
            return
        app_id = definition.id.value
        names = ", ".join(str(m["model_id"]) for m in missing)
        # If any missing entry is a user-provided pack, tack a short pointer
        # at the setup guide onto the (English) message so operators reading
        # server logs see WHERE to find the conversion recipe — the frontend
        # still keys off the stable ``app_builder.model_not_installed`` code
        # + the structured ``details.missing[]`` for its localized render.
        guide_entry = next(
            (
                m
                for m in missing
                if m.get("provisioning") == "user-provided"
                and m.get("setup_guide_url")
            ),
            None,
        )
        if guide_entry is not None:
            message = (
                f"app {app_id!r} cannot run: model(s) require manual setup: "
                f"{names}. See setup guide: "
                f"{guide_entry['setup_guide_url']}"
            )
        else:
            message = (
                f"app {app_id!r} cannot run: model(s) not installed: "
                f"{names}. Install the model(s) from the App Builder "
                "model panel (or re-run the app's install script) and "
                "try again."
            )
        raise AppProjectModelNotInstalledError(
            message=message,
            details={
                "app_id": app_id,
                "missing": missing,
            },
        )

    def _probe_model_ref(
        self, ref: AppProjectModelRef, app_dir: Path
    ) -> dict[str, object] | None:
        """Return a ``{model_id, ..., provisioning, setup_guide_url}`` dict
        when ``ref`` is NOT installed, else ``None``.

        Mirrors the resolution order used by
        :meth:`AppProjectPackager._collect_model_weights`: explicit
        ``model_dir`` first (with a fallback to the built-in / user
        weights layouts when the expansion misses), then fall back to
        the ``<model_root>/<id>/`` / ``<user_model_root>/models/<id>/``
        layout when the ref declared nothing.

        When the ref declares a ``pack_dir`` we ALSO peek at that pack's
        ``weights.json`` (if present) for the ``provisioning`` /
        ``setup_guide`` fields. ``provisioning="user-provided"`` marks a
        pack the user must convert manually (no auto-download), and the
        UI renders ``setup_guide_url`` (repo-relative filesystem path to
        the pack's setup markdown) as a copyable hint instead of the
        generic "install from the model panel" message. Both default to
        ``"download"`` / ``None`` when the pack (or its ``weights.json``)
        is absent so refs from the four legacy packs behave unchanged.
        """
        # ``model_dir`` — the primary weights anchor (models with binary
        # weight files). Its expansion is the authoritative check.
        # Ref with NEITHER ``model_dir`` NOR ``pack_dir`` declared: the
        # spec is deliberately opaque about where its files live (the
        # runner locates them at runtime via ``repoRoot`` self-resolve).
        # We have no ground truth to probe, so skip — the app itself is
        # the authority on where it looks. Refs that declare EITHER path
        # ARE probed below.
        if not ref.model_dir and not ref.pack_dir:
            return None
        paths_tried: list[str] = []
        if ref.model_dir:
            resolved = self._expand_placeholders(ref.model_dir, app_dir)
            paths_tried.append(str(resolved))
            if _dir_has_content(resolved):
                return None
            # Expand-miss fallback: a common LLM-generated mistake is
            # hard-coding the built-in path for a user pack. Probe the
            # fallback layout too before declaring the model missing.
            for candidate in self._model_dir_fallbacks(ref.id):
                paths_tried.append(str(candidate))
                if _dir_has_content(candidate):
                    return None
            return self._missing_entry(
                ref, app_dir, paths_tried, "model_dir absent or empty"
            )
        # No ``model_dir`` declared — fall back to the canonical layout.
        for candidate in self._model_dir_fallbacks(ref.id):
            paths_tried.append(str(candidate))
            if _dir_has_content(candidate):
                return None
        # A ref that also declares ``pack_dir`` but no ``model_dir``:
        # some legacy packs ship weights alongside pack metadata, so a
        # present pack directory is proof enough. This mirrors the
        # existing packager's fallback for well-formed packs.
        if ref.pack_dir:
            pack_resolved = self._expand_placeholders(ref.pack_dir, app_dir)
            paths_tried.append(str(pack_resolved))
            if pack_resolved.is_dir():
                return None
        return self._missing_entry(
            ref,
            app_dir,
            paths_tried,
            "no model files found under any known layout",
        )

    def _missing_entry(
        self,
        ref: AppProjectModelRef,
        app_dir: Path,
        paths_tried: list[str],
        reason: str,
    ) -> dict[str, object]:
        """Build a ``details.missing[]`` row, enriched from the pack's
        ``weights.json`` when the ref declares a ``pack_dir``.

        Extracts ``provisioning`` (``"download"`` / ``"user-provided"``)
        and ``setup_guide`` (a filename relative to the pack dir) from
        the pack's ``weights.json``. ``setup_guide_url`` is returned as
        a *repo-relative POSIX path* (``factory/…/pack/SKILL.md``) so
        the frontend can render it as a copyable hint without leaking
        the operator's absolute FS layout. On any read/parse error we
        silently fall through to defaults — a broken ``weights.json``
        MUST NOT change the "model missing" verdict.
        """
        provisioning, setup_guide_url = self._read_pack_setup(ref, app_dir)
        return {
            "model_id": ref.id,
            "title": ref.title,
            "paths_tried": paths_tried,
            "reason": reason,
            "provisioning": provisioning,
            "setup_guide_url": setup_guide_url,
        }

    def _read_pack_setup(
        self, ref: AppProjectModelRef, app_dir: Path
    ) -> tuple[str, str | None]:
        """Return ``(provisioning, setup_guide_url_or_none)`` for ``ref``.

        Defaults ``("download", None)``. When ``ref.pack_dir`` expands to
        an existing directory containing a ``weights.json`` we parse it
        and read the two top-level scalar fields agreed with the pack
        author (canonical ``provisioning``; legacy ``user_provided: true``
        boolean also promotes to ``"user-provided"`` for older packs).
        """
        if not ref.pack_dir:
            return ("download", None)
        pack_dir = self._expand_placeholders(ref.pack_dir, app_dir)
        weights_path = pack_dir / "weights.json"
        try:
            raw = weights_path.read_text(encoding="utf-8")
        except OSError:
            return ("download", None)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return ("download", None)
        if not isinstance(data, dict):
            return ("download", None)
        prov_raw = data.get("provisioning")
        if isinstance(prov_raw, str) and prov_raw in (
            "download",
            "user-provided",
        ):
            provisioning = prov_raw
        elif data.get("user_provided") is True:
            # Legacy field on packs authored before ``provisioning``
            # existed. Older packs may set only this boolean.
            provisioning = "user-provided"
        else:
            provisioning = "download"
        guide_raw = data.get("setup_guide")
        setup_guide_url: str | None = None
        if isinstance(guide_raw, str) and guide_raw.strip():
            # Return as repo-relative POSIX path so the UI can show a
            # copyable hint and the operator can locate the guide file
            # without our absolute install path leaking into the DTO.
            guide_abs = (pack_dir / guide_raw).resolve()
            try:
                setup_guide_url = guide_abs.relative_to(
                    self._repo_root
                ).as_posix()
            except ValueError:
                # Pack lives outside repo_root (unusual — e.g. a symlinked
                # user pack). Fall back to the absolute path.
                setup_guide_url = guide_abs.as_posix()
        return (provisioning, setup_guide_url)

    def _model_dir_fallbacks(self, model_id: str) -> Iterable[Path]:
        """Canonical model-dir layouts probed when a ref does not declare one.

        Mirrors :meth:`AppProjectPackager._fallback_weights_dir`:
        built-in ``<model_root>/<id>/`` first, then the user-imported
        layout ``<user_model_root>/models/<id>/`` when configured.
        """
        yield (self._model_root / model_id).resolve()
        if self._user_model_root is not None:
            yield (
                self._user_model_root / "models" / model_id
            ).resolve()

    def _expand_placeholders(self, raw: str, app_dir: Path) -> Path:
        """Expand the ``${...}`` tokens the child env exposes to the app.

        Mirrors :meth:`_build_child_env` so a model ref written against
        the child env resolves the same way here. Kept as a small local
        helper (no import cycle with the packager) — the token set is
        stable and short.
        """
        substitutions = {
            "${APP_ROOT}": str(self._repo_root),
            "${APP_PROJECT_ROOT}": str(app_dir),
            "${APP_BUILDER_MODEL_ROOT}": str(self._model_root),
            "${APP_BUILDER_PACK_ROOT}": str(self._pack_root),
        }
        if self._user_model_root is not None:
            substitutions["${APP_BUILDER_USER_MODEL_ROOT}"] = str(
                self._user_model_root
            )
        if self._user_pack_root is not None:
            substitutions["${APP_BUILDER_USER_PACK_ROOT}"] = str(
                self._user_pack_root
            )
        expanded = raw
        for token, value in substitutions.items():
            expanded = expanded.replace(token, value)
        return Path(expanded).resolve()

    async def _live_run_info(
        self, definition: AppProjectDefinition
    ) -> AppProjectRunInfo | None:
        app_id = definition.id.value
        entry = self._active.get(app_id)
        if entry is None:
            return None
        bgp_id, port, manual = entry
        info = await self._manager.get(bgp_id)
        if info is None or info.status in TERMINAL_STATUSES:
            # Stale entry — the process is gone; forget it AND free its port.
            self._active.pop(app_id, None)
            self._health_urls.pop(app_id, None)
            self._release_port(port)
            return None
        return self._to_run_info(app_id, port, bgp_id, info, manual)

    async def _safe_logs(self, bgp_id: str) -> str:
        try:
            logs = await self._manager.logs(bgp_id)
        except Exception:  # noqa: BLE001
            return ""
        return logs.output if logs is not None else ""

    async def _drain_logs(
        self, bgp_id: str, *, budget_s: float = 2.0, interval_s: float = 0.1
    ) -> str:
        """Read the child's log tail, polling briefly until it is non-empty.

        A just-crashed process reports ``exited`` before its stdout/stderr
        pump has appended the final chunk (the Traceback), so an immediate
        :meth:`_safe_logs` can return ``""``. Poll up to ``budget_s`` for
        the pump to flush; return whatever is present when it stabilises or
        the budget elapses. Always returns a string (never raises).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        tail = await self._safe_logs(bgp_id)
        while loop.time() < deadline:
            await asyncio.sleep(interval_s)
            latest = await self._safe_logs(bgp_id)
            if latest == tail and tail != "":
                break  # stabilised with content
            tail = latest
        return tail

    async def _safe_stop(self, bgp_id: str) -> None:
        """Best-effort tear-down of a spawned process; never raises.

        Used on unexpected failures during the readiness poll so a live
        process is not leaked. Swallows any error (the shared manager's
        shutdown / Job Object remains the final backstop on host exit).
        """
        with contextlib.suppress(Exception):
            await self._manager.stop(bgp_id)

    def _to_run_info(
        self,
        app_id: str,
        port: int,
        bgp_id: str,
        info: Info | None,
        manual_command: str | None = None,
        ready_override: bool | None = None,
    ) -> AppProjectRunInfo:
        if info is None:
            self._active.pop(app_id, None)
            self._health_urls.pop(app_id, None)
            self._release_port(port)
            return AppProjectRunInfo(
                app_id=app_id,
                status="stopped",
                port=None,
                url=None,
                pid=None,
                process_id=None,
                manual_command=None,
            )
        status: AppProjectStatus
        is_ready = ready_override if ready_override is not None else info.ready
        if info.status in ("exited", "failed"):
            status = "failed"
            # A dead process no longer holds its port — free the reservation
            # so another app can take it even before the stale ``_active``
            # entry is reaped on the next run()/stop().
            self._release_port(port)
        elif info.status == "stopped":
            status = "stopped"
            self._release_port(port)
        elif is_ready:
            # Ready when a live /health probe passed (ready_override) or, when
            # no probe was done, the bg-process manager's own readiness flag.
            status = "ready"
        else:
            status = "starting"
        return self._run_info(
            app_id, port, bgp_id, info.pid, status, manual_command
        )

    def _run_info(
        self,
        app_id: str,
        port: int,
        bgp_id: str,
        pid: int | None,
        status: AppProjectStatus,
        manual_command: str | None = None,
    ) -> AppProjectRunInfo:
        url = f"http://{_HOST}:{port}/" if status in (
            "starting",
            "running",
            "ready",
        ) else None
        return AppProjectRunInfo(
            app_id=app_id,
            status=status,
            port=port if status != "stopped" else None,
            url=url,
            pid=pid,
            process_id=bgp_id,
            manual_command=(
                manual_command if status != "stopped" else None
            ),
        )
