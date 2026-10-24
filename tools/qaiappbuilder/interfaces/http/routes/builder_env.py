# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model-conversion environment routes (user-triggered, background install).

* ``GET    /api/system/builder-env``          -- is the environment usable + the job, if any.
* ``POST   /api/system/builder-env/install``  -- start the install (202). One job at a time.
* ``DELETE /api/system/builder-env/install``  -- cancel the running install.

Design notes
------------
* Detection reads the disk on every GET (cheap: a JSON file + two ``stat`` calls), so a
  finished install -- or one done by hand -- shows up on the next poll with no restart.
* The routes are available whether or not ``qairt_env.json`` exists: that file is
  precisely what the install creates, so it must not gate the feature (unlike
  ``qairt_switch``).
* State-changing routes are protected by the global CSRF / auth middleware like every
  other mutating route; they must never be added to the auth public-path list.
* The payload never contains the child's environment or the proxy URL. Paths are
  returned: the UI shows where the SDK / venv live.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from qai.platform.builder_env import (
    AlreadyRunningError,
    InstallJob,
    ScriptMissingError,
    detect_builder_env,
)
from qai.platform.errors import ConflictError, NotFoundError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

__all__ = ["build_router"]


# ---- DTOs -----------------------------------------------------------------


class ProblemDto(BaseModel):
    code: str
    path: str = ""
    key: str = ""


class JobDto(BaseModel):
    job_id: str
    state: Literal["running", "succeeded", "failed", "cancelled"]
    overall: float
    step: str
    step_state: str
    message: str
    unit: str
    done: int
    total: int | None
    speed: int
    eta: int | None
    log_tail: list[str]
    error_code: str
    error_message: str
    failed_step: str
    started_at: float
    finished_at: float | None


class BuilderEnvResponse(BaseModel):
    """``GET /api/system/builder-env``."""

    ready: bool
    missing: list[str]
    problems: list[ProblemDto]
    sdk_root: str
    venv: str
    config_file: str
    job: JobDto | None
    # False when the installer script is not in this build (the UI hides the button).
    installable: bool


class InstallRequest(BaseModel):
    mirror: Literal["auto", "official", "cn"] = "auto"
    proxy: str = Field(default="", max_length=300)


class InstallStarted(BaseModel):
    job_id: str
    status: Literal["queued"]


class CancelResponse(BaseModel):
    cancelled: bool


def _job_dto(job: InstallJob) -> JobDto:
    return JobDto(
        job_id=job.job_id,
        state=job.state.value,
        overall=round(job.overall, 1),
        step=job.step,
        step_state=job.step_state,
        message=job.message,
        unit=job.unit,
        done=job.done,
        total=job.total,
        speed=job.speed,
        eta=job.eta,
        log_tail=list(job.log_tail),
        error_code=job.error_code,
        error_message=job.error_message,
        failed_step=job.failed_step,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


# ---- Router ---------------------------------------------------------------


def build_router(*, container: Container) -> APIRouter:
    router = APIRouter(prefix="/api/system", tags=["builder-env"])

    def _service():
        service = getattr(container, "builder_env_service", None)
        if service is None:
            raise NotFoundError(
                "builder_env.unavailable",
                "builder_env",
                "service",
                message="the model-conversion environment installer is not available in this build",
            )
        return service

    def _config_file():
        return container.data_paths.root / "config" / "qairt_env.json"

    @router.get("/builder-env", response_model=BuilderEnvResponse)
    async def get_builder_env() -> BuilderEnvResponse:
        service = _service()
        status = detect_builder_env(_config_file())
        job = service.job
        return BuilderEnvResponse(
            ready=status.ready,
            missing=list(status.missing),
            problems=[ProblemDto(code=p.code, path=p.path, key=p.key) for p in status.problems],
            sdk_root=status.sdk_root,
            venv=status.venv,
            config_file=status.config_file,
            job=_job_dto(job) if job is not None else None,
            installable=service.installable,
        )

    @router.post("/builder-env/install", response_model=InstallStarted, status_code=202)
    async def start_install(body: InstallRequest | None = None) -> InstallStarted:
        service = _service()
        body = body or InstallRequest()
        try:
            job = service.start(mirror=body.mirror, proxy=body.proxy)
        except AlreadyRunningError as exc:
            raise ConflictError("builder_env.already_running", str(exc)) from exc
        except ScriptMissingError as exc:
            raise NotFoundError(
                "builder_env.unavailable", "builder_env", "script", message=str(exc)
            ) from exc
        except ValueError as exc:
            raise ValidationError(
                "builder_env.invalid_request", str(exc), field_errors={"proxy": [str(exc)]}
            ) from exc
        return InstallStarted(job_id=job.job_id, status="queued")

    @router.delete("/builder-env/install", response_model=CancelResponse)
    async def cancel_install() -> CancelResponse:
        service = _service()
        return CancelResponse(cancelled=await service.cancel())

    return router
