# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Production adapter: subprocess-backed inference service.

Manages the lifecycle of ``GenieAPIService.exe`` (or equivalent binary)
via :mod:`subprocess`.  Implements :class:`InferenceServicePort` using:

- ``Popen`` with ``CREATE_NEW_PROCESS_GROUP`` on Windows.
- ``CTRL_BREAK_EVENT`` for graceful shutdown on Windows,
  ``SIGTERM`` on POSIX.
- Background daemon thread for stdout/stderr capture into a bounded deque.
- Event-driven SSE log streaming (``stream_logs``) mirroring V1's
  ``ServiceManager`` (monotonic ``_total_written`` + ``threading.Event``).
- HTTP health-probe via :mod:`httpx` (async), supporting arbitrary
  host/port (V1 Connection-panel "Test").
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import Any, Callable, Union

from qai.model_runtime.domain.entities import (
    ModelInfo,
    detect_model_format,
    extract_context_length,
)
from qai.model_runtime.domain.enums import ServiceState
from qai.model_runtime.domain.errors import ServicePortInUseError
from qai.model_runtime.infrastructure.process_group import ProcessKillGroup

# Provider type accepted by the adapter: callable returning either a value
# or an awaitable yielding a value. Lets the DI layer reuse the existing
# *async* forge.config readers (``_load_service_launch``) without forcing a
# blocking shim, while still permitting trivial sync providers in tests.
_StrProvider = Callable[[], Union[str, Awaitable[str]]]
_IntProvider = Callable[[], Union[int, Awaitable[int]]]

logger = logging.getLogger("qai.model_runtime.infrastructure.process_service")

# Default retained log line count. Aligns with V1 ``ServiceManager``
# (``backend/service_manager.py:37`` ``LOG_BUFFER_SIZE = 6000``) so the
# Service Logs SSE stream keeps the same depth of history as V1 out of the
# box. Overridable per-deployment via ``Settings.service.log_buffer_size``.
_LOG_BUFFER_SIZE = 6000
_GRACEFUL_TIMEOUT_S = 8
_KILL_TIMEOUT_S = 3
_PROBE_TIMEOUT_S = 5.0
# Windows system PIDs (0 = System Idle, 4 = System) must never be killed when
# reclaiming an orphan-held port (V1 ``start_server.py:_kill_port`` parity).
_MIN_KILLABLE_PID = 4


class ProcessBackedInferenceService:
    """Subprocess-backed implementation of :class:`InferenceServicePort`.

    Spawns ``GenieAPIService.exe`` (or a configurable binary) and manages
    start / stop / probe / load_model lifecycle.  Logs are captured into a
    bounded deque for retrieval and streamed via :meth:`stream_logs`.

    Parameters:
        install_dir: Directory containing the service binary.
        default_port: Port the service listens on unless overridden.
        exe_name: Filename of the service binary (default ``GenieAPIService.exe``).
        models_root: Directory scanned by :meth:`list_models` for available
            models (each model is a sub-directory containing ``config.json``).
            Defaults to ``install_dir / "models"`` when empty.
    """

    def __init__(
        self,
        *,
        install_dir: str = "",
        default_port: int = 8000,
        exe_name: str = "GenieAPIService.exe",
        log_buffer_size: int = _LOG_BUFFER_SIZE,
        models_root: str = "",
        models_root_provider: _StrProvider | None = None,
        loglevel_provider: _IntProvider | None = None,
        install_dir_provider: _StrProvider | None = None,
        port_provider: _IntProvider | None = None,
    ) -> None:
        """Construct the subprocess-backed inference service adapter.

        ``log_buffer_size`` (PR-095 / S9 audit §3.3 A-26) overrides the
        retained log line count.  The DI wiring resolves the value
        from :class:`qai.platform.config.Settings.service.log_buffer_size`
        so operators can tune it without touching code.  A value of
        ``0`` disables retention entirely (useful for memory-tight
        deployments where logs are off-loaded to disk).

        ``models_root_provider`` / ``loglevel_provider`` are optional
        callables (sync or async) the adapter consults at spawn time to
        read live ``forge.config.service_launch.{models_root_path,loglevel}``
        values, mirroring V1 ``backend/main.py:5251-5267``. When omitted the
        adapter falls back to the static ``models_root`` / a default
        ``loglevel=3`` so unit tests and minimal containers keep working
        without re-wiring.

        ``install_dir_provider`` is an optional callable (sync or async)
        consulted at status/start time to read the live GenieAPIService
        install dir from ``forge.config.genie_service.root_path`` (V1
        ``backend/main.py:5205-5211`` ``_build_service_exe_path``). This
        makes the binary discoverable the instant it appears on disk
        (e.g. download-center install / manual drop into ``data/bin``)
        without restarting the API server — V1 parity. When omitted the
        adapter falls back to the static ``install_dir``.
        """
        self._install_dir = install_dir
        self._default_port = default_port
        self._exe_name = exe_name
        self._models_root = models_root
        self._models_root_provider = models_root_provider
        self._loglevel_provider = loglevel_provider
        self._install_dir_provider = install_dir_provider
        self._port_provider = port_provider

        # State
        self._state: ServiceState = ServiceState.STOPPED
        self._loaded_model: str | None = None
        self._port: int | None = None
        self._exe_path: str = ""
        self._command: str = ""

        # Process
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._start_time: float | None = None
        # Clamp to a sane range — settings already validates 0..1_000_000
        # via Pydantic, but defensive coercion here keeps the adapter
        # safe when constructed outside the DI flow (tests, scripts).
        bounded = max(0, int(log_buffer_size or 0))
        self._buffer_size: int = bounded or _LOG_BUFFER_SIZE
        self._log_buffer: deque[str] = deque(maxlen=self._buffer_size)
        self._log_thread: threading.Thread | None = None
        # Monotonic write counter (V1 ``_total_written``): lets stream_logs
        # detect new lines even after the bounded deque wraps around.
        self._total_written: int = 0
        # Cross-thread "new log line" notification (V1 ``_new_line_event``).
        self._new_line_event = threading.Event()

        # OS-level "parent dies -> daemon dies" safeguard (AGENTS.md 铁律 5).
        # Created lazily on first spawn so a process that never starts the
        # daemon (tests, cloud-only deploys) does not allocate a Job Object.
        # Held for the lifetime of this adapter (== lifetime of the API
        # process) so the Win32 job handle stays open; the OS kills every
        # assigned child the instant the last handle closes (i.e. the API
        # process exits, gracefully OR force-killed). This is the EXTRA rail
        # next to the graceful ``stop()`` — it is never the status truth
        # source (铁律 1: status stays on ``poll()``).
        self._kill_group: ProcessKillGroup | None = None

    # ------------------------------------------------------------------
    # InferenceServicePort implementation
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        model_name: str | None = None,
        port: int | None = None,
        loglevel: int | None = None,
    ) -> None:
        """Start the inference daemon subprocess.

        Builds GenieAPIService.exe argv with V1-parity short-option CLI:
        ``-c <models_root>/<model_name>/config.json -l -n -1 -p <port>
        -d <loglevel>`` (V1 ``backend/main.py:5251-5267`` ``_build_service_args``).
        ``-l`` (load_model) and ``-n -1`` (num_response) are fixed by V1; the
        real GenieAPIService binary defines this CLI and only accepts these
        five flags.

        ``loglevel`` resolves precedence: explicit arg > injected provider
        (``forge.config.service_launch.loglevel``) > V1 default ``3``.
        ``models_root`` resolves: injected provider > static field >
        ``install_dir/models`` (matching :meth:`_resolve_models_root`).

        Before spawning, ``service_config.json`` is synced to the chosen
        model (V1 ``_sync_service_config_model``, ``main.py:5214-5248``) so
        GenieAPIService's internal model table matches the directory on disk.
        Sync failures are warned but never fatal — V1 parity.
        """
        effective_port = port if port is not None else self._default_port

        # Single-instance guard + orphan reclaim (real-state-first). Extracted
        # to keep ``start`` readable; raises ``ServicePortInUseError`` when the
        # port cannot be made available for our spawn.
        await self._guard_port_for_spawn(effective_port)

        # install_dir: resolve live from the provider (V1
        # ``_build_service_exe_path`` reads ``genie_service.root_path`` per
        # spawn) so a binary installed after server start is honoured without
        # a restart. Persist it so _do_start / status report the same path.
        live_install_dir = await self._resolve_install_dir_live()
        if live_install_dir:
            self._install_dir = live_install_dir

        # loglevel: explicit arg > provider > V1 default 3
        effective_loglevel: int | None = loglevel
        if effective_loglevel is None and self._loglevel_provider is not None:
            try:
                raw = await self._maybe_await(self._loglevel_provider())
                effective_loglevel = int(raw)
            except (TypeError, ValueError, Exception):  # noqa: BLE001
                effective_loglevel = None
        if effective_loglevel is None:
            effective_loglevel = 3

        # models_root: provider > static field > install_dir/models
        models_root = ""
        if self._models_root_provider is not None:
            try:
                raw_root = await self._maybe_await(self._models_root_provider())
                models_root = str(raw_root or "")
            except Exception:  # noqa: BLE001 — convenience read; never fatal
                models_root = ""
        if not models_root:
            models_root = str(self._resolve_models_root())

        # Build V1 CLI: -c <config> -l -n -1 -p <port> -d <loglevel>
        args: list[str] = []
        if model_name:
            # Absolute config path: the daemon runs with cwd = its bin dir, so
            # a relative ``-c data/models/...`` would resolve against the bin
            # dir and fail ("config file is not found"). V1 stored an absolute
            # models_root for this reason; resolve here for defence in depth.
            config_file = str((Path(models_root) / model_name / "config.json").resolve())
            args += ["-c", config_file]
        args.append("-l")
        args += ["-n", "-1"]
        args += ["-p", str(effective_port)]
        args += ["-d", str(effective_loglevel)]

        # Pre-spawn: sync service_config.json (V1 _sync_service_config_model)
        if model_name:
            try:
                await asyncio.to_thread(self._sync_service_config_model, model_name)
            except Exception as exc:  # noqa: BLE001 — best-effort, V1 parity
                logger.warning("service_config.json sync failed (non-fatal): %r", exc)

        await asyncio.to_thread(self._do_start, args, effective_port, model_name)

    async def _guard_port_for_spawn(self, port: int) -> None:
        """Ensure ``port`` is free for our spawn, else raise (real-state-first).

        AGENTS.md 铁律 1/2:

        1. If WE already own a live daemon (``self._proc`` alive — ``poll()``
           is the direct truth source for "the process we manage"), this is a
           genuine "already running": raise rather than spawn a competing
           instance.
        2. If the port is occupied but we DON'T own a live process, the holder
           is a leftover ORPHAN from a previous run (the API was force-killed
           before the Job Object could reap it, or ran without the OS
           safeguard). V1 reaped this at launcher level
           (``start_server.py:_kill_port`` — netstat + taskkill); we mirror it,
           then spawn cleanly. We never *adopt* the orphan (铁律 2: adopting
           forfeits stdout PIPE -> logs / pid / uptime forever); we always
           spawn our own so the full management rail stays intact.
        3. If the port stays occupied after the reclaim attempt (no killable
           PID, permission denied, or a legitimate *other* program holds it),
           raise rather than spawn a daemon that will fail to bind.
        """
        self._refresh_state()
        if self._proc is not None and self._proc.poll() is None:
            logger.warning(
                "start refused: daemon already running (pid=%d)",
                self._proc.pid,
            )
            raise ServicePortInUseError(port)

        if not await asyncio.to_thread(self._is_port_in_use, port):
            return  # happy path — port free, spawn proceeds

        logger.warning(
            "port %d held by an orphan (not owned by this server); reclaiming before spawn",
            port,
        )
        reclaimed = await asyncio.to_thread(self._reclaim_orphan_port, port)
        still_in_use = await asyncio.to_thread(self._is_port_in_use, port)
        if not reclaimed or still_in_use:
            logger.warning(
                "start refused: port %d still in use after reclaim attempt",
                port,
            )
            raise ServicePortInUseError(port)

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        """Resolve ``value`` if awaitable, else return it as-is.

        Lets the adapter accept either sync or async providers without
        forcing every caller to wrap a trivial value in an async shim.
        """
        if inspect.isawaitable(value):
            return await value
        return value

    async def stop(self) -> None:
        """Stop the inference daemon gracefully."""
        await asyncio.to_thread(self._do_stop)

    async def probe(self, *, host: str | None = None, port: int | None = None) -> dict[str, Any]:
        """HTTP health probe.

        When *host*/*port* are supplied probe that arbitrary address (V1
        Connection panel "Test", reaching ``/v1/models`` — works for remote
        daemons and bypasses browser CORS). Otherwise probe the locally
        managed daemon's ``/health`` endpoint.
        """
        if host is not None and port is not None:
            return await self._probe_address(host, port)

        if self._state != ServiceState.RUNNING or self._port is None:
            return {"reachable": False, "alive": False, "model": None}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
                resp = await client.get(f"http://127.0.0.1:{self._port}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    model = data.get("model", self._loaded_model)
                    return {"reachable": True, "alive": True, "model": model}
                return {"reachable": False, "alive": False, "model": None}
        except Exception:  # noqa: BLE001
            return {"reachable": False, "alive": False, "model": None}

    def _is_port_in_use(self, port: int, host: str = "127.0.0.1") -> bool:
        """Return True iff something is already listening on ``host:port``.

        Direct TCP connect probe (synchronous; call via ``asyncio.to_thread``).
        This answers exactly "is the port taken", which is the right signal for
        the single-instance start guard — unlike the ``/v1/models`` HTTP probe
        which flickers while the daemon loads its model. A successful connect
        (errno 0) means occupied; connection refused means free.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                return sock.connect_ex((host, port)) == 0
            except OSError:
                return False

    def _reclaim_orphan_port(self, port: int) -> bool:
        """Kill the leftover process LISTENING on ``port`` (V1 parity).

        Mirrors V1 ``start_server.py:437-468`` ``_kill_port``: resolve the
        owning PID(s) via ``netstat -ano`` and force-kill them with
        ``taskkill /F /PID``. This reclaims a port held by an *orphan* daemon
        from a previous run (the API was hard-killed before the Job Object
        could reap it, or ran on a build without the OS safeguard).

        Returns ``True`` if a kill signal was sent to at least one PID,
        ``False`` if no killable owner was found / the platform is not
        Windows. Synchronous; call via ``asyncio.to_thread``.

        Safety: skips system PIDs (<= 4), our own PID, and our own live
        daemon's PID; best-effort — any subprocess / parsing failure is
        logged and treated as "could not reclaim" so the caller refuses to
        spawn rather than racing a half-killed orphan. On non-Windows this is
        a graceful no-op.
        """
        if sys.platform != "win32":
            return False
        try:
            result = subprocess.run(
                ["netstat", "-ano"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("netstat failed while reclaiming port: %r", exc)
            return False

        pids: set[int] = set()
        needle = f":{port}"
        for line in result.stdout.splitlines():
            if needle in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        pids.add(int(parts[-1]))
                    except ValueError:
                        continue
        own_pid = os.getpid()
        daemon_pid = (
            self._proc.pid if self._proc is not None and self._proc.poll() is None else None
        )
        killed = False
        for pid in pids:
            # Skip Windows system PIDs (0/4), ourselves, and our own live
            # daemon — never kill anything we manage or the OS depends on.
            if pid <= _MIN_KILLABLE_PID or pid in (own_pid, daemon_pid):
                continue
            try:
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/PID", str(pid)],  # noqa: S607
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                logger.info("reclaimed port %d: killed orphan pid=%d", port, pid)
                killed = True
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "taskkill pid=%d failed while reclaiming port: %r",
                    pid,
                    exc,
                )
        return killed

    async def _probe_address(self, host: str, port: int) -> dict[str, Any]:
        """Probe an arbitrary ``host:port`` daemon (V1 ``/v1/models``)."""
        url = f"http://{host}:{port}/v1/models"
        try:
            import httpx

            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
                resp = await client.get(url)
            reachable = resp.status_code < 500
            return {"reachable": reachable, "alive": reachable, "model": None}
        except Exception:  # noqa: BLE001
            return {"reachable": False, "alive": False, "model": None}

    async def status(self) -> dict[str, Any]:
        """Return detailed status dict — V1 parity (``service_manager.status``).

        SINGLE SOURCE OF TRUTH = our in-process ``Popen`` handle. ``running``
        is determined purely by ``self._proc`` / ``poll()`` (via
        ``_refresh_state``). We do **not** HTTP-probe ``/v1/models`` here:
        in V1 that probe is a *separate* concern (it marks the Chat model
        dropdown's ``is_running`` in ``models_registry.py``), NOT the Service
        panel's status. Mixing it in caused the daemon's model-load window
        (when ``/v1/models`` is briefly non-200) to flip the UI Stopped↔Running
        and produced phantom toasts. The Service panel reports the state of
        *the process this server manages*, which ``poll()`` answers
        deterministically.

        ``exe_path`` ("is the binary installed") is a DISK fact, resolved live
        every call (V1 ``_build_service_exe_path``, main.py:5436) so discovery
        never needs a restart and never depends on running state. Empty string
        means genuinely "not installed".
        """
        self._refresh_state()
        running = self._state == ServiceState.RUNNING
        uptime: float | None = None
        if self._start_time is not None and running:
            uptime = round(time.monotonic() - self._start_time, 1)
        pid = self._proc.pid if self._proc and self._proc.poll() is None else None
        # Unconditional live resolution (V1 parity): installed-ness is a disk
        # fact independent of running. Prefer the live install-dir resolution;
        # fall back to the spawned-from path only if live resolution is empty.
        exe_path = await self._resolve_exe_path_live() or self._exe_path
        return {
            "state": self._state.value,
            "running": running,
            "pid": pid,
            "uptime_seconds": uptime,
            "model": self._loaded_model,
            "port": self._port,
            "exe_path": exe_path,
            "command": self._command,
            "memory_mb": 0.0,
        }

    async def _resolve_install_dir_live(self) -> str:
        """Resolve the live install dir (provider > static field).

        Mirrors V1 ``_build_service_exe_path`` reading
        ``forge.config.genie_service.root_path`` on each access. Falls back
        to the static ``install_dir`` when no provider is wired (tests).
        """
        if self._install_dir_provider is not None:
            try:
                raw = await self._maybe_await(self._install_dir_provider())
                resolved = str(raw or "").strip()
                if resolved:
                    return resolved
            except Exception:  # noqa: BLE001 — convenience read; never fatal
                pass
        return self._install_dir

    async def _resolve_exe_path_live(self) -> str:
        """Return the resolved ``<install_dir>/<exe>`` iff it exists on disk."""
        install_dir = await self._resolve_install_dir_live()
        if not install_dir:
            return ""
        exe_path = Path(install_dir) / self._exe_name
        return str(exe_path) if exe_path.is_file() else ""

    async def load_model(self, model_name: str) -> None:
        """Load *model_name*; start the daemon first if it isn't running.

        V1 parity (``backend/main.py:_do_load_local_model`` @5300-5400): the
        ``/api/service/load-model`` endpoint **starts the GenieAPIService with
        the requested model when the service is not already running** (the
        daemon loads the model from its ``-c <model>/config.json`` at boot),
        rather than erroring out. This is exactly the chat dropdown / ``/model``
        auto-load path (#4): the user picks a stopped local model and expects
        it to come up. Only when the service is *already running* do we send
        the in-process ``/load_model`` switch command.

        Previously this raised ``RuntimeError("Service is not running")`` →
        HTTP 500, which broke the auto-load UX (audit A1).
        """
        self._refresh_state()
        if self._state != ServiceState.RUNNING or self._port is None:
            # Service down → start it with the requested model (V1 starts the
            # daemon, which loads the model via its ``-c`` config at boot).
            await self.start(model_name=model_name)
            self._loaded_model = model_name
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{self._port}/load_model",
                    json={"model": model_name},
                )
                resp.raise_for_status()
            self._loaded_model = model_name
            self._append_log(f"Model loaded: {model_name}")
        except Exception as exc:
            raise RuntimeError(f"Failed to load model '{model_name}': {exc}") from exc

    async def get_logs(self) -> list[str]:
        """Return snapshot of the log buffer."""
        return list(self._log_buffer)

    async def clear_logs(self) -> int:
        """Clear the retained log buffer; return the post-clear sequence.

        The returned value is V1's ``skip_from``: the frontend passes it
        back to :meth:`stream_logs` so cleared history is not replayed.
        """
        self._log_buffer.clear()
        return self._total_written

    async def stream_logs(self, *, skip: int = 0) -> AsyncIterator[str]:
        """Stream buffered then live log lines (V1 ``stream_logs``).

        Buffered lines whose sequence ``>= skip`` are yielded first, then
        new lines are yielded event-driven (via the cross-thread
        ``_new_line_event``) until the daemon process exits.
        """
        loop = asyncio.get_running_loop()

        buf_snapshot = list(self._log_buffer)
        total_at_snapshot = self._total_written
        buf_start_seq = total_at_snapshot - len(buf_snapshot)
        for i, line in enumerate(buf_snapshot):
            if buf_start_seq + i >= skip:
                yield line

        sent_seq = total_at_snapshot

        while True:
            await loop.run_in_executor(None, lambda: self._new_line_event.wait(timeout=0.5))
            self._new_line_event.clear()

            current_total = self._total_written
            if current_total > sent_seq:
                current_buf = list(self._log_buffer)
                current_buf_start = current_total - len(current_buf)
                for i, line in enumerate(current_buf):
                    if current_buf_start + i >= sent_seq:
                        yield line
                sent_seq = current_total

            self._refresh_state()
            if self._state != ServiceState.RUNNING:
                current_total = self._total_written
                if current_total > sent_seq:
                    current_buf = list(self._log_buffer)
                    current_buf_start = current_total - len(current_buf)
                    for i, line in enumerate(current_buf):
                        if current_buf_start + i >= sent_seq:
                            yield line
                break

    async def list_models(self, *, models_root: str | None = None) -> list[ModelInfo]:
        """Scan the models root for model dirs containing ``config.json``.

        Mirrors V1's ``list_service_models``: walk every ``config.json``
        under the models root, infer each model's runtime format
        (qnn/gguf/mnn) from its files, and drop directories that don't look
        like a model (``unknown`` format).

        *models_root* (V1 ``service_launch.models_root_path``) overrides the
        adapter's default (``install_dir/models``) when supplied.
        """
        root = self._resolve_models_root(models_root)
        if not root.is_dir():
            return []

        def _scan() -> list[ModelInfo]:
            results: list[ModelInfo] = []
            try:
                for config_json in sorted(root.rglob("config.json")):
                    model_dir = config_json.parent
                    fmt = self._detect_format(model_dir)
                    if fmt == "unknown":
                        continue
                    size_mb = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file()) / (
                        1024 * 1024
                    )
                    # Context-window size for the dropdown ctx badge (V1
                    # parity). Resolved via the V1 4-source cascade
                    # (``models_registry.py:_read_context_size_from_model_dir``):
                    # config.json dialog.context.size -> prompt.json
                    # context_size -> config.json top-level fields ->
                    # default 8192. Best-effort file reads — a missing /
                    # malformed file simply hands ``None`` to that source,
                    # never fatal.
                    config_data: dict | None = None
                    try:
                        with config_json.open("r", encoding="utf-8") as cf:
                            config_data = json.load(cf)
                    except (OSError, json.JSONDecodeError):
                        config_data = None
                    prompt_data: dict | None = None
                    prompt_json = model_dir / "prompt.json"
                    if prompt_json.exists():
                        try:
                            with prompt_json.open("r", encoding="utf-8") as pf:
                                prompt_data = json.load(pf)
                        except (OSError, json.JSONDecodeError):
                            prompt_data = None
                    ctx_len = extract_context_length(config_data, prompt_data)
                    results.append(
                        ModelInfo(
                            name=model_dir.name,
                            path=str(model_dir),
                            size_mb=round(size_mb, 1),
                            config_path=str(config_json),
                            model_format=fmt,
                            context_length=ctx_len,
                        )
                    )
            except OSError as exc:
                logger.warning("Error scanning models directory: %s", exc)
            return results

        return await asyncio.to_thread(_scan)

    async def get_state(self) -> ServiceState:
        """Return current service state (refreshed)."""
        self._refresh_state()
        return self._state

    def get_install_dir(self) -> str:
        """Return the filesystem path of the service installation."""
        return self._install_dir

    def set_buffer_size(self, size: int) -> None:
        """Dynamically adjust the retained log buffer size.

        V1 parity (``backend/service_manager.py:67-79``
        ``ServiceManager.set_buffer_size``): the persisted log-buffer-size
        setting is applied at runtime without re-constructing the adapter.
        The new size is clamped to ``[100, 100000]`` (V1's exact bounds)
        and stored on ``_buffer_size`` so the next :meth:`start` rebuilds
        the deque at the new depth (V1 ``service_manager.py:147``).

        Unlike V1 — which only re-applies the size on the next ``start()`` —
        this also rebuilds the **live** deque immediately, preserving the
        existing retained lines (truncating the oldest when shrinking).
        The settings-save path can therefore call this and have the change
        take effect without restarting the running daemon.
        """
        size = max(100, min(100000, int(size)))
        self._buffer_size = size
        # Re-bound the live deque while keeping current lines (newest kept
        # when shrinking, since ``deque(iterable, maxlen=n)`` drops from the
        # left). ``_total_written`` is intentionally untouched so the
        # monotonic stream_logs sequencing stays correct.
        self._log_buffer = deque(self._log_buffer, maxlen=size)
        logger.debug("ProcessBackedInferenceService: log buffer size set to %d", size)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_models_root(self, override: str | None = None) -> Path:
        """Resolve the directory scanned by :meth:`list_models`."""
        if override:
            return Path(override)
        if self._models_root:
            return Path(self._models_root)
        return Path(self._install_dir) / "models"

    @staticmethod
    def _detect_format(model_dir: Path) -> str:
        """Infer a model dir's runtime format (delegates to the domain)."""
        try:
            entries = [f for f in model_dir.iterdir() if f.is_file()]
        except OSError:
            return "unknown"
        names = [f.name.lower() for f in entries]
        suffixes = [f.suffix.lower() for f in entries]
        return detect_model_format(names, suffixes)

    def _append_log(self, line: str) -> None:
        """Append a synthetic log line and notify streamers."""
        self._log_buffer.append(line)
        self._total_written += 1
        self._new_line_event.set()

    def _sync_service_config_model(self, model_name: str) -> None:
        """Sync ``service_config.json`` to *model_name* before spawn.

        V1 parity (``backend/main.py:5214-5248``
        ``_sync_service_config_model``): GenieAPIService reads
        ``service_config.json`` at start-up and emits
        ``[E] Model directory does not exist`` errors when its ``models[*]``
        names drift away from the on-disk directory. This method updates
        the first enabled NPU slot (``backend in {"qnn", ""}``) plus
        ``default_model``, mirroring V1 exactly. The file is written only
        when something changed, with ``ensure_ascii=False`` + 4-space
        indent (V1 wire format).

        All filesystem failures are caught and logged at WARNING per V1's
        non-fatal contract: the daemon may still start with a stale config
        and surface its own error.
        """
        if not model_name:
            return
        if not self._install_dir:
            return
        cfg_path = Path(self._install_dir) / "service_config.json"
        if not cfg_path.is_file():
            return
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("_sync_service_config_model read failed (non-fatal): %s", exc)
            return

        models = cfg.get("models", []) if isinstance(cfg, dict) else []
        changed = False
        if isinstance(models, list):
            for m in models:
                if not isinstance(m, dict):
                    continue
                if m.get("enabled", False) and str(m.get("backend", "")).lower() in ("qnn", ""):
                    if m.get("name") != model_name or m.get("path") != model_name:
                        m["name"] = model_name
                        m["path"] = model_name
                        changed = True
                    break
        if isinstance(cfg, dict) and cfg.get("default_model") != model_name:
            cfg["default_model"] = model_name
            changed = True

        if not changed:
            return

        try:
            with cfg_path.open("w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)
            logger.info(
                "service_config.json: synced models[*] and default_model to %r",
                model_name,
            )
        except OSError as exc:
            logger.warning("_sync_service_config_model write failed (non-fatal): %s", exc)

    def _do_start(self, args: list[str], port: int, model_name: str | None) -> None:
        """Blocking start logic — run in a thread."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError(f"Service already running (pid={self._proc.pid})")

            # Absolute exe path: GenieAPIService derives its internal "root
            # dir" from argv[0] *and* its launch cwd. A relative ``cmd[0]``
            # (which happens when ``genie_service.root_path`` is stored
            # relative, e.g. ``data\bin\...\v73``) plus a relative ``cwd``
            # makes the daemon resolve argv[0] against the cwd a second time,
            # so it logs ``root dir = current work dir + data/bin/...`` (path
            # doubled) and fails to load ``service_config.json`` — it then
            # falls back to the binary's built-in defaults, which leak the
            # progress lines ("Inferencing..." etc.) into ``delta.content``.
            # V1 never hit this because it stored an *absolute*
            # ``genie_root_path`` (``WEBUI_DIR/bin/...``); resolve here for
            # defence in depth so ``cmd[0]`` and ``cwd`` are always absolute.
            exe_path = (Path(self._install_dir) / self._exe_name).resolve()
            if not exe_path.is_file():
                raise RuntimeError(f"Service binary not found: {exe_path}")

            cmd = [str(exe_path)] + args
            cmd_str = subprocess.list2cmdline(cmd)
            logger.info("Starting inference service: %s", cmd_str)

            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(exe_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    universal_newlines=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation_flags,
                )
            except Exception as exc:
                self._state = ServiceState.ERROR
                raise RuntimeError(f"Failed to start service: {exc}") from exc

            self._proc = proc
            self._start_time = time.monotonic()
            self._port = port
            self._loaded_model = model_name
            self._exe_path = str(exe_path)
            self._command = cmd_str
            self._state = ServiceState.RUNNING

            # OS-level orphan safeguard (AGENTS.md 铁律 5): assign the freshly
            # spawned daemon to a kill-on-parent-close group so it cannot
            # outlive this API process when we are hard-killed (Task Manager /
            # SIGKILL) and never reach the graceful ``_do_stop`` / lifespan
            # shutdown. Best-effort + additive: a failure (or non-Windows)
            # simply leaves the daemon relying on the graceful path alone, so
            # start/stop/status behaviour is unchanged.
            if self._kill_group is None:
                self._kill_group = ProcessKillGroup()
            try:
                self._kill_group.assign(proc.pid)
            except Exception as exc:  # noqa: BLE001 — must never abort start
                logger.warning("kill-group assign failed (non-fatal): %r", exc)
            # Rebuild the deque at the latest ``_buffer_size`` (V1
            # ``service_manager.py:147``): a ``set_buffer_size`` call between
            # runs takes effect here, clearing stale history.
            self._log_buffer = deque(maxlen=self._buffer_size)
            self._total_written = 0
            self._new_line_event.set()

            # Start log reader thread
            self._log_thread = threading.Thread(
                target=self._read_logs,
                args=(proc,),
                daemon=True,
                name="model-runtime-log-reader",
            )
            self._log_thread.start()
            logger.info("Inference service started (pid=%d)", proc.pid)

    def _do_stop(self) -> None:
        """Blocking stop logic — run in a thread."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                self._start_time = None
                self._state = ServiceState.STOPPED
                self._new_line_event.set()
                return

            pid = proc.pid
            self._state = ServiceState.STOPPING
            logger.info("Stopping inference service (pid=%d)", pid)

            # Graceful shutdown
            if sys.platform == "win32":
                try:
                    import signal as _signal

                    os.kill(pid, _signal.CTRL_BREAK_EVENT)
                except Exception as exc:
                    logger.warning("CTRL_BREAK_EVENT failed: %s", exc)
                    proc.terminate()
            else:
                proc.terminate()

            # Wait for graceful exit
            try:
                proc.wait(timeout=_GRACEFUL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Process did not exit in %ds, force killing (pid=%d)",
                    _GRACEFUL_TIMEOUT_S,
                    pid,
                )
                proc.kill()
                try:
                    proc.wait(timeout=_KILL_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    logger.error("Process still alive after kill (pid=%d)", pid)

            self._proc = None
            self._start_time = None
            self._state = ServiceState.STOPPED
            self._append_log("Service stopped")
            logger.info("Inference service stopped (pid=%d)", pid)

    def _read_logs(self, proc: subprocess.Popen[str]) -> None:
        """Background thread: read stdout line by line into deque."""
        try:
            while True:
                assert proc.stdout is not None
                line = proc.stdout.readline()
                if not line:
                    break  # EOF — process exited
                self._log_buffer.append(line.rstrip("\r\n"))
                self._total_written += 1
                self._new_line_event.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Log reader ended: %s", exc)
        finally:
            # Wake any stream_logs waiter so it can observe process exit.
            self._new_line_event.set()
            logger.debug("Log reader thread exiting (pid=%s)", proc.pid)

    def _refresh_state(self) -> None:
        """Sync internal state with process liveness."""
        if self._proc is not None and self._proc.poll() is not None:
            # Process has exited unexpectedly
            exit_code = self._proc.returncode
            self._proc = None
            self._start_time = None
            if self._state == ServiceState.STOPPING:
                self._state = ServiceState.STOPPED
            else:
                self._state = ServiceState.ERROR
                self._append_log(f"Service exited unexpectedly (code={exit_code})")


__all__ = ["ProcessBackedInferenceService"]
