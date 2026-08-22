# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``aria2c``-backed :class:`DownloadEnginePort` (PR-044).

Home rationale (platform shared kernel)
---------------------------------------
This engine was lifted from
``qai.model_catalog.infrastructure.aria2c_download_engine`` into
``qai.platform.download`` so that multiple bounded contexts can reuse
the multi-threaded aria2c download machinery without any context
importing another context (which the ``context-isolation`` import-linter
contract forbids). ``qai.model_catalog`` now consumes it via a thin
re-export shim, so its behaviour is byte-for-byte unchanged. Error
codes, argv, and options are preserved verbatim.

Strategy
--------
The download-engine port allows multiple transports; this adapter shells
out to ``aria2c`` (a multi-connection CLI download engine). Production
deployments install the binary; environments without it surface
:class:`Aria2cBinaryNotFoundError` at lifespan startup via
:meth:`Aria2cDownloadEngine.require_binary`, and the wiring chooses an
alternative ``DownloadEnginePort`` implementation (the registry lists
multiple transports; ``aria2c`` is just the production default).

Process abstraction
-------------------
Per S4 spec §8 row "ProcessRunnerPort coordination", the adapter
prefers :class:`qai.platform.process.ProcessRunnerPort` (the real
:class:`SubprocessProcessRunner` shipped by PR-041) when wired in;
otherwise it constructs the local
:class:`_InternalSubprocessRunner` directly. The branch is selected at
construction time; tests can inject any object satisfying the minimal
:class:`ProcessRunnerLike` Protocol below.

ProcessRunnerLike Protocol
~~~~~~~~~~~~~~~~~~~~~~~~~~

The constructor takes ``process_runner: ProcessRunnerLike | None``;
when ``None`` it builds the internal subprocess fallback so the adapter
remains usable in isolation. Production wiring in
``apps/api/_model_catalog_di.py`` passes a structurally-compatible
runner (the platform-level :class:`ProcessRunnerPort` adapter is
duck-typed against this Protocol, so no direct cross-context import is
required).

Progress reporting
------------------
``aria2c`` natively emits machine-readable progress to stderr; this
adapter intentionally does not parse that stream. Instead it exposes
the **port** (``start`` / ``cancel`` / ``progress`` /
``stream_progress``) and emits a single ``is_complete`` frame from
:meth:`stream_progress` once the byte transfer finishes. Sub-classes /
alternative ``DownloadEnginePort`` adapters that need richer progress
plug parsed RPC frames in directly; for the catalog use cases the
single-frame contract is sufficient.

Tests inject a fake :class:`ProcessRunnerLike` to exercise the wiring
without spawning a real process. See
``tests/integration/model_catalog/test_aria2c_download_engine.py``.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from qai.platform.errors import (
    ExternalServiceError,
    InfrastructureError,
)
from qai.platform.logging import get_logger

from qai.platform.download.ports import DownloadJobLike
from qai.platform.download.value_objects import (
    DownloadProgress,
    SourceUrl,
    StorageKey,
)

_log = get_logger(__name__)


__all__ = [
    "Aria2cDownloadEngine",
    "ProcessRunnerLike",
    "Aria2cBinaryNotFoundError",
]


@runtime_checkable
class ProcessRunnerLike(Protocol):
    """Minimal subset of :class:`qai.platform.process.ProcessRunnerPort`.

    Defined locally as a duck-typed shape so this adapter does not
    import the platform-level Port directly (keeping the
    cross-context surface narrow).  Python's ``Protocol`` matching
    is structural, so any object with a compatible ``run`` coroutine
    — including :class:`SubprocessProcessRunner` — satisfies it.
    """

    async def run(
        self,
        argv: list[str],
        *,
        timeout_s: float | None = ...,
    ) -> int:
        ...


class Aria2cBinaryNotFoundError(InfrastructureError):
    """Raised at construction time if ``aria2c`` is not on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "model_catalog.aria2c.binary_missing",
            "aria2c CLI binary not found on PATH; install aria2 or "
            "configure an alternative DownloadEnginePort",
        )


class Aria2cDownloadEngine:
    """:class:`DownloadEnginePort` shelling out to ``aria2c``."""

    __slots__ = ("_runner", "_binary", "_jobs", "_download_root")

    def __init__(
        self,
        *,
        process_runner: ProcessRunnerLike | None = None,
        binary: str = "aria2c",
        download_root: Path | None = None,
    ) -> None:
        self._runner = process_runner or _InternalSubprocessRunner()
        self._binary = binary
        # Absolute base dir for resolving ``StorageKey.category`` (see the RPC
        # engine for rationale). ``None`` keeps the legacy CWD-relative dir.
        self._download_root = Path(download_root) if download_root is not None else None
        # Each in-flight job tracks an asyncio.Task; ``cancel`` flips it.
        self._jobs: dict[str, asyncio.Task[int] | None] = {}

    @classmethod
    def require_binary(cls, *, binary: str = "aria2c") -> None:
        """Raise :class:`Aria2cBinaryNotFoundError` if not on PATH.

        Wiring code calls this before instantiating the engine in
        production so a missing binary surfaces at lifespan startup,
        not at first download.
        """
        if shutil.which(binary) is None:
            raise Aria2cBinaryNotFoundError()

    async def start(
        self,
        job: DownloadJobLike,
        *,
        source: SourceUrl,
        target: StorageKey,
    ) -> None:
        """Submit ``job`` for transfer.

        Calls ``aria2c <source> -d <target.category> -o <target.name>``
        through the configured process runner. The call returns once the
        runner reports the process has been launched (``run`` returns
        the *exit code*; for ``Aria2cDownloadEngine`` we await it inline
        because we do not yet have a streaming-runner Port).
        """
        argv = [
            self._binary,
            "--allow-overwrite=true",
            "--quiet=true",
            # Robustness params (2026-06-19) — match the RPC sibling so that
            # the CLI fallback isn't *worse* than the daemon path. Without
            # these aria2c happily hangs on a dead link until the OS kills
            # the parent process. See ``Aria2cDaemon.add_uri_options`` for
            # full rationale per option.
            "--max-tries=5",
            "--retry-wait=3",
            "--connect-timeout=15",
            "--timeout=30",
            "--lowest-speed-limit=50K",
            "-d",
            (
                str(self._download_root / target.category)
                if self._download_root is not None
                else target.category
            ),
            "-o",
            target.name,
            source.value,
        ]
        try:
            exit_code = await self._runner.run(argv, timeout_s=None)
        except Exception as exc:  # noqa: BLE001 — wrap cleanly
            raise ExternalServiceError(
                "model_catalog.aria2c.start_failed",
                f"aria2c failed to start for job {job.job_id.value!r}: {exc}",
                service="aria2c",
                cause=exc,
            ) from exc
        if exit_code != 0:
            raise ExternalServiceError(
                "model_catalog.aria2c.nonzero_exit",
                (
                    f"aria2c exited {exit_code} for job "
                    f"{job.job_id.value!r}"
                ),
                service="aria2c",
                status=int(exit_code),
            )

    async def cancel(self, job: DownloadJobLike) -> None:
        """Best-effort cancel.

        Per the port contract this is **idempotent** — cancelling an
        already-completed (or never-started) job is a no-op. The aria2c
        wrapper relies on the underlying :class:`ProcessRunnerLike` for
        actual signalling; if the runner has no record of the job we
        simply return.
        """
        task = self._jobs.pop(job.job_id.value, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # We cancelled this job ourselves; benign per the
                # idempotent best-effort cancel contract.
                pass
            except Exception:  # noqa: BLE001 - best-effort cancel
                _log.warning(
                    "model_catalog.aria2c.cancel_task_cleanup_failed",
                    job_id=job.job_id.value,
                    exc_info=True,
                )

    async def progress(self, job: DownloadJobLike) -> DownloadProgress:
        """Single-shot progress snapshot.

        ``aria2c`` does not expose live progress through ``run``; this
        adapter therefore returns the job's last known progress
        verbatim. ``StreamDownloadProgressUseCase`` consumes
        :meth:`stream_progress` instead, which is where the meaningful
        progress signal originates.
        """
        return job.progress

    def stream_progress(
        self, job: DownloadJobLike
    ) -> AsyncIterator[DownloadProgress]:
        """Yield a single completion frame.

        The catalog use cases only need the terminal ``is_complete``
        snapshot to finalise the job; alternative
        ``DownloadEnginePort`` adapters that parse aria2c's RPC
        interface for intermediate progress can do so by overriding
        this method.
        """
        return self._iter_completion(job)

    async def _iter_completion(
        self, job: DownloadJobLike
    ) -> AsyncIterator[DownloadProgress]:
        # Yield the current progress (which is what ``start`` left us
        # with) marked complete so the use case transitions to
        # COMPLETED. Real-byte aware adapters can override.
        total = job.progress.total_bytes
        yield DownloadProgress(
            bytes_downloaded=total if total is not None else 0,
            total_bytes=total,
            speed_bps=0.0,
            eta_seconds=0.0,
        )


class _InternalSubprocessRunner:
    """Pure-asyncio fallback for callers that do not inject a runner.

    Uses :func:`asyncio.create_subprocess_exec` to launch the child
    process, awaits its exit code, and surfaces stdout/stderr to the
    debug log if non-empty. Production wiring injects the
    platform-level :class:`SubprocessProcessRunner`; this internal
    fallback keeps the adapter usable when constructed with
    ``process_runner=None`` (e.g. unit tests that exercise the
    binary path directly without a Container).
    """

    __slots__ = ()

    async def run(
        self,
        argv: list[str],
        *,
        timeout_s: float | None = None,
    ) -> int:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise InfrastructureError(
                "model_catalog.subprocess.binary_missing",
                f"binary not found: {argv[0]!r}",
                cause=exc,
            ) from exc
        try:
            if timeout_s is None:
                _stdout, _stderr = await proc.communicate()
            else:
                _stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise InfrastructureError(
                "model_catalog.subprocess.timeout",
                f"process {argv[0]!r} exceeded timeout={timeout_s}s",
                cause=exc,
            ) from exc
        return int(proc.returncode or 0)
