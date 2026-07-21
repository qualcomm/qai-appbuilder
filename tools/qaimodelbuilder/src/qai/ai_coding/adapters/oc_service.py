# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""asyncio.subprocess-backed :class:`OcServicePort` adapter (PR-105).

Manages the local OpenCode HTTP server subprocess via
:func:`asyncio.create_subprocess_exec`, mirroring the legacy
``opencode_proc_manager`` capability without taking a hard
dependency on ``backend.ai_coding.opencode_proc_manager``.

Key design points
-----------------
* The adapter is **single-instance** (one OpenCode server per
  process); concurrent calls to :meth:`start` while a previous start
  is in progress are serialised by an :class:`asyncio.Lock` so the
  caller never sees two PIDs claiming the same port.
* The adapter records a ring buffer of the last ``_LOG_BUFFER_MAX``
  stdout/stderr lines so :meth:`logs` can return without re-reading
  the file system.
* External-process detection: when the managed PID is not alive but
  the configured port is reachable, :meth:`status` reports
  ``running=True`` + ``external=True`` so the WebUI shows the
  correct indicator.

CLI path / port resolution
--------------------------
The adapter does NOT read the WebUI's config document directly
(domain-purity).  Construction takes the resolved values; the DI
layer (``apps/api/_ai_coding_di.py``) reads them from the
:class:`Settings` namespace and the ``ai_coding.oc.config`` kv doc
before instantiating the adapter.

Stub mode
---------
When ``cli_path`` is empty, the adapter's :meth:`start` raises
:class:`qai.platform.errors.ValidationError` (caller surfaces 400).
:meth:`status` and :meth:`logs` always succeed (empty payload).
This is the fresh-install path before the user configures OpenCode.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from qai.ai_coding.application.ports import (
    OcServicePort,
    OcServiceStatus,
)
from qai.platform.errors import ValidationError
from qai.platform.logging import get_logger
from qai.platform.process import ProcessKillGroup
from qai.platform.tasks import TaskRegistry

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = get_logger(__name__)


__all__ = ["LocalOcServiceAdapter"]


#: Maximum number of stdout / stderr lines retained in the ring
#: buffer.  Above this the oldest entries are dropped; chosen to
#: match the legacy ``oc_proc_manager.MAX_LOG_LINES`` cap.
_LOG_BUFFER_MAX = 500


def _parse_port_from_base_url(base_url: str) -> int | None:
    """Extract the TCP port from an OpenCode ``base_url`` (V1 parity).

    Mirrors V1 ``opencode_api_routes.py:1104-1107`` which derived the
    port from ``base_url`` via ``int(base_url.rstrip("/").rsplit(":",
    1)[-1])``.  Returns ``None`` when the URL carries no parsable
    trailing port (the caller then falls back to its existing port).
    """
    if not base_url:
        return None
    try:
        tail = base_url.rstrip("/").rsplit(":", 1)[-1]
        return int(tail)
    except (ValueError, IndexError):
        return None


class LocalOcServiceAdapter(OcServicePort):
    """Local subprocess-backed :class:`OcServicePort`.

    Construction-time configuration provides *fallback* defaults; the
    authoritative ``cli_path`` / ``hostname`` / ``port`` (+ Basic Auth
    credentials) are resolved **live** at each :meth:`start` /
    :meth:`status` / :meth:`stop` call from the optional
    ``config_provider`` so a ``PUT /api/oc/config`` takes effect on the
    very next operation without a process restart (AGENTS.md 铁律 1/4 —
    "external resource state from a live read, not a constructor-time
    snapshot").  V1 read these live from ``oc_config`` on every
    ``oc_service_start`` (``opencode_api_routes.py:1096-1107``).

    * ``cli_path`` — absolute filesystem path to the ``opencode`` CLI
      binary.  Empty string (and no config-provided value) disables
      :meth:`start` (raises :class:`ValidationError`); :meth:`status` /
      :meth:`logs` still function but report ``running=False``.
    * ``hostname`` — bind hostname fallback (the DI layer reads the
      default from ``Settings``; the OC config doc overrides it live).
    * ``port`` — bind TCP port fallback (``0`` = unconfigured sentinel
      until the config doc supplies one).
    * ``probe_timeout`` — per-probe timeout (seconds) used by the
      external-process detection path.
    * ``config_provider`` — optional async callable returning the live
      OC config document (``cli_path`` / ``base_url`` / ``hostname`` /
      ``port`` / ``username`` / ``password``).  When ``None`` the
      adapter behaves exactly like the legacy static-snapshot form
      (back-compat for tests).
    * ``provider_sync`` — optional async callable invoked once at the top
      of :meth:`start` (before the spawn) to sync the Cloud Models
      providers into OpenCode's own ``opencode.jsonc`` (RE-OC-3; V1 ran
      this inside the session manager's ``start()``).  Best-effort — its
      failure never blocks the spawn.  ``None`` disables the sync.
    """

    __slots__ = (
        "_cli_path",
        "_config_provider",
        "_hostname",
        "_lock",
        "_log_buffer",
        "_password",
        "_port",
        "_probe_timeout",
        "_proc",
        "_provider_sync",
        "_started_at",
        "_tasks",
        "_username",
    )

    def __init__(
        self,
        *,
        cli_path: str = "",
        hostname: str,
        port: int,
        probe_timeout: float = 1.5,
        config_provider: (
            Callable[[], Awaitable[dict[str, Any]]] | None
        ) = None,
        provider_sync: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._cli_path = cli_path
        self._hostname = hostname
        self._port = port
        self._probe_timeout = probe_timeout
        self._config_provider = config_provider
        self._provider_sync = provider_sync
        # Basic Auth credentials resolved live from the config doc
        # (V1 ``OPENCODE_SERVER_USERNAME`` / ``OPENCODE_SERVER_PASSWORD``).
        self._username = ""
        self._password = ""
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._started_at: float | None = None
        self._log_buffer: deque[str] = deque(maxlen=_LOG_BUFFER_MAX)
        # R-3 — retain a strong ref to the background stdout drainer so
        # it is not GC'd mid-flight and is cancelled on :meth:`stop`.
        self._tasks = TaskRegistry()
        # State-Truth-First 铁律5: OS-level orphan-kill fallback so the
        # OpenCode server subprocess is reaped even if the API process is
        # force-killed before graceful stop() runs. Lazily created,
        # best-effort (no-op off Windows). Mirrors model_runtime /
        # sticky_worker / sandbox daemon.
        self._kill_group: ProcessKillGroup | None = None

    # ------------------------------------------------------------------
    # Live config resolution (AGENTS.md 铁律 1/4)
    # ------------------------------------------------------------------
    async def _refresh_config(self) -> None:
        """Re-read the live OC config doc into the bound fields.

        Called at the top of :meth:`status` / :meth:`start` /
        :meth:`stop` so a ``PUT /api/oc/config`` update is honoured on
        the next operation (no process restart needed).  Mirrors V1
        ``oc_service_start`` reading ``oc_config`` fresh each call.

        Resolution priority (per field): the config doc value wins over
        the constructor fallback; ``port`` is taken from an explicit
        ``port`` key, else parsed from ``base_url`` (V1 behaviour), else
        the existing fallback.  Best-effort — a provider failure leaves
        the current fields intact (the adapter degrades to its
        construction-time snapshot rather than crashing).
        """
        if self._config_provider is None:
            return
        try:
            doc = await self._config_provider()
        except Exception:  # noqa: BLE001 — degrade to current snapshot.
            return
        if not isinstance(doc, dict):
            return

        cli_path = doc.get("cli_path")
        if isinstance(cli_path, str) and cli_path.strip():
            self._cli_path = cli_path.strip()

        hostname = doc.get("hostname")
        if isinstance(hostname, str) and hostname.strip():
            self._hostname = hostname.strip()

        # Port: explicit ``port`` key first, else parse ``base_url``
        # (V1 derived it from base_url).
        resolved_port: int | None = None
        raw_port = doc.get("port")
        if isinstance(raw_port, int) and raw_port > 0:
            resolved_port = raw_port
        elif isinstance(raw_port, str) and raw_port.strip().isdigit():
            resolved_port = int(raw_port.strip())
        if resolved_port is None:
            base_url = doc.get("base_url")
            if isinstance(base_url, str):
                resolved_port = _parse_port_from_base_url(base_url)
        if resolved_port is not None and resolved_port > 0:
            self._port = resolved_port

        username = doc.get("username")
        self._username = username.strip() if isinstance(username, str) else ""
        password = doc.get("password")
        self._password = password if isinstance(password, str) else ""

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def status(self) -> OcServiceStatus:
        # Live-read the config so a PUT /api/oc/config update is
        # reflected in the reported cli_path / port immediately.
        await self._refresh_config()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            uptime = (
                time.monotonic() - self._started_at
                if self._started_at is not None
                else None
            )
            return OcServiceStatus(
                running=True,
                pid=proc.pid,
                uptime_seconds=uptime,
                port=self._port,
                cli_path=self._cli_path,
                external=False,
            )

        # Managed proc is not alive; probe the port for an external
        # process.  This mirrors the legacy ``oc_proc_manager.status``
        # fallback which keeps the WebUI in sync when an admin
        # started OpenCode by hand.
        external_alive = await self._port_reachable()
        if external_alive:
            return OcServiceStatus(
                running=True,
                pid=None,
                uptime_seconds=None,
                port=self._port,
                cli_path=self._cli_path,
                external=True,
            )

        return OcServiceStatus(
            running=False,
            pid=None,
            uptime_seconds=None,
            port=self._port,
            cli_path=self._cli_path,
            external=False,
        )

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    async def start(self) -> OcServiceStatus:
        async with self._lock:
            # Idempotent: an already-running managed process returns
            # the current status without re-spawning.  ``status()``
            # also live-refreshes the config (cli_path / port /
            # Basic Auth creds) so the spawn below uses the latest
            # values from PUT /api/oc/config (V1 read oc_config fresh
            # on every oc_service_start).
            existing = await self.status()
            if existing.running:
                return existing

            if not self._cli_path:
                raise ValidationError(
                    code="ai_coding.oc_service.cli_path_unconfigured",
                    message=(
                        "OpenCode cli_path is not configured; set it via "
                        "PUT /api/oc/config before starting the service"
                    ),
                    field_errors={"cli_path": ["unconfigured"]},
                )

            # RE-OC-3 — sync the Cloud Models providers into OpenCode's
            # own ``opencode.jsonc`` before the spawn so the freshly
            # started server can use them (V1 ran this inside the session
            # manager's ``start()``).  Best-effort: a sync failure must
            # never block the service start.
            if self._provider_sync is not None:
                try:
                    await self._provider_sync()
                except Exception as exc:  # noqa: BLE001 — never block start.
                    logger.warning(
                        "ai_coding.oc_service.provider_sync_failed",
                        error=str(exc),
                    )

            # Basic Auth (optional): inject OPENCODE_SERVER_PASSWORD /
            # OPENCODE_SERVER_USERNAME into the child env when a password
            # is configured (V1 ``OpenCodeProcessManager.start``
            # ``opencode_session_manager.py:1428-1435``).  Inherit the
            # parent env and overlay only the auth vars.
            proc_env: dict[str, str] | None = None
            if self._password:
                proc_env = dict(os.environ)
                proc_env["OPENCODE_SERVER_PASSWORD"] = self._password
                proc_env["OPENCODE_SERVER_USERNAME"] = (
                    self._username or "opencode"
                )

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self._cli_path,
                    "serve",
                    "--hostname",
                    self._hostname,
                    "--port",
                    str(self._port),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=proc_env,
                )
            except FileNotFoundError as exc:
                raise ValidationError(
                    code="ai_coding.oc_service.cli_not_found",
                    message=(
                        f"opencode CLI not found at {self._cli_path!r}"
                    ),
                    field_errors={"cli_path": [self._cli_path]},
                ) from exc

            self._started_at = time.monotonic()
            # Assign the spawned OpenCode server to the OS kill-group so it
            # cannot outlive a force-killed parent as an orphan (铁律5).
            # Best-effort: a failure never blocks start (graceful stop()
            # remains the primary teardown).
            if self._kill_group is None:
                self._kill_group = ProcessKillGroup()
            pid = getattr(self._proc, "pid", None)
            if pid is not None:
                self._kill_group.assign(int(pid))
            # Spin up a background reader so the pipe doesn't block
            # the subprocess.  The reader runs until the process exits.
            self._tasks.spawn(
                self._drain_output(self._proc),
                name="oc_service.drain_stdout",
            )
            logger.info(
                "ai_coding.oc_service.started",
                pid=self._proc.pid,
                port=self._port,
            )
            return await self.status()

    async def stop(self, *, force: bool = False) -> OcServiceStatus:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.returncode is not None:
                return await self.status()

            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
                # Wait briefly for the process to exit; on timeout
                # escalate to kill().
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                # Already gone — idempotent.
                pass

            self._proc = None
            self._started_at = None
            # R-3 — cancel the background stdout drainer (the pipe is
            # gone now the process exited); idempotent.
            await self._tasks.cancel_all()
            logger.info(
                "ai_coding.oc_service.stopped",
                force=force,
            )
            return await self.status()

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
    async def logs(self, *, last_n: int = 100) -> list[str]:
        # Clamp to the buffer cap so a misbehaving caller can't drain
        # the entire history in one call.
        clamped = max(1, min(last_n, _LOG_BUFFER_MAX))
        # Snapshot the deque (it may be mutated concurrently by the
        # drain task; deque is thread-safe for append + iteration but
        # we copy to be defensive against asyncio scheduling).
        snapshot = list(self._log_buffer)
        if clamped >= len(snapshot):
            return snapshot
        return snapshot[-clamped:]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _drain_output(
        self, proc: asyncio.subprocess.Process
    ) -> None:
        """Read stdout into the ring buffer until the process exits."""
        if proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    decoded = line.decode("utf-8", errors="replace").rstrip()
                except Exception:  # noqa: BLE001
                    decoded = repr(line)
                self._log_buffer.append(decoded)
        except Exception as exc:  # noqa: BLE001
            self._log_buffer.append(f"<drain error: {exc!r}>")

    async def _port_reachable(self) -> bool:
        """Return :data:`True` iff a TCP connect to the port succeeds."""
        try:
            fut = asyncio.open_connection(self._hostname, self._port)
            reader, writer = await asyncio.wait_for(
                fut, timeout=self._probe_timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return False
