# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — standalone fullstack *app project* routes (plan §5.1/§5.2).

Backs the "My Apps" surface: list the app projects generated under
``data/app_builder/<app_id>/`` and read one app's detail. This is the
Phase-2 read-only slice; managed run / logs / package routes (plan
§5.3-§5.6) are added in later phases.

Routes
------
* ``GET /api/app-builder/apps``            — list app projects
* ``GET /api/app-builder/apps/{app_id}``   — one app project detail

The route stays thin (Contract 4): it reaches the use cases through
``container.app_builder`` and never imports app_builder infrastructure
directly. Domain errors from the use cases are re-raised as the semantic
:mod:`qai.platform.errors` classes (``NotFoundError`` → 404,
``ValidationError`` → 400, ``ConflictError`` → 409,
``ExternalServiceError`` → 503) so the global error handler serializes
the conforming ``{type, code, message, details}`` body the frontend
``parseApiError`` expects — routes never hand-build ``HTTPException``
error bodies (which would emit a non-conforming ``{"detail": ...}``).

Phase-2 status shape: listing is read-only, so every ``AppEntry``
reports ``status="stopped"`` with ``preview_url`` / ``port`` / ``pid``
all ``None`` (State-Truth-First — no managed process exists yet).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from interfaces.http.sse import format_sse_event
from qai.app_builder.domain.app_project import (
    AppProjectAlreadyRunningError,
    AppProjectDefinition,
    AppProjectDeleteFailedError,
    AppProjectInvalidError,
    AppProjectNoBindablePortError,
    AppProjectNotFoundError,
    AppProjectNotRunningError,
    AppProjectPortInUseError,
    AppProjectRunInfo,
    AppProjectStartFailedError,
)
from qai.platform.errors import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    QaiError,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Response models (inline — plan §5.1 shape)
# ---------------------------------------------------------------------------
class AppEntry(BaseModel):
    """One row of ``GET /apps`` (plan §5.1)."""

    id: str
    name: str
    path: str
    models: list[str]
    status: str
    preview_url: str | None
    port: int | None
    pid: int | None
    modified_at: float


class AppListResponse(BaseModel):
    """``GET /apps`` response envelope."""

    apps: list[AppEntry]


class AppModelDetail(BaseModel):
    """One bundled model, expanded for the detail view."""

    id: str
    title: str
    builtin: bool


class AppEntryPoint(BaseModel):
    """``entry:`` block of ``app.yaml`` (plan §1.4)."""

    app_module: str
    health_path: str
    frontend_path: str


class AppRuntime(BaseModel):
    """``runtime:`` block of ``app.yaml`` (plan §1.4)."""

    host: str
    preferred_port: int | None


class AppDetailResponse(BaseModel):
    """``GET /apps/{app_id}`` response — superset of :class:`AppEntry`."""

    id: str
    name: str
    path: str
    models: list[str]
    status: str
    preview_url: str | None
    port: int | None
    pid: int | None
    modified_at: float
    # Detail-only fields.
    description: str
    models_detail: list[AppModelDetail]
    entry: AppEntryPoint
    runtime: AppRuntime


# ---------------------------------------------------------------------------
# Managed-run request / response models (plan §5.3 / §5.4 / §5.5)
# ---------------------------------------------------------------------------
class RunAppRequest(BaseModel):
    """Body of ``POST /apps/{app_id}/run`` (plan §5.3).

    ``port`` ``None`` → the host auto-allocates a bindable port;
    ``open_browser`` is advisory (the frontend decides whether to open the
    preview URL after readiness — the host never calls ``webbrowser``).
    """

    port: int | None = None
    open_browser: bool = True


class RunAppResponse(BaseModel):
    """Managed-run snapshot returned by run / stop (plan §5.3).

    Maps 1:1 from :class:`~qai.app_builder.domain.app_project.AppProjectRunInfo`.
    """

    app_id: str
    status: str
    port: int | None
    url: str | None
    pid: int | None
    process_id: str | None
    manual_command: str | None


class AppLogsResponse(BaseModel):
    """``GET /apps/{app_id}/logs`` response (plan §5.5)."""

    app_id: str
    status: str
    output: str
    truncated: bool = False


class PackageJobResponse(BaseModel):
    """``POST /apps/{app_id}/package`` response — the created job id (plan §5.6)."""

    job_id: str


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------
def _to_entry(definition: AppProjectDefinition) -> AppEntry:
    """Map a domain :class:`AppProjectDefinition` to the list DTO.

    Phase 2 is read-only: ``status`` is always ``"stopped"`` and the
    managed-run fields are ``None``.
    """
    return AppEntry(
        id=definition.id.value,
        name=definition.name,
        path=definition.path,
        models=[m.id for m in definition.models],
        status="stopped",
        preview_url=None,
        port=None,
        pid=None,
        modified_at=definition.modified_at,
    )


def _to_detail(definition: AppProjectDefinition) -> AppDetailResponse:
    return AppDetailResponse(
        id=definition.id.value,
        name=definition.name,
        path=definition.path,
        models=[m.id for m in definition.models],
        status="stopped",
        preview_url=None,
        port=None,
        pid=None,
        modified_at=definition.modified_at,
        description=definition.description,
        models_detail=[
            AppModelDetail(id=m.id, title=m.title, builtin=m.builtin)
            for m in definition.models
        ],
        entry=AppEntryPoint(
            app_module=definition.app_module,
            health_path=definition.health_path,
            frontend_path=definition.frontend_path,
        ),
        runtime=AppRuntime(
            host=definition.host,
            preferred_port=definition.preferred_port,
        ),
    )


def _to_run_response(info: AppProjectRunInfo) -> RunAppResponse:
    """Map a domain :class:`AppProjectRunInfo` to the run/stop DTO."""
    return RunAppResponse(
        app_id=info.app_id,
        status=info.status,
        port=info.port,
        url=info.url,
        pid=info.pid,
        process_id=info.process_id,
        manual_command=info.manual_command,
    )


def _raise_conforming(exc: Any, *, fallback: str) -> NoReturn:
    """Re-raise an ``AppProject*`` domain error as the platform ``QaiError``
    whose class the global handler maps to the right HTTP status + the
    conforming ``{type, code, message, details}`` body.

    Routes MUST NOT hand-build ``HTTPException(detail=...)`` for these — that
    produces ``{"detail": {...}}`` (no top-level ``type``), which the frontend
    ``parseApiError`` rejects as a non-conforming envelope. Raising the
    semantic ``QaiError`` lets ``interfaces/http/error_handlers`` serialize it
    uniformly (status is derived from the class; see its ``_STATUS_MAP``).

    Status mapping (via the raised class):
      * ``AppProjectNotFoundError``     -> NotFoundError        (404)
      * ``AppProjectInvalidError``      -> ValidationError      (400)
      * ``AppProjectPortInUseError`` /
        ``AppProjectNoBindablePortError`` /
        ``AppProjectAlreadyRunningError`` /
        ``AppProjectNotRunningError``   -> ConflictError        (409)
      * ``AppProjectStartFailedError`` /
        ``AppProjectPackageFailedError``-> ExternalServiceError (503)
    """
    code = exc.code
    message = exc.message or fallback
    details = exc.details or {}
    if isinstance(exc, AppProjectNotFoundError):
        raise NotFoundError(
            code, "app_project", details.get("app_id", ""), message=message
        ) from exc
    if isinstance(exc, AppProjectInvalidError):
        raise ValidationError(code, message) from exc
    if isinstance(
        exc,
        (
            AppProjectPortInUseError,
            AppProjectNoBindablePortError,
            AppProjectAlreadyRunningError,
            AppProjectNotRunningError,
        ),
    ):
        raise ConflictError(code, message, details=details) from exc
    # Start / package failures are infrastructure-level (subprocess spawn /
    # readiness / packaging IO) -> 503 via ExternalServiceError.
    raise ExternalServiceError(
        code, message, service="app_builder"
    ) from exc


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(router: APIRouter, *, container: Container) -> None:  # noqa: PLR0915 - route-registration closure factory: nested handlers must capture the DI container
    """Mount the app-project routes onto ``router``."""

    def _services() -> Any:
        return container.app_builder

    def _require(uc_attr: str) -> Any:
        """Return the named use case, or a clean 503 when it is not wired.

        Guards against a raw ``AttributeError`` → ugly 500 on a lean
        container that left the use case ``None``.
        """
        uc = getattr(_services(), uc_attr, None)
        if uc is None:
            raise ExternalServiceError(
                "app_builder.use_case_unavailable",
                f"{uc_attr} not available",
                service="app_builder",
            )
        return uc

    @router.get("/apps", response_model=AppListResponse)
    async def list_apps() -> AppListResponse:
        """List generated app projects (plan §5.1).

        Never 500s on a fresh install: when the use case is not wired
        (lean container / no data dir yet) we surface an empty
        ``{"apps": []}`` rather than a 503 (State-Truth-First: an empty
        listing reflects "no apps generated yet").
        """
        uc = getattr(_services(), "list_app_projects_use_case", None)
        if uc is None:
            return AppListResponse(apps=[])
        projects = await uc.execute()
        return AppListResponse(apps=[_to_entry(p) for p in projects])

    @router.get("/apps/{app_id}", response_model=AppDetailResponse)
    async def get_app(app_id: str) -> AppDetailResponse:
        """Return one app project's detail (plan §5.2).

        ``AppProjectNotFoundError`` → 404, ``AppProjectInvalidError`` →
        400, both with the plan §5.7 ``{code, message, details}``
        envelope.
        """
        uc = _require("get_app_project_use_case")
        try:
            definition = await uc.execute(app_id)
        except (AppProjectNotFoundError, AppProjectInvalidError) as exc:
            _raise_conforming(
                exc, fallback=f"app project {app_id!r} not found or invalid"
            )
        return _to_detail(definition)

    @router.delete("/apps/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_app(app_id: str) -> Response:
        """Delete a generated app project (plan: destructive project removal).

        Stops the managed process first (if running) so no orphan is left,
        then removes the on-disk dev project dir under
        ``data/app_builder/<app_id>/``. Packaged zips in the workspace are
        NOT affected. ``AppProjectNotFoundError`` → 404,
        ``AppProjectDeleteFailedError`` → 503 (both conforming). Returns
        204 No Content on success.
        """
        uc = _require("delete_app_project_use_case")
        try:
            await uc.execute(app_id)
        except (
            AppProjectNotFoundError,
            AppProjectInvalidError,
            AppProjectDeleteFailedError,
        ) as exc:
            _raise_conforming(
                exc, fallback=f"app project {app_id!r} could not be deleted"
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------
    # Phase 3 — managed run / stop / logs (plan §5.3 / §5.4 / §5.5 / §5.7)
    # ------------------------------------------------------------------
    @router.post("/apps/{app_id}/run", response_model=RunAppResponse)
    async def run_app(app_id: str, body: RunAppRequest) -> RunAppResponse:
        """Start (or return the already-running) managed app process.

        Maps the process manager's domain errors to the plan §5.7 HTTP
        envelope (``AppProjectNotFoundError`` → 404,
        ``AppProjectInvalidError`` → 400,
        ``AppProjectPortInUseError`` / ``AppProjectNoBindablePortError`` /
        ``AppProjectAlreadyRunningError`` → 409,
        ``AppProjectStartFailedError`` → 502). A lean container that left
        the use case ``None`` surfaces a clean 503.
        """
        uc = _require("run_app_project_use_case")
        try:
            info = await uc.execute(app_id, port=body.port)
        except (
            AppProjectNotFoundError,
            AppProjectInvalidError,
            AppProjectPortInUseError,
            AppProjectNoBindablePortError,
            AppProjectAlreadyRunningError,
            AppProjectStartFailedError,
        ) as exc:
            _raise_conforming(
                exc, fallback=f"app project {app_id!r} could not be started"
            )
        return _to_run_response(info)

    @router.delete("/apps/{app_id}/run", response_model=RunAppResponse)
    async def stop_app(app_id: str) -> RunAppResponse:
        """Stop the managed app process (kills the whole tree).

        ``AppProjectNotRunningError`` → 409 with the stable code.
        """
        uc = _require("stop_app_project_use_case")
        try:
            info = await uc.execute(app_id)
        except AppProjectNotRunningError as exc:
            _raise_conforming(
                exc, fallback=f"app project {app_id!r} is not running"
            )
        return _to_run_response(info)

    @router.get("/apps/{app_id}/logs", response_model=AppLogsResponse)
    async def get_app_logs(app_id: str) -> AppLogsResponse:
        """Return the retained stdout/stderr tail + live status (plan §5.5).

        Graceful for a not-running app: rather than 409, returns an empty
        log tail with ``status="stopped"``. This endpoint doubles as the
        frontend's status-poll source, which polls BEFORE the managed
        process is registered (during spawn) and AFTER it stops — a 409
        there is an expected, non-error condition, so we report the steady
        "stopped" state instead of erroring (avoids console 409 spam and a
        non-conforming error body on a normal lifecycle transition).
        """
        logs_uc = _require("get_app_project_logs_use_case")
        status_uc = getattr(_services(), "get_app_project_status_use_case", None)
        try:
            output = await logs_uc.execute(app_id)
        except AppProjectNotRunningError:
            return AppLogsResponse(app_id=app_id, status="stopped", output="")
        status = "running"
        if status_uc is not None:
            try:
                status = (await status_uc.execute(app_id)).status
            except AppProjectNotRunningError:
                status = "stopped"
        return AppLogsResponse(app_id=app_id, status=status, output=output)

    # ------------------------------------------------------------------
    # Phase 5 — packaging (plan §5.6 / §5.7 / §10.4)
    # ------------------------------------------------------------------
    @router.post(
        "/apps/{app_id}/package",
        response_model=PackageJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_package(app_id: str) -> PackageJobResponse:
        """Start an app-project packaging job; return its ``job_id`` (plan §5.6).

        ``AppProjectNotFoundError`` → 404, ``AppProjectInvalidError`` → 400,
        both with the plan §5.7 envelope. A lean container that left the use
        case ``None`` surfaces a clean 503.
        """
        uc = _require("package_app_project_use_case")
        try:
            job_id = await uc.start(app_id)
        except (AppProjectNotFoundError, AppProjectInvalidError) as exc:
            _raise_conforming(
                exc, fallback=f"app project {app_id!r} not found or invalid"
            )
        return PackageJobResponse(job_id=job_id)

    @router.get("/apps/{app_id}/package/{job_id}/progress")
    async def stream_package_progress(
        app_id: str, job_id: str
    ) -> StreamingResponse:
        """Stream packaging progress over SSE (plan §5.6).

        Emits ``event: progress`` per snapshot (payload: ``phase`` /
        ``percent`` / ``message`` / ``size_bytes`` / ``zip_path`` /
        ``is_complete``), a terminal ``event: done`` with
        ``{zip_path, size_bytes}``, and ``event: error`` carrying the
        ``{code, message}`` envelope on ``AppProjectPackageFailedError``.

        ``stream`` raises ``ValueError`` (unknown job) BEFORE the iterator
        begins → clean 404; once the iterator is awaited the response is
        committed to 200 and any further failure surfaces as an SSE error
        frame (the status code can no longer change).
        """
        uc = _require("package_app_project_use_case")
        try:
            iterator = await uc.stream(job_id)
        except ValueError as exc:
            raise NotFoundError(
                "app_builder.package_job_not_found",
                "package_job",
                job_id,
                message=f"package job {job_id!r} not found",
            ) from exc
        return StreamingResponse(
            _package_frames(iterator),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.delete(
        "/apps/{app_id}/package/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def cancel_package(app_id: str, job_id: str) -> Response:
        """Cancel / drop a packaging job (idempotent → 204)."""
        uc = _require("package_app_project_use_case")
        uc.cancel(job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------
    # Phase 5 — packaging (WS variant for progress)
    # ------------------------------------------------------------------

    async def _safe_send_json(ws: WebSocket, data: dict) -> bool:  # type: ignore[type-arg]
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            return False

    @router.websocket("/apps/{app_id}/package/{job_id}/ws")
    async def package_progress_ws(
        websocket: WebSocket, app_id: str, job_id: str
    ) -> None:
        """WebSocket variant of the packaging progress SSE endpoint.

        Wire format:
        ``{"type": "frame", "event": "<name>", "payload": {...}}``
        """
        await websocket.accept()
        progress_broadcaster = getattr(_services(), "progress_broadcaster", None)

        # If a broadcaster entry exists, replay from it (multi-subscriber).
        if progress_broadcaster is not None and progress_broadcaster.get(job_id) is not None:
            try:
                async for event_name, payload in progress_broadcaster.replay(job_id):
                    if not await _safe_send_json(
                        websocket,
                        {"type": "frame", "event": event_name, "payload": payload},
                    ):
                        return
                await _safe_send_json(websocket, {"type": "done"})
                try:
                    await websocket.close(code=1000)
                except (RuntimeError, WebSocketDisconnect):
                    pass
            except WebSocketDisconnect:
                return
            except Exception as exc:
                await _safe_send_json(
                    websocket,
                    {"type": "error", "code": "internal", "message": str(exc)},
                )
                try:
                    await websocket.close(code=1011)
                except (RuntimeError, WebSocketDisconnect):
                    pass
            return

        # Fallback: consume the use-case iterator directly (single subscriber).
        try:
            uc = _require("package_app_project_use_case")
        except Exception as exc:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "unavailable", "message": str(exc)},
            )
            try:
                await websocket.close(code=4503)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return
        try:
            iterator = await uc.stream(job_id)
        except ValueError as exc:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "not_found", "message": str(exc)},
            )
            try:
                await websocket.close(code=4404)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return

        try:
            last_zip: str | None = None
            last_size: int | None = None
            async for snap in iterator:
                last_zip = snap.zip_path
                last_size = snap.size_bytes
                payload = {
                    "phase": snap.phase,
                    "percent": snap.percent,
                    "message": snap.message,
                    "size_bytes": snap.size_bytes,
                    "zip_path": snap.zip_path,
                    "is_complete": snap.is_complete,
                }
                if not await _safe_send_json(
                    websocket,
                    {"type": "frame", "event": "progress", "payload": payload},
                ):
                    return
            await _safe_send_json(websocket, {"type": "done"})
            try:
                await websocket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
        except QaiError as exc:
            await _safe_send_json(
                websocket,
                {"type": "frame", "event": "error", "payload": exc.to_dict()},
            )
            try:
                await websocket.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "internal", "message": str(exc)},
            )
            try:
                await websocket.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass


async def _package_frames(iterator: Any) -> Any:
    """Translate a packaging progress iterator to SSE byte frames.

    One ``event: progress`` frame per snapshot, a terminal ``event: done``
    carrying ``{zip_path, size_bytes}`` from the completing snapshot, and an
    ``event: error`` frame carrying the ``QaiError`` envelope
    (``AppProjectPackageFailedError`` etc.) raised mid-stream.
    """
    last_zip: str | None = None
    last_size: int | None = None
    try:
        async for snap in iterator:
            last_zip = snap.zip_path
            last_size = snap.size_bytes
            yield format_sse_event(
                "progress",
                {
                    "phase": snap.phase,
                    "percent": snap.percent,
                    "message": snap.message,
                    "size_bytes": snap.size_bytes,
                    "zip_path": snap.zip_path,
                    "is_complete": snap.is_complete,
                },
            )
    except QaiError as exc:
        yield format_sse_event("error", exc.to_dict())
        return
    yield format_sse_event(
        "done", {"zip_path": last_zip, "size_bytes": last_size}
    )


__all__ = [
    "AppDetailResponse",
    "AppEntry",
    "AppListResponse",
    "AppLogsResponse",
    "PackageJobResponse",
    "RunAppRequest",
    "RunAppResponse",
    "register",
]
