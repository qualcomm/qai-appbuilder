# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Download-engine port for the platform shared kernel.

Lifted from :mod:`qai.model_catalog.application.ports` so the aria2c
download engine can live under ``qai.platform.*`` and be shared by
multiple bounded contexts without violating the ``context-isolation``
import-linter contract.

Platform-neutrality
-------------------
Platform code MUST NOT import any ``qai.<boundedcontext>`` module. The
original port typed its ``job`` parameter as the concrete
``qai.model_catalog.domain.entities.DownloadJob`` entity. To keep the
port platform-neutral we type ``job`` as :class:`DownloadJobLike`, a
``@runtime_checkable`` structural Protocol exposing exactly what the
engine reads from a download job:

* ``job_id`` -- an object with a ``.value: str`` attribute.
* ``progress`` -- a :class:`DownloadProgress` snapshot.

The concrete ``DownloadJob`` entity structurally satisfies this Protocol
(it has a ``job_id`` with ``.value`` and a ``progress`` field), so
existing use cases keep passing real ``DownloadJob`` instances unchanged.
:mod:`qai.model_catalog.application.ports` re-exports
:class:`DownloadEnginePort` from here so callers importing it from the
old path keep working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from qai.platform.download.value_objects import (
    DownloadProgress,
    SourceUrl,
    StorageKey,
)


__all__ = [
    "DownloadJobLike",
    "DownloadEnginePort",
]


@runtime_checkable
class _JobIdLike(Protocol):
    """Structural shape of a download-job identifier: exposes ``.value``."""

    @property
    def value(self) -> str:
        ...


@runtime_checkable
class DownloadJobLike(Protocol):
    """Platform-neutral structural view of a download job.

    The engine only ever reads ``job.job_id.value`` (a ``str``) and
    ``job.progress`` (a :class:`DownloadProgress`) from a download job;
    it never mutates it. Rather than import the concrete
    ``qai.model_catalog.domain.entities.DownloadJob`` (which would break
    context isolation), the port types its ``job`` parameter against this
    ``@runtime_checkable`` Protocol. Any object exposing a ``job_id`` with
    a ``.value: str`` and a ``progress: DownloadProgress`` satisfies it --
    including the concrete ``DownloadJob`` entity.
    """

    @property
    def job_id(self) -> _JobIdLike:
        ...

    @property
    def progress(self) -> DownloadProgress:
        ...


@runtime_checkable
class DownloadEnginePort(Protocol):
    """Abstract download engine.

    Implementations may be a native multi-connection CLI engine
    (default in production), httpx, requests, curl-impersonate, etc.
    The domain never assumes any particular transport.

    Concurrency contract
    --------------------
    * ``start`` is allowed to return as soon as the transfer has been
      submitted to the engine; the actual byte movement happens in the
      background.  The ``stream_progress`` / ``progress`` methods feed
      progress back to use cases.
    * ``cancel`` MUST be idempotent.  Calling it on a job that already
      completed / failed is a no-op (no exception).
    """

    async def start(
        self,
        job: DownloadJobLike,
        *,
        source: SourceUrl,
        target: StorageKey,
    ) -> None:
        """Submit ``job`` for transfer from ``source`` into ``target``.

        Implementations MUST NOT mutate the download job directly;
        they signal progress through :meth:`stream_progress`.
        """
        ...

    async def cancel(self, job: DownloadJobLike) -> None:
        """Best-effort cancel.  Idempotent, never raises on terminal jobs."""
        ...

    async def progress(self, job: DownloadJobLike) -> DownloadProgress:
        """One-shot progress snapshot."""
        ...

    def stream_progress(
        self, job: DownloadJobLike
    ) -> AsyncIterator[DownloadProgress]:
        """Stream of progress updates until the engine deems the job done.

        The iterator must terminate (StopAsyncIteration) when the engine
        no longer has anything to report (success, cancellation, or
        error).  Use cases consume it via ``async for``.
        """
        ...
