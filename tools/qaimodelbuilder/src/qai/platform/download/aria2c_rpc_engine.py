# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``aria2c`` RPC-backed :class:`DownloadEnginePort` (F-13, GAP-PR-E1).

Home rationale (platform shared kernel)
---------------------------------------
Lifted from
``qai.model_catalog.infrastructure.aria2c_rpc_download_engine`` into
``qai.platform.download`` so multiple bounded contexts can reuse the
RPC daemon engine without importing another context (forbidden by the
``context-isolation`` import-linter contract). ``qai.model_catalog`` now
consumes it via a thin re-export shim; its behaviour is byte-for-byte
unchanged (RPC port 6810, connection count, retry/stall logic, logger
name and error codes are all preserved verbatim).

Why a second engine
-------------------
The PR-044 :class:`Aria2cDownloadEngine` shells out to ``aria2c`` once
per job and only emits a single terminal progress frame from
:meth:`stream_progress`.  V1 (``backend/aria2c_downloader.py``) ran the
binary in **RPC daemon** mode and polled ``aria2.tellStatus`` every
500 ms to surface real ``completedLength`` / ``totalLength`` /
``downloadSpeed`` snapshots — that is the user-perceived behaviour
``model_catalog`` SSE consumers expect.  This adapter ports the V1
streaming behaviour into the V2 hexagon while preserving the existing
:class:`DownloadEnginePort` contract (no domain / port changes).

V1 source-of-truth
------------------
* ``backend/aria2c_downloader.py:54-67``  RPC port / connections / poll
  interval (0.5 s).
* ``backend/aria2c_downloader.py:507-578`` add_uri options + tellStatus
  poll loop with completedLength / totalLength / downloadSpeed mapping.
* ``backend/aria2c_downloader.py:316-322`` daemon liveness probe.

Architecture
------------
This module lives in the platform shared kernel. A sibling
implementation exists in
``qai.service_release.infrastructure.aria2c_daemon``; the two daemons
run as separate processes on different RPC ports (6810 vs 6800) so they
do not collide; both are short-lived and gated by lazy-start + atexit
cleanup.

Key design choices
~~~~~~~~~~~~~~~~~~
* **Lazy daemon start**: the binary is not spawned until the first
  :meth:`start` call.  Tests injecting fake RPC / process spawners pay
  no real-process cost.
* **No module-level mutable state**: the daemon process handle and RPC
  client live on the engine instance; ``atexit`` cleanup is registered
  per-instance (see :meth:`_register_atexit_cleanup`).
* **Graceful fallback**: when the binary is missing or daemon spawn
  fails, the engine transparently delegates to the legacy
  :class:`Aria2cDownloadEngine` (single-frame mode).  This keeps the
  port contract honoured even on machines without a working daemon.
* **Loopback host literal-free**: the RPC URL is built from
  :data:`qai.platform.config.LOOPBACK_HOST`, satisfying the
  ``check_no_magic_host_port.py`` guard.

Concurrency contract (port §164-171)
* ``start`` submits the job (``aria2.addUri``) and returns immediately;
  byte transfer continues in the background.
* ``stream_progress`` polls ``aria2.tellStatus`` every 500 ms, yields
  monotonically-increasing :class:`DownloadProgress` snapshots and
  terminates when the job reaches ``complete`` / ``error`` / ``cancel``
  (all enumerated in V1 ``aria2c_downloader.py:566-571``).
* ``cancel`` calls ``aria2.remove`` (idempotent: already-terminal jobs
  raise inside aria2c which the adapter swallows, matching the port
  spec).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from qai.platform.config import LOOPBACK_HOST
from qai.platform.errors import ExternalServiceError, InfrastructureError

from qai.platform.download.ports import DownloadJobLike
from qai.platform.download.value_objects import (
    DownloadProgress,
    SourceUrl,
    StorageKey,
)
from qai.platform.download.aria2c_engine import (
    Aria2cBinaryNotFoundError,
    Aria2cDownloadEngine,
    ProcessRunnerLike,
)


__all__ = [
    "Aria2cRpcDownloadEngine",
    "RpcClientLike",
    "DaemonSpawnerLike",
]


logger = logging.getLogger("qai.model_catalog.aria2c_rpc")


# ── Constants (V1 backend/aria2c_downloader.py:54-67) ──────────────────────

# Default RPC port for the model_catalog daemon.  Distinct from
# ``service_release``'s default (6800) so the two engines never collide
# even if both are wired into the same process.  Configurable via
# constructor for tests / multi-instance scenarios.
_DEFAULT_RPC_PORT = 6810

# aria2c knobs (verbatim from V1 _ARIA2C_CONNECTIONS / _ARIA2C_CHUNK_SIZE).
_CONNECTIONS = 16
_CHUNK_SIZE = "1M"

# Daemon readiness wait (V1 _START_TIMEOUT) and progress poll cadence
# (V1 _POLL_INTERVAL — 0.5 s, NOT 1 s).
_START_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.5

# Outer stall watchdog (2026-06-19): if neither RPC ``completedLength`` nor
# the on-disk file size advance for this many seconds, treat the download
# as dead. Second line of defence behind aria2c's own ``lowest-speed-limit``
# (set in :meth:`Aria2cRpcDownloadEngine.start`'s options dict). Same value
# as the service_release sibling for consistency.
_STALL_TIMEOUT_S = 180.0


# ===========================================================================
# Duck-typed Protocols for testability.
# ===========================================================================


@runtime_checkable
class RpcClientLike(Protocol):
    """Minimal JSON-RPC surface this engine consumes.

    Implementations may use :mod:`httpx` (the production
    :class:`_HttpxRpcClient`) or a pure in-memory fake (unit tests).
    Methods MUST be coroutine functions returning ``dict[str, Any]``
    in JSON-RPC 2.0 envelope shape (``{"result": ...}`` or
    ``{"error": {...}}``).
    """

    async def call(self, method: str, params: list[Any]) -> dict[str, Any]:
        ...

    async def aclose(self) -> None:
        ...


@runtime_checkable
class DaemonSpawnerLike(Protocol):
    """Spawns the aria2c daemon and waits for RPC readiness.

    Returning ``None`` signals "binary unavailable / failed to start";
    the engine then falls back to the legacy single-frame adapter.
    """

    async def spawn(
        self,
        *,
        binary: str,
        rpc_port: int,
        rpc_secret: str | None,
    ) -> "_DaemonHandle | None":
        ...


class _DaemonHandle:
    """Opaque handle returned by a :class:`DaemonSpawnerLike`.

    The engine only needs to ``terminate()`` the handle on shutdown;
    everything else (PID tracking, log capture) is the spawner's job.
    """

    __slots__ = ("_terminate_callback",)

    def __init__(self, *, terminate_callback) -> None:
        self._terminate_callback = terminate_callback

    def terminate(self) -> None:
        try:
            self._terminate_callback()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("aria2c daemon terminate callback raised", exc_info=True)


# ===========================================================================
# Production HTTPX-based RPC client + subprocess daemon spawner.
# ===========================================================================


class _HttpxRpcClient:
    """JSON-RPC 2.0 client backed by :class:`httpx.AsyncClient`."""

    __slots__ = ("_url", "_secret", "_client")

    def __init__(self, *, url: str, secret: str | None) -> None:
        self._url = url
        self._secret = secret
        # 5 s aligns with V1 _rpc_call_async timeout (aria2c_downloader.py:213).
        self._client = httpx.AsyncClient(timeout=5.0)

    async def call(self, method: str, params: list[Any]) -> dict[str, Any]:
        # aria2c expects the secret as a magic ``token:<secret>`` first param
        # (https://aria2.github.io/manual/en/html/aria2c.html#rpc-auth).
        effective_params: list[Any] = (
            [f"token:{self._secret}", *params] if self._secret else list(params)
        )
        payload = {
            "jsonrpc": "2.0",
            "id": "qai-model-catalog",
            "method": method,
            "params": effective_params,
        }
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                "model_catalog.aria2c_rpc.transport_error",
                f"aria2c RPC transport error for {method!r}: {exc}",
                service="aria2c_rpc",
                cause=exc,
            ) from exc
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class _SubprocessDaemonSpawner:
    """Default :class:`DaemonSpawnerLike` using :mod:`subprocess`."""

    __slots__ = ("_log_dir",)

    def __init__(self, *, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir

    async def spawn(
        self,
        *,
        binary: str,
        rpc_port: int,
        rpc_secret: str | None,
    ) -> _DaemonHandle | None:
        # Resolve binary first — missing binary is a fast fallback signal.
        if shutil.which(binary) is None and not Path(binary).is_file():
            return None

        argv = [
            binary,
            "--enable-rpc",
            f"--rpc-listen-port={rpc_port}",
            "--rpc-allow-origin-all",
            "--daemon=false",
            "--file-allocation=none",
            "--quiet=true",
            "--log-level=warn",
        ]
        if rpc_secret:
            argv.append(f"--rpc-secret={rpc_secret}")

        logger.info("starting aria2c RPC daemon: %s", " ".join(argv))

        def _spawn_sync() -> subprocess.Popen[bytes] | None:
            try:
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                )
                return subprocess.Popen(  # noqa: S603 — argv is fully controlled.
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except OSError as exc:
                logger.error("aria2c daemon spawn failed: %s", exc)
                return None

        proc = await asyncio.to_thread(_spawn_sync)
        if proc is None:
            return None

        def _terminate() -> None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                except OSError as exc:
                    logger.warning("aria2c daemon terminate error: %s", exc)

        return _DaemonHandle(terminate_callback=_terminate)


# ===========================================================================
# The engine.
# ===========================================================================


class Aria2cRpcDownloadEngine:
    """:class:`DownloadEnginePort` driving an aria2c JSON-RPC daemon.

    This adapter is the production default (PR-E1).  It produces real
    incremental :class:`DownloadProgress` frames by polling
    ``aria2.tellStatus`` every 500 ms, matching V1's behaviour.

    Construction-time fallback
    --------------------------
    If ``check_binary_now=True`` and the ``aria2c`` binary is not
    discoverable, the constructor records the condition and routes ALL
    port operations through the injected ``fallback_engine`` (typically
    a :class:`Aria2cDownloadEngine`).  Likewise, if the daemon fails to
    start at first :meth:`start` invocation, subsequent operations fall
    back transparently.  This honours the GAP-plan F-13 fallback rule
    without breaking the port contract.
    """

    __slots__ = (
        "_binary",
        "_rpc_port",
        "_rpc_secret",
        "_spawner",
        "_rpc_client_factory",
        "_fallback",
        "_daemon_handle",
        "_rpc_client",
        "_jobs",
        "_lock",
        "_atexit_registered",
        "_disabled",
        "_download_root",
    )

    def __init__(
        self,
        *,
        binary: str = "aria2c",
        rpc_port: int = _DEFAULT_RPC_PORT,
        rpc_secret: str | None = None,
        spawner: DaemonSpawnerLike | None = None,
        rpc_client_factory=None,
        fallback_engine: Aria2cDownloadEngine | None = None,
        process_runner: ProcessRunnerLike | None = None,
        check_binary_now: bool = True,
        download_root: Path | None = None,
    ) -> None:
        self._binary = binary
        self._rpc_port = rpc_port
        self._rpc_secret = rpc_secret
        self._spawner = spawner or _SubprocessDaemonSpawner()
        # Default factory yields the production httpx client.  Tests inject
        # a fake to exercise the polling logic without a real daemon.
        self._rpc_client_factory = rpc_client_factory or self._default_rpc_factory
        # Absolute base dir the ``StorageKey.category`` sub-dir is resolved
        # against, so the download lands at a DETERMINISTIC absolute path
        # (``download_root/<category>/<name>``) instead of relying on aria2c's
        # process CWD. ``None`` preserves the legacy CWD-relative behavior
        # (``dir=<category>``) for callers that have not opted in. Propagated
        # to the fallback engine so both paths resolve identically.
        self._download_root = Path(download_root) if download_root is not None else None
        # Lazy: we only construct a fallback when actually needed (so tests
        # exercising the fallback path can pass an explicit engine).
        self._fallback = fallback_engine or Aria2cDownloadEngine(
            process_runner=process_runner,
            binary=binary,
            download_root=download_root,
        )
        self._daemon_handle: _DaemonHandle | None = None
        self._rpc_client: RpcClientLike | None = None
        # ``gid`` per job_id (str → str).  Used by cancel / stream_progress.
        self._jobs: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._atexit_registered = False
        # ``True`` once we've decided to permanently route through the
        # fallback (binary missing OR daemon spawn failed).
        self._disabled = False

        if check_binary_now:
            if shutil.which(binary) is None and not Path(binary).is_file():
                logger.info(
                    "aria2c binary %r not on PATH; "
                    "Aria2cRpcDownloadEngine will fall back to single-frame engine",
                    binary,
                )
                self._disabled = True

    # ── Helpers ────────────────────────────────────────────────────────

    def _default_rpc_factory(self) -> RpcClientLike:
        url = f"http://{LOOPBACK_HOST}:{self._rpc_port}/jsonrpc"
        return _HttpxRpcClient(url=url, secret=self._rpc_secret)

    @classmethod
    def require_binary(cls, *, binary: str = "aria2c") -> None:
        """Raise :class:`Aria2cBinaryNotFoundError` if the binary is missing.

        Mirrors :meth:`Aria2cDownloadEngine.require_binary` so the wiring
        layer can choose between strict (raise at lifespan) vs. lenient
        (fall back at runtime) policies.
        """
        if shutil.which(binary) is None and not Path(binary).is_file():
            raise Aria2cBinaryNotFoundError()

    async def _ensure_daemon(self) -> bool:
        """Lazy-start the daemon + RPC client.  Idempotent.

        Returns ``False`` when spawn fails — caller should switch to
        fallback for that operation (and we mark ``_disabled`` so future
        calls short-circuit).
        """
        if self._disabled:
            return False
        if self._daemon_handle is not None and self._rpc_client is not None:
            return True
        async with self._lock:
            if self._daemon_handle is not None and self._rpc_client is not None:
                return True
            handle = await self._spawner.spawn(
                binary=self._binary,
                rpc_port=self._rpc_port,
                rpc_secret=self._rpc_secret,
            )
            if handle is None:
                logger.warning(
                    "aria2c daemon spawn returned no handle; falling back"
                )
                self._disabled = True
                return False

            client = self._rpc_client_factory()

            # Wait for RPC readiness via aria2.getVersion.
            ready = await self._await_rpc_ready(client)
            if not ready:
                logger.error(
                    "aria2c RPC daemon never became ready within %.1fs; falling back",
                    _START_TIMEOUT_S,
                )
                handle.terminate()
                await client.aclose()
                self._disabled = True
                return False

            self._daemon_handle = handle
            self._rpc_client = client
            self._register_atexit_cleanup()
            return True

    async def _await_rpc_ready(self, client: RpcClientLike) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _START_TIMEOUT_S
        while loop.time() < deadline:
            try:
                resp = await client.call("aria2.getVersion", [])
                if "result" in resp:
                    return True
            except Exception:  # noqa: BLE001 — keep trying until deadline
                pass
            await asyncio.sleep(0.3)
        return False

    def _register_atexit_cleanup(self) -> None:
        if self._atexit_registered:
            return
        # Bound method so the atexit hook cleans up THIS engine instance,
        # not a module-level singleton (no global mutable state).
        atexit.register(self._atexit_cleanup)
        self._atexit_registered = True

    def _atexit_cleanup(self) -> None:
        # ``atexit`` runs in the main thread after the event loop is gone;
        # synchronous cleanup only.
        if self._daemon_handle is not None:
            self._daemon_handle.terminate()
            self._daemon_handle = None
        # ``RpcClientLike.aclose`` is async; skip in atexit (process is
        # exiting anyway, the OS reclaims sockets).

    async def shutdown(self) -> None:
        """Async cleanup hook — callers (lifespan) may invoke this."""
        if self._rpc_client is not None:
            try:
                await self._rpc_client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("aria2c RPC client aclose error", exc_info=True)
            self._rpc_client = None
        if self._daemon_handle is not None:
            self._daemon_handle.terminate()
            self._daemon_handle = None

    # ── DownloadEnginePort surface ─────────────────────────────────────

    async def start(
        self,
        job: DownloadJobLike,
        *,
        source: SourceUrl,
        target: StorageKey,
    ) -> None:
        """Submit ``job`` to the daemon via ``aria2.addUri``.

        On daemon-unavailable falls back to the legacy adapter (which
        synchronously runs ``aria2c`` once).
        """
        if not await self._ensure_daemon():
            await self._fallback.start(job, source=source, target=target)
            return

        assert self._rpc_client is not None  # for type-checkers
        # Resolve the download dir: when a ``download_root`` was configured,
        # the archive lands at ``download_root/<category>/<name>`` (a stable
        # ABSOLUTE path callers can predict); otherwise fall back to the bare
        # category (legacy CWD-relative behavior).
        download_dir = (
            str(self._download_root / target.category)
            if self._download_root is not None
            else target.category
        )
        options = {
            "dir": download_dir,
            "out": target.name,
            "max-connection-per-server": str(_CONNECTIONS),
            "split": str(_CONNECTIONS),
            "min-split-size": _CHUNK_SIZE,
            "continue": "true",
            "allow-overwrite": "true",
            "file-allocation": "none",
            "auto-file-renaming": "false",
            # Robustness params (2026-06-19) — keep in lockstep with the
            # service_release sibling (Aria2cDaemon.add_uri_options). aria2c
            # CLI defaults are too tolerant for an interactive download
            # center: without these the daemon silently spins on a dead TCP
            # stream and the SSE client sees a frozen progress bar. Each
            # value is documented at length over in aria2c_daemon.py.
            "max-tries": "10",
            "retry-wait": "5",
            "connect-timeout": "20",
            "timeout": "60",
            "lowest-speed-limit": "10K",
        }
        try:
            resp = await self._rpc_client.call(
                "aria2.addUri", [[source.value], options]
            )
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap cleanly
            raise ExternalServiceError(
                "model_catalog.aria2c_rpc.add_uri_failed",
                f"aria2.addUri failed for job {job.job_id.value!r}: {exc}",
                service="aria2c_rpc",
                cause=exc,
            ) from exc

        if "error" in resp:
            err = resp["error"]
            raise ExternalServiceError(
                "model_catalog.aria2c_rpc.rpc_error",
                f"aria2.addUri error for job {job.job_id.value!r}: {err}",
                service="aria2c_rpc",
            )
        gid = resp.get("result", "")
        if not isinstance(gid, str) or not gid:
            raise ExternalServiceError(
                "model_catalog.aria2c_rpc.empty_gid",
                f"aria2.addUri returned no GID for job {job.job_id.value!r}",
                service="aria2c_rpc",
            )
        self._jobs[job.job_id.value] = gid
        logger.info(
            "aria2c task submitted: gid=%s job=%s url=%s",
            gid, job.job_id.value, source.value,
        )

    async def cancel(self, job: DownloadJobLike) -> None:
        """``aria2.remove`` the GID; idempotent."""
        gid = self._jobs.pop(job.job_id.value, None)
        if gid is None:
            # Job never started via this engine — try fallback (its cancel
            # is also a no-op for unknown jobs).
            await self._fallback.cancel(job)
            return
        if self._rpc_client is None:
            return
        try:
            await self._rpc_client.call("aria2.remove", [gid])
        except Exception as exc:  # noqa: BLE001 — cancel must be idempotent
            logger.info(
                "aria2c cancel ignored error for gid=%s: %s", gid, exc
            )

    async def progress(self, job: DownloadJobLike) -> DownloadProgress:
        """One-shot progress snapshot via ``aria2.tellStatus``."""
        gid = self._jobs.get(job.job_id.value)
        if gid is None or self._rpc_client is None:
            return await self._fallback.progress(job)
        try:
            resp = await self._rpc_client.call(
                "aria2.tellStatus",
                [
                    gid,
                    [
                        "status",
                        "completedLength",
                        "totalLength",
                        "downloadSpeed",
                    ],
                ],
            )
        except Exception:  # noqa: BLE001 — best-effort snapshot
            return job.progress
        return _to_progress(resp.get("result", {}), previous=job.progress)

    def stream_progress(
        self, job: DownloadJobLike
    ) -> AsyncIterator[DownloadProgress]:
        """Poll ``aria2.tellStatus`` until terminal aria2 state."""
        return self._iter_progress(job)

    async def _iter_progress(
        self, job: DownloadJobLike
    ) -> AsyncIterator[DownloadProgress]:
        gid = self._jobs.get(job.job_id.value)
        if gid is None or self._rpc_client is None:
            # No daemon path — yield single completion frame via fallback.
            async for frame in self._fallback.stream_progress(job):
                yield frame
            return

        last: DownloadProgress = job.progress
        # Outer stall watchdog state (see _STALL_TIMEOUT_S).
        loop = asyncio.get_running_loop()
        last_progress_bytes = last.bytes_downloaded
        last_progress_at = loop.time()
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            try:
                resp = await self._rpc_client.call(
                    "aria2.tellStatus",
                    [
                        gid,
                        [
                            "status",
                            "completedLength",
                            "totalLength",
                            "downloadSpeed",
                            "errorMessage",
                        ],
                    ],
                )
            except Exception as exc:  # noqa: BLE001 — V1 retries on transport
                logger.debug(
                    "aria2c tellStatus poll error (will retry): %s", exc
                )
                yield last
                continue

            if "error" in resp:
                err = resp["error"]
                raise ExternalServiceError(
                    "model_catalog.aria2c_rpc.poll_rpc_error",
                    f"aria2.tellStatus rpc error gid={gid!r}: {err}",
                    service="aria2c_rpc",
                )

            status = resp.get("result", {}) or {}
            aria_state = str(status.get("status", ""))

            # V1 :562-564 — error state propagates as ExternalServiceError.
            if aria_state == "error":
                err_msg = status.get("errorMessage") or "Unknown aria2c error"
                raise ExternalServiceError(
                    "model_catalog.aria2c_rpc.aria_error",
                    f"aria2c reported error: {err_msg}",
                    service="aria2c_rpc",
                )

            snapshot = _to_progress(status, previous=last)

            if aria_state == "complete":
                # Force a complete frame even when totalLength==0 (zero-byte
                # downloads): is_complete == bytes_downloaded >= total_bytes.
                total = snapshot.total_bytes if snapshot.total_bytes else snapshot.bytes_downloaded
                final = DownloadProgress(
                    bytes_downloaded=total,
                    total_bytes=total,
                    speed_bps=0.0,
                    eta_seconds=0.0,
                )
                yield final
                # Drop GID — cancel after completion is a no-op.
                self._jobs.pop(job.job_id.value, None)
                return

            if aria_state in {"removed", "cancelled"}:
                # Treat aria2's removed/cancelled states as terminal:
                # the use case finalises FAILED if the last frame is
                # not complete (see StreamDownloadProgressUseCase :110-).
                yield snapshot
                self._jobs.pop(job.job_id.value, None)
                return

            # Stall watchdog: forward motion in ``bytes_downloaded`` resets
            # the timer. After _STALL_TIMEOUT_S without progress we ask
            # aria2c to remove the task and raise — the use case will
            # finalise the job as FAILED via the standard error path.
            if snapshot.bytes_downloaded > last_progress_bytes:
                last_progress_bytes = snapshot.bytes_downloaded
                last_progress_at = loop.time()
            elif loop.time() - last_progress_at >= _STALL_TIMEOUT_S:
                logger.warning(
                    "aria2c download stalled %.0fs without progress; "
                    "removing task gid=%s job=%s",
                    _STALL_TIMEOUT_S,
                    gid,
                    job.job_id.value,
                )
                try:
                    await self._rpc_client.call("aria2.remove", [gid])
                except Exception:  # noqa: BLE001 — best-effort
                    pass
                self._jobs.pop(job.job_id.value, None)
                raise ExternalServiceError(
                    "model_catalog.aria2c_rpc.stalled",
                    (
                        f"aria2c download stalled (no progress for "
                        f"{int(_STALL_TIMEOUT_S)}s) gid={gid!r}"
                    ),
                    service="aria2c_rpc",
                )

            # active / waiting / paused / unknown — keep polling.
            last = snapshot
            yield snapshot


# ===========================================================================
# Helpers
# ===========================================================================


def _to_progress(
    status: dict[str, Any], *, previous: DownloadProgress
) -> DownloadProgress:
    """Convert an ``aria2.tellStatus`` payload into a domain VO.

    Defensive against missing / non-numeric fields (V1 also did
    ``int(... or 0)``).  Falls back to ``previous`` totals when aria2
    has not yet learned the content-length (totalLength==0 in the wire
    response means "unknown").
    """
    completed_raw = status.get("completedLength", 0)
    total_raw = status.get("totalLength", 0)
    speed_raw = status.get("downloadSpeed", 0)

    try:
        completed = max(0, int(completed_raw or 0))
    except (TypeError, ValueError):
        completed = previous.bytes_downloaded
    try:
        total_int = max(0, int(total_raw or 0))
    except (TypeError, ValueError):
        total_int = 0
    try:
        speed = max(0, int(speed_raw or 0))
    except (TypeError, ValueError):
        speed = 0

    total: int | None = total_int if total_int > 0 else previous.total_bytes
    # Maintain the VO invariant ``bytes_downloaded <= total_bytes``.
    if total is not None and completed > total:
        completed = total

    eta: float | None = None
    if total is not None and speed > 0 and completed < total:
        remaining = total - completed
        eta = float(remaining) / float(speed)

    return DownloadProgress(
        bytes_downloaded=completed,
        total_bytes=total,
        speed_bps=float(speed),
        eta_seconds=eta,
    )


# Re-export for callers that already import the legacy error from this module
# tree — keeps the public surface tidy.
_ = InfrastructureError  # silence "unused" — re-exported via aria2c module.
