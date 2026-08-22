# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for standalone fullstack app projects (plan §6.3).

Phase 2 implements the read-only surface — list + get. The remaining
use cases (``RunAppProjectUseCase`` / ``StopAppProjectUseCase`` /
``GetAppProjectLogsUseCase`` / ``StreamAppProjectLogsUseCase`` /
``PackageAppProjectUseCase`` / ``StreamPackageProgressUseCase``, plan
§6.3) are Phase 3 / Phase 5 and will be *appended* to this module.

Layering (import-linter ``layered-app_builder``): use cases import from
the domain only and receive their collaborators (ports) via the
constructor. The repository dependency is duck-typed (structural
``AppProjectRepositoryPort``): any object exposing
``async list_projects()`` / ``async get_project(app_id)`` works, so this
module needs no import of the concrete infrastructure repository nor —
for the passthrough Phase-2 use cases — of the port protocol itself.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from qai.app_builder.domain.app_project import (
    AppProjectDefinition,
    AppProjectNotRunningError,
    AppProjectRunInfo,
)

__all__ = [
    "AppProjectPackagerLike",
    "AppProjectProcessLike",
    "AppProjectRepositoryLike",
    "DeleteAppProjectUseCase",
    "GetAppProjectLogsUseCase",
    "GetAppProjectStatusUseCase",
    "GetAppProjectUseCase",
    "ListAppProjectsUseCase",
    "PackageAppProjectUseCase",
    "RunAppProjectUseCase",
    "StopAppProjectUseCase",
]


class AppProjectRepositoryLike(Protocol):
    """Structural view of the app-project repository the use cases need.

    Declared locally (application layer) so the use cases have a typed
    dependency without importing the concrete infrastructure repository —
    that would violate the ``application → infrastructure`` forbidden
    direction. The canonical ``AppProjectRepositoryPort`` added to
    ``application/ports.py`` is the same shape; both are satisfied
    structurally by :class:`FileSystemAppProjectRepository`.
    """

    async def list_projects(self) -> list[AppProjectDefinition]:
        ...

    async def get_project(self, app_id: str) -> AppProjectDefinition:
        ...

    async def delete_project(self, app_id: str) -> None:
        ...


class ListAppProjectsUseCase:
    """List all valid app projects (newest-first)."""

    def __init__(self, *, repository: AppProjectRepositoryLike) -> None:
        self._repository = repository

    async def execute(self) -> list[AppProjectDefinition]:
        return await self._repository.list_projects()


class GetAppProjectUseCase:
    """Fetch a single app project by id.

    Propagates the repository's domain errors verbatim:
    :class:`AppProjectNotFoundError` (unknown / invalid id / escaping
    path / missing dir) and :class:`AppProjectInvalidError` (present dir
    with a missing / malformed ``app.yaml``). The route layer maps those
    to HTTP 404 / 400 per plan §5.7.
    """

    def __init__(self, *, repository: AppProjectRepositoryLike) -> None:
        self._repository = repository

    async def execute(self, app_id: str) -> AppProjectDefinition:
        return await self._repository.get_project(app_id)


# ---------------------------------------------------------------------------
# Phase 3 — managed run lifecycle (plan §6.3)
# ---------------------------------------------------------------------------
class AppProjectProcessLike(Protocol):
    """Structural view of the app-project process manager the use cases need.

    Declared locally (application layer) — the same shape as the canonical
    ``AppProjectProcessPort`` in ``application/ports.py`` — so the use cases
    take a typed collaborator without importing the concrete
    infrastructure ``AppProjectProcessManager`` (that would violate the
    ``application → infrastructure`` forbidden direction). Both protocols
    are satisfied structurally by
    :class:`~qai.app_builder.infrastructure.app_project_process_manager.AppProjectProcessManager`.
    """

    async def run(
        self, definition: AppProjectDefinition, *, port: int | None
    ) -> AppProjectRunInfo:
        ...

    async def stop(self, app_id: str) -> AppProjectRunInfo:
        ...

    async def status(self, app_id: str) -> AppProjectRunInfo:
        ...

    async def logs(self, app_id: str) -> str:
        ...


class RunAppProjectUseCase:
    """Start (or return the already-running) managed process for an app.

    Resolves the :class:`AppProjectDefinition` through the repository
    (propagating ``AppProjectNotFoundError`` / ``AppProjectInvalidError``
    verbatim so the route layer maps them to 404 / 400), then delegates to
    the process manager, which owns port allocation, spawn + ``/health``
    readiness, and the single-instance-per-app rule. The process manager's
    domain errors (``AppProjectPortInUseError`` / ``AppProjectNoBindablePortError``
    / ``AppProjectStartFailedError``) propagate unchanged.
    """

    def __init__(
        self,
        *,
        repository: AppProjectRepositoryLike,
        process: AppProjectProcessLike,
    ) -> None:
        self._repository = repository
        self._process = process

    async def execute(
        self, app_id: str, *, port: int | None = None
    ) -> AppProjectRunInfo:
        definition = await self._repository.get_project(app_id)
        return await self._process.run(definition, port=port)


class StopAppProjectUseCase:
    """Stop the managed process for an app project.

    Propagates ``AppProjectNotRunningError`` when nothing is running for
    ``app_id`` (route layer → 409).
    """

    def __init__(self, *, process: AppProjectProcessLike) -> None:
        self._process = process

    async def execute(self, app_id: str) -> AppProjectRunInfo:
        return await self._process.stop(app_id)


class DeleteAppProjectUseCase:
    """Delete an app project — stop its managed process first, then remove it.

    Orchestration (plan §5: delete is destructive + must not orphan a
    process):

    1. Best-effort STOP the managed process so we never delete files out
       from under a running uvicorn (which would leave an orphan holding the
       port). A ``AppProjectNotRunningError`` is swallowed — nothing to stop.
    2. Delete the on-disk project dir via the repository (path-traversal
       safe, strictly under ``data/app_builder/``). Propagates
       ``AppProjectNotFoundError`` (unknown id → 404) and
       ``AppProjectDeleteFailedError`` (IO error → 5xx).

    Packaged zips in the workspace are NOT touched — only the dev project.
    """

    def __init__(
        self,
        *,
        repository: AppProjectRepositoryLike,
        process: AppProjectProcessLike,
    ) -> None:
        self._repository = repository
        self._process = process

    async def execute(self, app_id: str) -> None:
        # 1) Stop first (tolerate "not running").
        try:
            await self._process.stop(app_id)
        except AppProjectNotRunningError:
            pass
        # 2) Remove the project directory.
        await self._repository.delete_project(app_id)


class GetAppProjectStatusUseCase:
    """Return the live managed-run status for an app project.

    Returns a ``stopped`` :class:`AppProjectRunInfo` when no managed
    process exists (never raises for the not-running case — status is a
    read-only snapshot).
    """

    def __init__(self, *, process: AppProjectProcessLike) -> None:
        self._process = process

    async def execute(self, app_id: str) -> AppProjectRunInfo:
        return await self._process.status(app_id)


class GetAppProjectLogsUseCase:
    """Return the retained stdout/stderr tail of an app's managed process.

    Propagates ``AppProjectNotRunningError`` when nothing is/was running
    for ``app_id`` (route layer → 409).
    """

    def __init__(self, *, process: AppProjectProcessLike) -> None:
        self._process = process

    async def execute(self, app_id: str) -> str:
        return await self._process.logs(app_id)


# ---------------------------------------------------------------------------
# Phase 5 — packaging (plan §5.6 / §6.3 / §10.4)
# ---------------------------------------------------------------------------
class PackageProgressLike(Protocol):
    """Structural view of one packaging progress snapshot.

    The concrete
    :class:`~qai.app_builder.infrastructure.app_project_packager.PackageProgress`
    (infrastructure) satisfies this by shape; declaring it locally keeps the
    application layer free of any ``application → infrastructure`` import
    (``layered-app_builder`` contract). The use case never *reads* these
    fields — it relays snapshots verbatim to the route/SSE layer — so a
    read-only structural view is enough.
    """

    phase: str
    percent: float
    message: str
    zip_path: str | None
    size_bytes: int | None
    is_complete: bool


class AppProjectPackagerLike(Protocol):
    """Structural view of the app-project packager the use case needs.

    Declared locally (application layer) — the same shape as the canonical
    ``AppProjectPackagerPort`` in ``application/ports.py`` — so the use case
    takes a typed collaborator without importing the concrete infrastructure
    :class:`FileSystemAppProjectPackager` (that would violate the
    ``application → infrastructure`` forbidden direction). The packager's
    :meth:`package` is an **async generator** yielding progress snapshots.
    """

    def package(
        self, definition: AppProjectDefinition
    ) -> AsyncIterator[PackageProgressLike]:
        ...


class PackageAppProjectUseCase:
    """Start / stream / cancel an app-project packaging job (plan §5.6).

    Mirrors the ``DownloadModelWeightsUseCase`` start/stream/cancel shape: an
    in-memory job registry keyed by an opaque ``job_id`` holds a lazily-driven
    packager async iterator so a slow (large-weight) copy can be polled /
    streamed by ``job_id`` without blocking the ``POST`` that started it.

    * :meth:`start` resolves the :class:`AppProjectDefinition` through the
      repository (propagating ``AppProjectNotFoundError`` /
      ``AppProjectInvalidError`` verbatim so the route maps them to 404/400),
      creates the ``packager.package(definition)`` iterator (NOT yet drained),
      registers it under a fresh ``job_id``, and returns the id.
    * :meth:`stream` drains the job's iterator, relaying each progress
      snapshot; a ``AppProjectPackageFailedError`` raised mid-stream surfaces
      as an SSE ``error`` frame. Raises ``KeyError``-shaped lookup via a
      ``ValueError`` on an unknown job so the route can 404 before committing
      the stream. The job is dropped from the registry once the stream ends.
    * :meth:`cancel` drops the job from the registry (idempotent).
    """

    def __init__(
        self,
        *,
        repository: AppProjectRepositoryLike,
        packager: AppProjectPackagerLike,
    ) -> None:
        self._repository = repository
        self._packager = packager
        self._jobs: dict[str, AsyncIterator[Any]] = {}

    async def start(self, app_id: str) -> str:
        """Resolve the app + create a packaging job; return its ``job_id``.

        Propagates ``AppProjectNotFoundError`` / ``AppProjectInvalidError``
        from the repository verbatim (route → 404 / 400).
        """
        definition = await self._repository.get_project(app_id)
        iterator = self._packager.package(definition)
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = iterator
        return job_id

    async def stream(self, job_id: str) -> AsyncIterator[Any]:
        """Return an async iterator of progress snapshots for ``job_id``.

        Raises :class:`ValueError` (route → 404) when the job is unknown,
        BEFORE the iterator begins, so no stream is committed yet.
        """
        iterator = self._jobs.get(job_id)
        if iterator is None:
            raise ValueError(f"package job {job_id!r} not found")
        return self._drain(job_id, iterator)

    async def _drain(
        self, job_id: str, iterator: AsyncIterator[Any]
    ) -> AsyncIterator[Any]:
        try:
            async for snapshot in iterator:
                yield snapshot
        finally:
            # Drop on success, failure, OR early consumer close so the
            # registry never grows unbounded across repeated packagings.
            self._jobs.pop(job_id, None)

    def cancel(self, job_id: str) -> None:
        """Remove the job from the registry (idempotent)."""
        self._jobs.pop(job_id, None)
