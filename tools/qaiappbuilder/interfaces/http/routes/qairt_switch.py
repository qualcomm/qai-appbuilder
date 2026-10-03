# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""QAIRT SDK hot-switch HTTP API.

Thin DTO/routing adapter over :class:`qai.platform.qairt_switch.service.
QairtSwitchService`. Every route here requires normal authentication —
unlike the pre-existing ``GET /api/system/qairt-versions`` (which stays
public/read-only in ``interfaces/http/routes/system.py``), these routes
mutate process state (installs an extension, rewrites ``qairt_env.json``,
recycles the sticky worker) and must never be added to the auth
middleware's public allowlist.

Three endpoints:

* ``GET  /api/system/qairt-switches/preflight?version=<v>`` — side-effect
  free; reports the target/active SDK, the minimum/installed extension
  version, the bounded candidate list, and whether an install is needed.
* ``POST /api/system/qairt-switches`` — reserves the single active-switch
  slot and dispatches the transaction through the App Builder background
  task registry (so app shutdown cancellation drives the service's own
  shielded rollback instead of abandoning the coroutine).
* ``GET  /api/system/qairt-switches/{job_id}`` — polls a job's terminal
  or in-flight :class:`SwitchDiagnostics`, mapped straight through with
  no additional free-text fields (the domain type is already the
  allowlisted public shape).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from qai.platform.errors import ConflictError, NotFoundError, ValidationError
from qai.platform.qairt_switch.models import Phase
from qai.platform.qairt_switch.service import (
    AlreadyRunningError,
    InvalidTargetError,
    MissingAcknowledgementError,
    SwitchJob,
)
from qai.platform.qairt_switch.versioning import QairtVersion
from qai.platform.qairt_versions import discover_qairt_versions, resolve_qairt_installation

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

__all__ = ["build_router"]


# ---- Response DTOs --------------------------------------------------------


class QairtSwitchPreflightResponse(BaseModel):
    """``GET /api/system/qairt-switches/preflight`` payload.

    Side-effect free. Never contains local paths, environment, commands,
    prompts, model names, or PIDs.
    """

    target_version: str
    active_version: str | None
    required_appbuilder_version: str
    installed_appbuilder_version: str | None
    candidate_versions: list[str]
    install_required: bool
    sticky_worker_running: bool


class QairtSwitchCreateRequest(BaseModel):
    """Body for ``POST /api/system/qairt-switches``."""

    target_version: str
    no_qnn_inference_acknowledged: bool = False


class QairtSwitchCreateResponse(BaseModel):
    """``POST /api/system/qairt-switches`` payload (HTTP 202)."""

    job_id: str
    status: Literal["queued"]


class CandidateAttemptDto(BaseModel):
    candidate: str
    outcome: str
    detail: str | None = None


class DownloadProgressDto(BaseModel):
    bytes_downloaded: int
    total_bytes: int | None = None


class QairtSwitchJobResponse(BaseModel):
    """``GET /api/system/qairt-switches/{job_id}`` payload.

    Mapped straight from :class:`SwitchDiagnostics` -- already the
    allowlisted public shape (no raw paths/environment/commands/tokens/
    unsanitized stderr).
    """

    job_id: str
    status: str
    phase: str
    target_version: str
    active_version: str | None
    candidate_attempts: list[CandidateAttemptDto]
    progress: DownloadProgressDto | None
    error_code: str | None
    error_message: str | None
    correlation_id: str
    issue_url: str


_TERMINAL_FAILURE_PHASES = frozenset({Phase.ROLLED_BACK, Phase.ROLLBACK_FAILED})


def _job_status(job: SwitchJob) -> str:
    if job.diagnostics.succeeded:
        return "succeeded"
    if job.diagnostics.phase in _TERMINAL_FAILURE_PHASES:
        return "failed"
    return "running"


def _job_to_dto(job: SwitchJob) -> QairtSwitchJobResponse:
    d = job.diagnostics
    return QairtSwitchJobResponse(
        job_id=job.job_id,
        status=_job_status(job),
        phase=d.phase.value,
        target_version=str(d.target_version),
        active_version=str(d.active_version) if d.active_version is not None else None,
        candidate_attempts=[
            CandidateAttemptDto(
                candidate=str(a.candidate), outcome=a.outcome.value, detail=a.detail
            )
            for a in d.attempts
        ],
        progress=(
            DownloadProgressDto(
                bytes_downloaded=d.progress.bytes_downloaded,
                total_bytes=d.progress.total_bytes,
            )
            if d.progress is not None
            else None
        ),
        error_code=d.error_code,
        error_message=d.error_message,
        correlation_id=d.correlation_id,
        issue_url=d.issue_url,
    )


def _fallback_install_root() -> Path:
    return Path("C:/Qualcomm/AIStack/QAIRT")


# ---- Router factory -------------------------------------------------------


def build_router(*, container: Container) -> APIRouter:
    """Build a router bound to the given DI container.

    Every route 404s with ``qairt_switch.unavailable`` when
    ``container.qairt_switch_service`` is ``None`` (no existing
    ``qairt_env.json`` on this install -- see ``apps.api.
    di._build_qairt_switch_service`` for the exact gate).
    """
    router = APIRouter(prefix="/api/system", tags=["qairt-switch"])

    def _service():
        service = container.qairt_switch_service
        if service is None:
            raise NotFoundError(
                "qairt_switch.unavailable",
                "qairt_switch",
                "service",
                message=(
                    "QAIRT SDK switching is unavailable: no existing "
                    "qairt_env.json was found on this install."
                ),
            )
        return service

    @router.get(
        "/qairt-switches/preflight",
        response_model=QairtSwitchPreflightResponse,
    )
    async def preflight(
        version: str = Query(..., description="Target QAIRT SDK version"),
    ) -> QairtSwitchPreflightResponse:
        service = _service()

        try:
            target = QairtVersion.parse(version)
        except ValueError as exc:
            raise ValidationError(
                "qairt_switch.invalid_target",
                f"Not a valid QAIRT version: {version!r}",
                field_errors={"version": [str(exc)]},
            ) from exc

        install_root, configured_version_str = resolve_qairt_installation(
            container.qairt_env_file, fallback_root=_fallback_install_root()
        )
        installed_versions = discover_qairt_versions(install_root)
        if str(target) not in installed_versions:
            raise ValidationError(
                "qairt_switch.target_not_installed",
                f"QAIRT SDK {target} is not an installed version.",
                field_errors={"version": ["not an installed QAIRT SDK version"]},
            )

        active_version = (
            configured_version_str if configured_version_str in installed_versions else None
        )
        required = target.minimum_appbuilder
        installed_appbuilder = await service.inspect_installed_extension_version()
        install_required = installed_appbuilder is None or installed_appbuilder < required

        return QairtSwitchPreflightResponse(
            target_version=str(target),
            active_version=active_version,
            required_appbuilder_version=str(required),
            installed_appbuilder_version=(
                str(installed_appbuilder) if installed_appbuilder is not None else None
            ),
            candidate_versions=[str(c) for c in target.appbuilder_candidates()],
            install_required=install_required,
            sticky_worker_running=service.sticky_worker_running,
        )

    @router.post(
        "/qairt-switches",
        response_model=QairtSwitchCreateResponse,
        status_code=202,
    )
    async def create_switch(
        body: QairtSwitchCreateRequest,
    ) -> QairtSwitchCreateResponse:
        service = _service()

        _install_root, configured_version_str = resolve_qairt_installation(
            container.qairt_env_file, fallback_root=_fallback_install_root()
        )
        try:
            job = service.create_job(
                target_version=body.target_version,
                no_qnn_inference_acknowledged=body.no_qnn_inference_acknowledged,
                active_version=configured_version_str,
            )
        except AlreadyRunningError as exc:
            raise ConflictError("qairt_switch.already_running", str(exc)) from exc
        except MissingAcknowledgementError as exc:
            raise ValidationError(
                "qairt_switch.acknowledgement_required",
                str(exc),
                field_errors={"no_qnn_inference_acknowledged": [str(exc)]},
            ) from exc
        except InvalidTargetError as exc:
            raise ValidationError(
                "qairt_switch.invalid_target",
                str(exc),
                field_errors={"target_version": [str(exc)]},
            ) from exc

        tasks = getattr(container.app_builder, "background_tasks", None)
        if tasks is not None:
            tasks.spawn(service.run_switch(job.job_id), name=f"qairt-switch-{job.job_id}")
        else:  # pragma: no cover -- background_tasks is always wired in production

            asyncio.create_task(  # noqa: RUF006 -- no registry available; best effort
                service.run_switch(job.job_id)
            )

        return QairtSwitchCreateResponse(job_id=job.job_id, status="queued")

    @router.get(
        "/qairt-switches/{job_id}",
        response_model=QairtSwitchJobResponse,
    )
    async def get_switch(job_id: str) -> QairtSwitchJobResponse:
        service = _service()
        job = service.get_job(job_id)
        if job is None:
            raise NotFoundError("qairt_switch.job_not_found", "qairt_switch_job", job_id)
        return _job_to_dto(job)

    return router
