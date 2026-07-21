# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Streaming download engine: aria2c (preferred) with httpx fallback.

Ports the V1 ``stream_download`` algorithm (``backend/version_manager.py``
/ ``model_catalog_manager.py`` / ``aria2c_downloader.py``): try aria2c RPC
first when available, otherwise fall back to a single-connection httpx
stream. The backend does *not* compute speed/ETA — the frontend derives
those from successive ``downloaded_bytes`` deltas, so the frames only need
accurate ``downloaded_bytes`` / ``total_bytes`` / ``percent`` / ``status`` /
``engine``.

Cancellation: a per-task ``asyncio.Event`` lets :class:`Aria2cManager`
(or SSE disconnect) request a stop; the httpx loop checks it between
chunks and yields a ``cancelled`` terminal frame. For aria2c tasks the
event also triggers an ``aria2.remove`` of the active gid.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from qai.service_release.application.ports import DownloadEnginePort, DownloadSettingsPort
from qai.service_release.infrastructure.aria2c_daemon import (
    POLL_INTERVAL,
    Aria2cDaemon,
)
from qai.service_release.infrastructure.download_paths import DownloadPaths
from qai.service_release.domain.value_objects import (
    DownloadEngineKind,
    DownloadProgress,
    DownloadStatus,
)

logger = logging.getLogger("qai.service_release.download_engine")

_CHUNK_SIZE = 65536

# Outer stall watchdog (2026-06-19): if ``downloaded`` does not advance for
# this many seconds AND the file size on disk also does not change, treat the
# download as dead and emit an ERROR frame. This is the second line of defence
# behind aria2c's own ``lowest-speed-limit`` (``Aria2cDaemon.add_uri_options``):
# aria2c usually trips its own retry loop first, but if the daemon RPC keeps
# returning a stale ``completedLength`` (or aria2c is itself wedged), the
# outer watchdog still gets us unstuck. 90 s is a good balance between
# "tolerate a slow connection blip" and "don't make users wait minutes on a
# truly dead download".
_STALL_TIMEOUT = 180.0


def _filename_from_url(url: str) -> str:
    name = url.split("?")[0].rstrip("/").split("/")[-1]
    return name or "download"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


class HttpxDownloadEngine(DownloadEnginePort):
    """Streaming download engine: aria2c (when available) → httpx fallback.

    When an :class:`Aria2cDaemon` is wired and reports ``available`` the
    engine streams via the aria2c RPC daemon (multi-connection, resumable).
    Otherwise it downloads directly over httpx. Both paths are fully
    functional on their own; aria2c is a pure acceleration layer.
    """

    __slots__ = (
        "_paths",
        "_settings",
        "_cancels",
        "_client_factory",
        "_aria2c",
        "_proxy_provider",
        "_active",
    )

    def __init__(
        self,
        *,
        paths: DownloadPaths,
        settings: DownloadSettingsPort,
        client_factory: type[httpx.AsyncClient] | None = None,
        aria2c: Aria2cDaemon | None = None,
        proxy_provider: "Callable[[], str | None] | None" = None,
    ) -> None:
        self._paths = paths
        self._settings = settings
        self._cancels: dict[str, asyncio.Event] = {}
        self._client_factory = client_factory or httpx.AsyncClient
        self._aria2c = aria2c
        # H-2: live global proxy URL provider (or None). Injected by apps DI
        # so service_release stays import-isolated from the settings/chat
        # contexts. ``_proxy_url()`` reads it at call time → hot-applies.
        self._proxy_provider = proxy_provider
        # M-4 (State-Truth): latest streamed DownloadProgress per in-flight
        # task id, so a reconnecting client can query "what's downloading"
        # after an SSE disconnect. Reflects the engine's REAL in-flight tasks
        # (updated as frames stream, cleared in ``finally``), never an
        # optimistic cache.
        self._active: dict[str, DownloadProgress] = {}

    def active_downloads(self) -> list[DownloadProgress]:
        """Return a snapshot of the latest progress for each in-flight task.

        M-4 — State-Truth: only tasks the engine is actually streaming right
        now appear here (entries are removed when the stream ends).
        """
        return list(self._active.values())

    def _proxy_url(self) -> str:
        if self._proxy_provider is None:
            return ""
        try:
            return (self._proxy_provider() or "").strip()
        except Exception:  # noqa: BLE001 - never break a download on read
            return ""

    def request_cancel(self, task_id: str) -> bool:
        """Signal an in-flight download to stop. Returns True if one existed."""
        ev = self._cancels.get(task_id)
        if ev is not None:
            ev.set()
            return True
        return False

    async def stream_download(
        self,
        *,
        task_id: str,
        sub_dir: str,
        download_url: str,
        checksum_sha256: str = "",
    ) -> AsyncIterator[DownloadProgress]:
        cfg = await self._settings.read()
        # ABSOLUTE save dir: the rest of the app (scan, install, aria2c daemon
        # spawned without an explicit cwd) must all agree on ONE location.
        # ``download_dir`` derives from the relative default ``data_dir`` =
        # ``Path("data")``, so resolve here to pin every consumer to the same
        # absolute path regardless of the process cwd (AGENTS.md 🔴 §铁律4).
        save_dir = (self._paths.download_dir / sub_dir).resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = _filename_from_url(download_url)
        save_path = save_dir / filename

        cancel = asyncio.Event()
        self._cancels[task_id] = cancel
        try:
            # Prefer aria2c when wired + available (auto-installs on first use).
            if self._aria2c is not None and self._aria2c.available:
                async for frame in self._stream_aria2c(
                    task_id=task_id,
                    save_dir=save_dir,
                    save_path=save_path,
                    filename=filename,
                    download_url=download_url,
                    checksum_sha256=checksum_sha256,
                    cancel=cancel,
                ):
                    self._active[task_id] = frame  # M-4: track latest progress
                    yield frame
                return
            # httpx fallback.
            async for frame in self._stream_httpx(
                task_id=task_id,
                cfg=cfg,
                save_path=save_path,
                filename=filename,
                download_url=download_url,
                checksum_sha256=checksum_sha256,
                cancel=cancel,
            ):
                self._active[task_id] = frame  # M-4: track latest progress
                yield frame
        finally:
            self._cancels.pop(task_id, None)
            self._active.pop(task_id, None)

    # ── aria2c path (V1 aria2c_downloader.stream_download) ────────────────

    async def _stream_aria2c(
        self,
        *,
        task_id: str,
        save_dir: Path,
        save_path: Path,
        filename: str,
        download_url: str,
        checksum_sha256: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[DownloadProgress]:
        daemon = self._aria2c
        assert daemon is not None
        ctrl_path = save_dir / (filename + ".aria2")

        def frame(
            status: DownloadStatus,
            *,
            downloaded: int = 0,
            total: int = 0,
            error: str = "",
        ) -> DownloadProgress:
            return DownloadProgress(
                task_id=task_id,
                filename=filename,
                downloaded_bytes=downloaded,
                total_bytes=total,
                status=status,
                error=error,
                save_path=str(save_path),
                engine=DownloadEngineKind.ARIA2C,
            )

        # Auto-install hint (V1 :481-484): emit a preparing frame while the
        # binary is being fetched on first use.
        if (
            not daemon.exe_path
            and daemon.can_auto_install
        ):
            yield frame(DownloadStatus.PREPARING)

        if not await daemon.ensure_binary():
            # Binary unavailable → fall back to httpx (graceful degradation).
            logger.info("aria2c unavailable (%s); httpx fallback", daemon.install_error)
            cfg = await self._settings.read()
            async for f in self._stream_httpx(
                task_id=task_id,
                cfg=cfg,
                save_path=save_path,
                filename=filename,
                download_url=download_url,
                checksum_sha256=checksum_sha256,
                cancel=cancel,
            ):
                yield f
            return

        ready = await asyncio.to_thread(daemon.ensure_daemon)
        if not ready:
            yield frame(DownloadStatus.ERROR, error="aria2c daemon failed to start")
            return

        # Deterministic orphan cleanup: if a previous run left the target file
        # WITHOUT its ``.aria2`` control file, aria2c aborts ("File ... exists,
        # but a control file (*.aria2) does not exist."). ``allow-overwrite``
        # normally covers this, but a stale daemon process (started by an older
        # build) may not apply our per-request options — so we remove the orphan
        # ourselves before re-queuing. We only delete when the control file is
        # absent (a present ``.aria2`` means a resumable partial: keep it).
        if save_path.exists() and not ctrl_path.exists():
            try:
                save_path.unlink()
                logger.info(
                    "removed orphan download (no .aria2 control file): %s",
                    save_path,
                )
            except OSError as exc:  # noqa: BLE001 — non-fatal; aria2c may still cope
                logger.warning("failed to remove orphan %s: %s", save_path, exc)

        downloaded = 0
        total = 0
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                options = daemon.add_uri_options(save_dir, filename)
                # H-2: route the aria2c download through the global proxy
                # when configured (V1 parity: downloads honoured the global
                # proxy). ``all-proxy`` covers http/https/ftp uniformly.
                proxy_url = self._proxy_url()
                if proxy_url:
                    options = {**options, "all-proxy": proxy_url}
                result = await daemon.rpc(
                    client, "aria2.addUri", [[download_url], options]
                )
                if "error" in result:
                    yield frame(
                        DownloadStatus.ERROR,
                        error=f"aria2c addUri error: {result['error']}",
                    )
                    return
                gid = result.get("result", "")
                if not gid:
                    yield frame(DownloadStatus.ERROR, error="aria2c returned empty GID")
                    return
                logger.info("aria2c task gid=%s url=%s", gid, download_url)

                # Outer stall watchdog state (see _STALL_TIMEOUT).
                # We track the last *forward progress* moment using both the
                # RPC ``completedLength`` AND the on-disk file size, because
                # either signal can lag the other (RPC can cache a frame; the
                # filesystem can buffer chunks). Any forward motion in either
                # resets the timer.
                loop = asyncio.get_running_loop()
                last_progress_bytes = 0
                last_progress_at = loop.time()

                while True:
                    await asyncio.sleep(POLL_INTERVAL)

                    # Cancellation (SSE disconnect or explicit request).
                    if cancel.is_set():
                        try:
                            await daemon.rpc(client, "aria2.remove", [gid])
                        except Exception:  # noqa: BLE001
                            pass
                        yield frame(
                            DownloadStatus.CANCELLED,
                            downloaded=downloaded,
                            total=total,
                        )
                        return

                    # Completion: control file gone + target present (V1 :538).
                    if save_path.exists() and not ctrl_path.exists():
                        downloaded = save_path.stat().st_size
                        total = downloaded
                        break

                    try:
                        status_result = await daemon.rpc(
                            client,
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
                        task_status = status_result.get("result", {})
                        aria2_status = task_status.get("status", "")
                        downloaded = int(task_status.get("completedLength", 0) or 0)
                        t = int(task_status.get("totalLength", 0) or 0)
                        if t > 0:
                            total = t

                        if aria2_status == "error":
                            err = task_status.get("errorMessage", "Unknown aria2c error")
                            yield frame(
                                DownloadStatus.ERROR,
                                downloaded=downloaded,
                                total=total,
                                error=f"aria2c error: {err}",
                            )
                            return
                        if aria2_status == "complete":
                            break

                        # Stall watchdog: cross-check RPC ``downloaded`` AND
                        # on-disk size. The disk-size probe makes us robust
                        # against an aria2c that's still receiving bytes but
                        # whose RPC is stuck on a stale frame, and against an
                        # aria2c whose RPC works fine but whose file write
                        # buffer is wedged. Forward motion in EITHER signal
                        # resets the timer (most permissive — we err on the
                        # side of tolerance, the inner aria2c lowest-speed
                        # limit catches anything truly degenerate).
                        on_disk = (
                            save_path.stat().st_size if save_path.exists() else 0
                        )
                        progress_marker = max(downloaded, on_disk)
                        if progress_marker > last_progress_bytes:
                            last_progress_bytes = progress_marker
                            last_progress_at = loop.time()
                        elif loop.time() - last_progress_at >= _STALL_TIMEOUT:
                            logger.warning(
                                "aria2c download stalled %.0fs without progress; "
                                "removing task gid=%s",
                                _STALL_TIMEOUT,
                                gid,
                            )
                            try:
                                await daemon.rpc(client, "aria2.remove", [gid])
                            except Exception:  # noqa: BLE001
                                pass
                            yield frame(
                                DownloadStatus.ERROR,
                                downloaded=downloaded,
                                total=total,
                                error=(
                                    f"Download stalled (no progress for "
                                    f"{int(_STALL_TIMEOUT)}s)"
                                ),
                            )
                            return

                        yield frame(
                            DownloadStatus.DOWNLOADING,
                            downloaded=downloaded,
                            total=total,
                        )
                    except httpx.RequestError as exc:
                        logger.warning("aria2c RPC poll error (retry): %s", exc)
                        yield frame(
                            DownloadStatus.DOWNLOADING,
                            downloaded=downloaded,
                            total=total,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.exception("aria2c download error (task=%s)", task_id)
            yield frame(DownloadStatus.ERROR, error=f"Unexpected error: {exc}")
            return

        # Checksum verification (V1 :598-618).
        if checksum_sha256.strip() and save_path.exists():
            actual = await asyncio.to_thread(_sha256_file, save_path)
            if actual.lower() != checksum_sha256.strip().lower():
                save_path.unlink(missing_ok=True)
                yield frame(
                    DownloadStatus.ERROR,
                    error=(
                        f"Checksum mismatch: expected {checksum_sha256}, got {actual}"
                    ),
                )
                return

        if downloaded == 0 and save_path.exists():
            downloaded = save_path.stat().st_size
        yield frame(
            DownloadStatus.DONE,
            downloaded=downloaded,
            total=total if total > 0 else downloaded,
        )

    # ── httpx path (single-connection fallback) ───────────────────────────

    async def _stream_httpx(
        self,
        *,
        task_id: str,
        cfg,
        save_path: Path,
        filename: str,
        download_url: str,
        checksum_sha256: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[DownloadProgress]:
        downloaded = 0
        total = 0
        try:
            # H-2: honour the global proxy on the httpx fallback path (V1
            # ``model_catalog_manager.py:423-429`` used get_httpx_proxy_kwargs).
            _proxy = self._proxy_url()
            _proxy_kwargs = {"proxy": _proxy} if _proxy else {}
            async with self._client_factory(
                timeout=httpx.Timeout(cfg.download_timeout_seconds, connect=30),
                follow_redirects=True,
                verify=cfg.ssl_verify,
                **_proxy_kwargs,
            ) as client:
                async with client.stream("GET", download_url) as response:
                    if response.status_code != 200:
                        body = (await response.aread())[:200].decode(
                            "utf-8", "replace"
                        )
                        yield DownloadProgress(
                            task_id=task_id,
                            filename=filename,
                            status=DownloadStatus.ERROR,
                            error=f"HTTP {response.status_code}: {body}",
                            engine=DownloadEngineKind.HTTPX,
                        )
                        return
                    cl = response.headers.get("content-length")
                    if cl and cl.isdigit():
                        total = int(cl)
                    with save_path.open("wb") as fh:
                        async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                            if cancel.is_set():
                                yield DownloadProgress(
                                    task_id=task_id,
                                    filename=filename,
                                    downloaded_bytes=downloaded,
                                    total_bytes=total,
                                    status=DownloadStatus.CANCELLED,
                                    save_path=str(save_path),
                                    engine=DownloadEngineKind.HTTPX,
                                )
                                return
                            fh.write(chunk)
                            downloaded += len(chunk)
                            yield DownloadProgress(
                                task_id=task_id,
                                filename=filename,
                                downloaded_bytes=downloaded,
                                total_bytes=total,
                                status=DownloadStatus.DOWNLOADING,
                                save_path=str(save_path),
                                engine=DownloadEngineKind.HTTPX,
                            )
        except httpx.HTTPError as exc:
            yield DownloadProgress(
                task_id=task_id,
                filename=filename,
                downloaded_bytes=downloaded,
                total_bytes=total,
                status=DownloadStatus.ERROR,
                error=str(exc),
                engine=DownloadEngineKind.HTTPX,
            )
            return

        # Checksum verification (V1: delete + error on mismatch).
        if checksum_sha256.strip():
            actual = await asyncio.to_thread(_sha256_file, save_path)
            if actual.lower() != checksum_sha256.strip().lower():
                try:
                    save_path.unlink(missing_ok=True)
                except OSError:
                    pass
                yield DownloadProgress(
                    task_id=task_id,
                    filename=filename,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    status=DownloadStatus.ERROR,
                    error=(
                        f"Checksum mismatch: expected {checksum_sha256}, "
                        f"got {actual}"
                    ),
                    engine=DownloadEngineKind.HTTPX,
                )
                return

        yield DownloadProgress(
            task_id=task_id,
            filename=filename,
            downloaded_bytes=downloaded,
            total_bytes=total if total > 0 else downloaded,
            status=DownloadStatus.DONE,
            save_path=str(save_path),
            engine=DownloadEngineKind.HTTPX,
        )


__all__ = ["HttpxDownloadEngine"]
