# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — run lifecycle + artifacts routes.

Holds every endpoint that touches the run aggregate or its artifacts:
REST create/read/cancel, the SSE replay stream, artifact list + blob
download, audio upload, batch, paginated list, history delete, markdown
export and metrics.

``create_run`` and ``stream_run`` MUST live in the same module: they
share the per-``build_router``-call ``RunStreamBroadcaster`` fallback so
a fast SSE subscriber sees the same frame buffers the POST drainer fills.
The shared instance is reached through the ``broadcaster_getter`` injected
by :func:`register` (the ``_broadcaster`` closure built once in
:mod:`.__init__`), preserving the "prefer DI singleton, else fallback"
logic byte-for-byte.

Handler bodies are unchanged from the pre-split module; the thin
``_services`` / ``_broadcaster`` / ``_validate_*`` closures redefined at
the top of :func:`register` keep the handler text identical.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Path as FastApiPath,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import Response, StreamingResponse

from ._dto import (
    AggregatedMetricsResponse,
    ArtifactListResponse,
    BatchRunRequestBody,
    BatchRunResponseBody,
    BatchRunResultResponse,
    CancelRunResponse,
    RunCreateRequest,
    RunMetricsResponse,
    RunResponse,
    RunsListResponse,
    UploadAudioResponse,
    _ArtifactPayload,
    _aggregated_metrics_to_dto,
    _ratings_for_runs,
    _run_to_dto,
    _sse_event,
    _validate_app_model_id,
    _validate_run_id,
)

from qai.app_builder.application.run_stream_broadcaster import (
    RunStreamBroadcaster,
)
from qai.app_builder.application.use_cases.cancel_run import CancelRunUseCase
from qai.app_builder.application.use_cases.deferred_routes import (
    BatchRunRequest,
)
from qai.app_builder.application.use_cases.export_run_markdown import (
    ExportRunMarkdownUseCase,
)
from qai.app_builder.application.use_cases.get_aggregated_metrics import (
    GetAggregatedMetricsForModelUseCase,
)
from qai.app_builder.application.use_cases.get_run import GetRunUseCase
from qai.app_builder.application.use_cases.list_run_artifacts import (
    ListRunArtifactsUseCase,
)
from qai.app_builder.application.use_cases.run_app import RunAppUseCase
from qai.app_builder.application.use_cases.upload_audio import UploadAudioUseCase
from qai.app_builder.domain.value_objects import RunId
from qai.platform.errors import NotFoundError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def register(
    router: APIRouter,
    *,
    container: "Container",
    broadcaster_getter: "Any",
) -> None:
    """Mount the run + artifact routes onto ``router``.

    ``broadcaster_getter`` is the ``_broadcaster`` closure created once
    per ``build_router`` call; it implements the "prefer DI singleton,
    else router-scoped fallback" resolution shared by ``create_run`` and
    ``stream_run``.
    """

    def _services() -> Any:
        return container.app_builder

    def _broadcaster() -> RunStreamBroadcaster:
        return broadcaster_getter()

    # ---- runs (REST) ------------------------------------------------------

    @router.post(
        "/runs",
        response_model=RunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_run(body: RunCreateRequest) -> RunResponse:
        """Start a new run and drive it to completion in the background.

        The HTTP response returns immediately with the just-created
        ``Run`` aggregate (``status="streaming"`` once the runner has
        begun emitting frames). Clients consume frames either by
        subscribing to ``/runs/{run_id}/stream`` (SSE) or by polling
        ``GET /runs/{run_id}``.

        Background drainer (R17 — moved to the application layer):

        The route hands the use-case iterator to the broadcaster's
        background drainer (:meth:`RunStreamBroadcaster.schedule_drain`),
        which tees each NDJSON v3.1 frame into the per-run broadcast
        registry so the GET-stream handler can replay the full frame
        history to any subscriber, regardless of whether the client
        connected before, during, or after the run terminated. The use
        case still owns the state machine; the broadcaster is purely a
        frame-fan-out mechanism.
        """
        vid = _validate_app_model_id(body.model_id)
        services = _services()
        uc: RunAppUseCase = services.run_app_use_case
        # Drive the use case to completion in a background task; the route
        # itself only ensures the PENDING run is persisted before responding
        # so polling clients can see it. Errors during streaming are caught
        # by the use case and persisted as FAILED runs.
        iterator = await uc.execute(model_id=vid, inputs=body.inputs)

        # Read back the freshly-saved run BEFORE starting the drainer so
        # we have a stable run_id to register with the broadcast
        # buffer (the use case persists the PENDING run before
        # returning the iterator).
        runs_repo = services.run_repository
        most_recent = await runs_repo.get_last_for_model(vid)
        if most_recent is None:  # pragma: no cover — defensive
            raise NotFoundError(
                "app_builder.run_not_found",
                "run",
                "<just-created>",
            )

        # Register the broadcast entry + spawn the background drainer.
        # ``schedule_drain`` registers NOW (so a fast SSE subscriber that
        # opens the stream before the first frame arrives sees an empty-
        # but-valid entry rather than racing the drainer) and starts the
        # tee in a background task.
        _broadcaster().schedule_drain(str(most_recent.id), iterator)
        return _run_to_dto(most_recent)

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        rid = _validate_run_id(run_id)
        services = _services()
        uc: GetRunUseCase = services.get_run_use_case
        run = await uc.execute(run_id=rid)
        ratings = await _ratings_for_runs(services, [run])
        return _run_to_dto(run, rating=ratings.get(str(run.id)))

    @router.delete("/runs/{run_id}", response_model=CancelRunResponse)
    async def cancel_run(
        run_id: str,
        reason: str | None = None,
    ) -> CancelRunResponse:
        rid = _validate_run_id(run_id)
        services = _services()
        uc: CancelRunUseCase = services.cancel_run_use_case
        await uc.execute(run_id=rid, reason=reason)
        run = await services.run_repository.get(rid)
        return CancelRunResponse(run_id=str(run.id), status=run.status.value)

    # ---- runs (SSE) -------------------------------------------------------

    @router.get("/runs/{run_id}/stream")
    async def stream_run(run_id: str) -> StreamingResponse:
        """SSE stream of state transitions + frames for a run.

        Frame contract documented in the module docstring (§ "SSE frame
        contract"). The handler attaches to the existing run via the
        repository: it does NOT start a new run (that's ``POST /runs``).

        Frame source (R17 — moved to the application layer):

        The replay state machine lives in
        :meth:`RunStreamBroadcaster.replay`; it consumes the per-run
        broadcast registry populated by the ``POST /runs`` drainer and
        yields abstract ``(event_name, payload)`` tuples. New
        subscribers see the full frame history (the registry is
        cumulative). When the registry has no entry for the run (server
        restarted between POST and GET, or the TTL expired) it falls
        back to the repository's terminal-status snapshot so a polling
        client at least learns whether the run finished. This handler
        only validates the run exists (so 404 surfaces cleanly before
        the stream opens) and encodes each tuple to SSE wire bytes.
        """
        rid = _validate_run_id(run_id)
        services = _services()
        runs_repo = services.run_repository
        # Verify the run exists up-front so we can surface 404 cleanly
        # (errors that happen mid-stream become ``event: error`` frames).
        run = await runs_repo.get(rid)
        broadcaster = _broadcaster()
        _log.info("stream_run_sse: connection opened run_id=%s", run_id)

        async def _generator() -> AsyncIterator[bytes]:
            async for event_name, payload in broadcaster.replay(
                rid,
                run_repository=runs_repo,
                initial_status=run.status,
            ):
                yield _sse_event(event_name, payload)

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- runs (WS) --------------------------------------------------------

    async def _safe_send_json(ws: WebSocket, data: dict) -> bool:  # type: ignore[type-arg]
        """Send ``data`` as JSON; swallow disconnect races.

        Returns ``True`` on success, ``False`` if the peer is gone.
        """
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            return False

    @router.websocket("/runs/{run_id}/ws")
    async def run_stream_ws(
        websocket: WebSocket,
        run_id: str,
        from_seq: int = Query(default=0, ge=0),
    ) -> None:
        """WebSocket variant of ``GET /runs/{run_id}/stream`` (SSE).

        Pure server→client push: replays the run's frame buffer via the
        same :class:`RunStreamBroadcaster` that backs the SSE endpoint,
        then closes normally. Uses the standard WS wire format:
        ``{"type": "frame", "event": "<name>", "payload": {...}}`` per
        event, ``{"type": "done"}`` at end, ``{"type": "error", ...}``
        on failure.

        ``from_seq`` (default 0) — a reconnecting client that already
        applied frames up to sequence *S* passes ``from_seq=S + 1`` so
        only subsequent frames are delivered.
        """
        await websocket.accept()
        _log.info("run_stream_ws: connection accepted run_id=%s from_seq=%s", run_id, from_seq)
        broadcaster = _broadcaster()
        services = _services()
        runs_repo = services.run_repository
        rid = _validate_run_id(run_id)
        try:
            run = await runs_repo.get(rid)
        except Exception:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "not_found", "message": f"Run {run_id} not found"},
            )
            try:
                await websocket.close(code=4404)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return
        try:
            seq_cursor = 0
            async for event_name, payload in broadcaster.replay(
                rid,
                run_repository=runs_repo,
                initial_status=run.status,
            ):
                # Skip frames below from_seq (sequence-based reconnection).
                if from_seq > 0 and event_name == "frame":
                    seq = payload.get("sequence", seq_cursor)
                    if seq < from_seq:
                        seq_cursor = seq + 1
                        continue
                    seq_cursor = seq + 1
                if not await _safe_send_json(
                    websocket,
                    {"type": "frame", "event": event_name, "payload": payload},
                ):
                    return
            await _safe_send_json(websocket, {"type": "done"})
            _log.info("run_stream_ws: stream completed run_id=%s", run_id)
            try:
                await websocket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
        except WebSocketDisconnect:
            return
        except Exception as exc:
            _log.warning("run_stream_ws: error run_id=%s exc=%s", run_id, exc)
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "internal", "message": str(exc)},
            )
            try:
                await websocket.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass

    # ---- artifacts --------------------------------------------------------

    @router.get(
        "/runs/{run_id}/artifacts", response_model=ArtifactListResponse
    )
    async def list_run_artifacts(run_id: str) -> ArtifactListResponse:
        rid = _validate_run_id(run_id)
        uc: ListRunArtifactsUseCase = (
            _services().list_run_artifacts_use_case
        )
        artifacts = await uc.execute(run_id=rid)
        return ArtifactListResponse(
            run_id=str(rid),
            items=[
                _ArtifactPayload(
                    path=a.path,
                    size_bytes=a.size_bytes,
                    kind=a.kind.value,
                    checksum=a.checksum.value if a.checksum is not None else None,
                )
                for a in artifacts
            ],
        )

    @router.get("/artifacts/{run_id}/{relative_path:path}/blob")
    async def download_artifact_blob(
        run_id: str,
        relative_path: str = FastApiPath(..., description="Relative artifact path"),
    ) -> StreamingResponse:
        """Stream the bytes of an artifact previously produced by a run.

        Per spec constraint #5 the response is a ``StreamingResponse``
        — the entire blob is never buffered in memory.
        """
        rid = _validate_run_id(run_id)
        services = _services()
        # Verify run exists (404 if not).
        run = await services.run_repository.get(rid)
        # Verify the artifact is attached to this run.
        if not any(a.path == relative_path for a in run.artifacts):
            raise NotFoundError(
                "app_builder.artifact_not_found",
                "artifact",
                f"{run.id}:{relative_path}",
            )
        reader = services.artifact_blob_reader

        async def _iter() -> AsyncIterator[bytes]:
            async for chunk in reader.open(
                run_id=str(run.id), relative_path=relative_path
            ):
                yield chunk

        return StreamingResponse(
            _iter(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{relative_path.split("/")[-1]}"'
                ),
            },
        )

    # ---- upload (multipart) -----------------------------------------------

    @router.post(
        "/upload/audio",
        response_model=UploadAudioResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_audio(
        file: UploadFile = File(..., description="Audio file (multipart)"),
        content_type_override: str | None = Form(default=None),
    ) -> UploadAudioResponse:
        """Persist a user-uploaded audio file under the data dir.

        FastAPI handles multipart parsing (per spec constraint #4 we do
        NOT roll our own form parser).
        """
        if not file.filename:
            raise ValidationError(
                "app_builder.audio_filename_missing",
                "Uploaded file has no filename.",
                field_errors={"file": ["filename is required"]},
            )
        data = await file.read()
        ctype = content_type_override or file.content_type or "application/octet-stream"
        uc: UploadAudioUseCase = _services().upload_audio_use_case
        try:
            artifact = await uc.execute(
                filename=file.filename, data=data, content_type=ctype
            )
        except ValueError as exc:
            raise ValidationError(
                "app_builder.audio_invalid",
                str(exc),
                field_errors={"file": [str(exc)]},
            ) from exc
        return UploadAudioResponse(
            artifact=_ArtifactPayload(
                path=artifact.path,
                size_bytes=artifact.size_bytes,
                kind=artifact.kind.value,
                checksum=artifact.checksum.value
                if artifact.checksum is not None
                else None,
            )
        )

    # ---- 12. batch ----------------------------------------------------
    @router.post("/batch", response_model=BatchRunResponseBody)
    async def run_batch(body: BatchRunRequestBody) -> BatchRunResponseBody:
        uc = _services().run_batch_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="batch use case not wired")
        requests = [
            BatchRunRequest(model_id=r.model_id, inputs=dict(r.inputs))
            for r in body.runs
        ]
        results = await uc.execute(requests)
        return BatchRunResponseBody(
            results=[
                BatchRunResultResponse(
                    model_id=r.model_id, run_id=r.run_id, error=r.error
                )
                for r in results
            ]
        )

    # ---- 13. runs (list) ----------------------------------------------
    @router.get("/runs", response_model=RunsListResponse)
    async def list_runs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        model_id: str | None = Query(default=None),
        variant_id: str | None = Query(default=None),
    ) -> RunsListResponse:
        """List runs, newest first.

        Default (no ``model_id``) returns the global cross-model list
        (unchanged legacy behaviour). When ``model_id`` is supplied the
        list is scoped to that model's runs — restoring V1's per-model
        Run History panel (``HistoryPanel.js:57-78`` →
        ``GET /history/{model_id}/runs``). ``variant_id`` further narrows
        to a single precision variant (the legacy ``"_default"`` sentinel
        means "any variant"). Both are append-only query params; the
        existing response shape is unchanged (AGENTS §3.1).
        """
        uc = _services().list_runs_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="list-runs use case not wired")
        scoped_model = (
            _validate_app_model_id(model_id) if model_id is not None else None
        )
        runs = await uc.execute(
            limit=limit,
            offset=offset,
            model_id=scoped_model,
            variant_id=variant_id,
        )
        ratings = await _ratings_for_runs(_services(), runs)
        return RunsListResponse(
            runs=[_run_to_dto(r, rating=ratings.get(str(r.id))) for r in runs],
            limit=limit,
            offset=offset,
        )

    # ---- 14. history/runs/{run_id} (delete) ---------------------------
    @router.delete("/history/runs/{run_id}", response_model=CancelRunResponse)
    async def delete_run_history(run_id: str) -> CancelRunResponse:
        from qai.app_builder.domain.errors import RunNotFoundError as _RNF

        uc = _services().delete_run_history_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="delete-history use case not wired")
        try:
            await uc.execute(RunId(value=run_id))
        except _RNF as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CancelRunResponse(status="deleted", run_id=run_id)

    # ---- 7. metrics ---------------------------------------------------
    @router.get(
        "/metrics/{run_id}",
        response_model=RunMetricsResponse,
    )
    async def get_run_metrics(run_id: str) -> RunMetricsResponse:
        from qai.app_builder.domain.errors import RunNotFoundError as _RNF

        uc = _services().get_metrics_for_run_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="metrics use case not wired")
        try:
            m = await uc.execute(RunId(value=run_id))
        except _RNF as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunMetricsResponse(
            run_id=m.run_id,
            status=m.status,
            artifact_count=m.artifact_count,
            duration_ms=m.duration_ms,
            started_at=m.started_at,
            finished_at=m.finished_at,
            error_message=m.error_message,
            latency_ms=m.latency_ms,
        )

    # ---- 7b. model-level aggregated metrics (3-M1) --------------------
    @router.get(
        "/metrics/model/{model_id}",
        response_model=AggregatedMetricsResponse,
    )
    async def get_model_aggregated_metrics(
        model_id: str,
        variant_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> AggregatedMetricsResponse:
        """Historical aggregate over the last N successful runs of a model.

        Restores the V1 ``GET /api/appbuilder/metrics/{model_id}`` panel
        (``backend/app_builder/api_routes.py:1599-1643`` +
        ``telemetry.py:77-145``): latency percentiles (p50/p90/p99/mean/max
        from each completed run's wall-clock duration) + a folded user
        rating summary. Mounted on a distinct ``/metrics/model/...`` path
        so the ``{model_id}`` parameter never collides with the single-run
        ``/metrics/{run_id}`` route above.

        ``variant_id`` filters to a single pack variant; the legacy
        ``"_default"`` sentinel is normalised to "all variants" (V1
        parity). An empty history returns HTTP 200 with ``count == 0`` and
        ``null`` sub-objects — never 404 — so the frontend renders the
        "no successful runs yet" hidden state rather than the
        endpoint-missing ``pendingBackend`` placeholder.
        """
        mid = _validate_app_model_id(model_id)
        # V1 ``_default`` sentinel → aggregate across all variants.
        vid = None if (not variant_id or variant_id == "_default") else variant_id
        services = _services()
        uc = getattr(services, "get_aggregated_metrics_use_case", None)
        if uc is None:
            # Resilient fallback so the route is operational even if the DI
            # wiring has not yet been extended (additive change — mirrors
            # the export_run_markdown route's on-the-fly construction).
            uc = GetAggregatedMetricsForModelUseCase(
                runs=services.run_repository,
                feedback=getattr(services, "feedback_repository", None),
            )
        agg = await uc.execute(mid, variant_id=vid, limit=limit)
        return _aggregated_metrics_to_dto(agg)

    # ---- 20. runs/{run_id}/export.md  (PR-094 §17.5 #14) ---------------
    @router.get(
        "/runs/{run_id}/export.md",
        response_class=Response,
        responses={
            200: {
                "content": {"text/markdown": {}},
                "description": "Markdown report for the run",
            }
        },
    )
    async def export_run_markdown(run_id: str) -> Response:
        """Return a Markdown report for a finished (or in-flight) run.

        Mirrors the legacy ``GET /api/appbuilder/runs/{run_id}/export``
        format restored by PR-094 (§17.5 #14 / §3.3 A-14). Pulled from
        :class:`ExportRunMarkdownUseCase`; 404 surfaces unknown run ids
        via the existing :class:`NotFoundError` envelope.
        """
        from qai.app_builder.domain.errors import RunNotFoundError as _RNF

        rid = _validate_run_id(run_id)
        services = _services()
        uc = getattr(services, "export_run_markdown_use_case", None)
        if uc is None:
            # Fall back to constructing the use case on the fly so the
            # route is operational even if the DI module hasn't yet been
            # extended (PR-094 wiring lands in the same PR but the wiring
            # change is additive — keep the route resilient).
            uc = ExportRunMarkdownUseCase(runs=services.run_repository)
        try:
            md = await uc.execute(run_id=rid)
        except _RNF as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=md,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="run-{rid.value}.md"'
                ),
            },
        )
