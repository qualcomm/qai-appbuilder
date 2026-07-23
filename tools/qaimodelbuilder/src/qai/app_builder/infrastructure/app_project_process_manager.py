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
import os
import shlex
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from qai.app_builder.domain.app_project import (
    AppProjectDefinition,
    AppProjectNoBindablePortError,
    AppProjectNotRunningError,
    AppProjectPortInUseError,
    AppProjectRunInfo,
    AppProjectStartFailedError,
    AppProjectStatus,
)
from qai.platform.background_process.ports import (
    BackgroundProcessManagerPort,
    Info,
    Ready,
    StartInput,
)
from qai.platform.net.port_allocator import (
    DEFAULT_FALLBACK_PORTS,
    NoBindablePortError,
    PortInUseError,
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
            extra_env = self._build_child_env(definition, app_dir)
            requested = (
                port if port is not None else definition.preferred_port
            )

            if requested is not None:
                # Explicit port: honour it or fail loudly (no silent swap).
                chosen = await self._reserve_port(requested=requested)
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
            raise AppProjectStartFailedError(
                message=(
                    f"app {app_id!r} failed to start after "
                    f"{_MAX_SPAWN_ATTEMPTS} attempts (ports tried: {tried})"
                ),
                details={"app_id": app_id, "ports_tried": tried},
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
            ready_override = await self._probe_health_once(app_id)
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
        """
        async with self._lock:
            if requested is not None and requested in self._reserved_ports:
                # Explicit port already spoken for by another managed app —
                # honour the intent by failing loudly rather than colliding.
                raise AppProjectPortInUseError(
                    message=(
                        f"port {requested} is already reserved by another "
                        "running app"
                    ),
                    details={"port": requested},
                )
            excluded = tuple(self._reserved_ports.union(exclude))
            chosen = self._resolve_port(requested=requested, exclude=excluded)
            self._reserved_ports.add(chosen)
            return chosen

    def _release_port(self, port: int | None) -> None:
        """Drop a port reservation (idempotent; ``None`` is a no-op)."""
        if port is not None:
            self._reserved_ports.discard(port)

    def _resolve_port(
        self,
        *,
        requested: int | None,
        exclude: tuple[int, ...] = (),
    ) -> int:
        fallbacks = tuple(p for p in self._fallback_ports if p not in exclude)
        try:
            return resolve_bindable_port(
                _HOST, requested=requested, fallbacks=fallbacks
            )
        except PortInUseError as exc:
            raise AppProjectPortInUseError(
                message=f"port {exc.port} is already in use",
                details={"port": exc.port},
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
        if live is None or live.status in ("exited", "failed", "stopped"):
            # Process died during startup — clean up + fail (caller may
            # retry the next port for the TOCTOU auto-port case). Surface
            # the copy-pasteable manual command so the user can retry / debug
            # outside the host (plan §4.4 / §5.7). Free the port so the retry
            # (or another app) can take the next candidate cleanly.
            self._active.pop(app_id, None)
            self._health_urls.pop(app_id, None)
            self._release_port(port)
            tail = await self._safe_logs(info.id)
            raise AppProjectStartFailedError(
                message=(
                    f"app {app_id!r} process exited during startup "
                    f"(status={live.status if live else 'gone'})"
                ),
                details={
                    "app_id": app_id,
                    "port": port,
                    "manual_command": manual,
                    "log_tail": tail[-2000:],
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

    async def _live_run_info(
        self, definition: AppProjectDefinition
    ) -> AppProjectRunInfo | None:
        app_id = definition.id.value
        entry = self._active.get(app_id)
        if entry is None:
            return None
        bgp_id, port, manual = entry
        info = await self._manager.get(bgp_id)
        if info is None or info.status in ("exited", "failed", "stopped"):
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
