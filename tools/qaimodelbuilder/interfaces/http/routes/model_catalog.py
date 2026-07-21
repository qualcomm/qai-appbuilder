# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model catalog HTTP routes.

S3 PR-032 scope: 12 routes under ``/api/model-catalog`` exposing the 11
application use cases for the ``model_catalog`` bounded context.

Routes
------
- ``GET    /api/model-catalog/entries``                       — list entries
- ``POST   /api/model-catalog/entries``                       — register entry
- ``GET    /api/model-catalog/entries/{model_id}``            — get entry
- ``DELETE /api/model-catalog/entries/{model_id}``            — remove entry
- ``GET    /api/model-catalog/entries/{model_id}/versions``   — list versions
- ``POST   /api/model-catalog/download``                      — start download
- ``DELETE /api/model-catalog/download/{job_id}``             — cancel download
- ``GET    /api/model-catalog/download/{job_id}/progress``    — SSE progress
- ``POST   /api/model-catalog/verify``                        — checksum verify
- ``POST   /api/model-catalog/release-manifest/refresh``      — refresh manifest
- ``GET    /api/model-catalog/providers``                     — list providers
- ``PUT    /api/model-catalog/providers/{provider_id}``       — update provider

SSE wire-format contract (``GET /download/{job_id}/progress``)
--------------------------------------------------------------
Frozen here per S3-spec §4.4; PR-033/034/035 must mirror this shape:

* ``event: progress\\ndata: <progress-json>\\n\\n``  — one frame per
  ``DownloadProgress`` snapshot yielded by the use case.
* ``event: done\\ndata: {}\\n\\n``                   — end-of-stream.
* ``event: error\\ndata: <QaiError.to_dict()>\\n\\n`` — sent before
  closing if the use case raises a ``QaiError``; the stream closes
  immediately after.

All frames are encoded as UTF-8 bytes; ``data:`` is a single line
(no embedded newlines). Heartbeats are NOT emitted by this layer; the
use case is the source of truth for stream cadence.

Error handling
--------------
``ModelEntryNotFoundError`` / ``DownloadJobNotFoundError`` etc. are
raised by the use cases and translated to the canonical envelope by
the global ``register_error_handlers`` (PR-030). Routes never roll
their own JSON error responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from interfaces.http.sse import stream_progress_frames as _stream_progress_frames

from qai.model_catalog.application.use_cases.register_model_entry import (
    RegisterModelEntryCommand,
)
from qai.model_catalog.application.use_cases.remove_version import (
    RemoveVersionCommand,
)
from qai.model_catalog.application.use_cases.start_download import (
    StartDownloadCommand,
)
from qai.model_catalog.application.use_cases.update_provider_config import (
    UpdateProviderConfigCommand,
)
from qai.model_catalog.application.use_cases.verify_checksum import (
    VerifyChecksumCommand,
)
from qai.model_catalog.domain.entities import ModelEntry, ModelVersion
from qai.model_catalog.domain.ids import (
    DownloadJobId,
    ModelEntryId,
    ModelVersionId,
)
from qai.model_catalog.domain.value_objects import (
    ProviderKind,
    SourceUrl,
    StorageKey,
    Taxonomy,
)
from qai.platform.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------


class _VersionDTO(BaseModel):
    """Wire form of a :class:`ModelVersion`."""

    model_config = ConfigDict(protected_namespaces=())

    version_id: str
    parent_model_id: str
    checksum_algorithm: str
    checksum_value: str
    size_bytes: int
    manifest_url: str
    status: str


class ModelEntryResponse(BaseModel):
    """Wire form of a :class:`ModelEntry` aggregate."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    name: str
    provider: str
    source_url: str
    description: str
    tags: list[str]
    current_version_id: str | None
    versions: list[_VersionDTO]


class ModelEntriesResponse(BaseModel):
    """Plural envelope so the response is forward-compatible (paging, etc.)."""

    entries: list[ModelEntryResponse]


class VersionsResponse(BaseModel):
    """Versions of a single model entry."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    versions: list[_VersionDTO]


class RegisterModelEntryRequest(BaseModel):
    """Body for ``POST /api/model-catalog/entries``."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(
        ...,
        description="One of: local, ollama, openai_compat, anthropic, generic_cloud",
    )
    source_url: str = Field(..., min_length=1, max_length=2048)
    description: str = Field("", max_length=4096)
    tags: list[str] = Field(default_factory=list)


class StartDownloadRequest(BaseModel):
    """Body for ``POST /api/model-catalog/download``."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(..., min_length=1, max_length=128)
    version_id: str = Field(..., min_length=1, max_length=128)
    target_filename: str = Field(..., min_length=1, max_length=255)


class DownloadJobResponse(BaseModel):
    """Wire form of a :class:`DownloadJob`."""

    job_id: str
    target_model_version_id: str
    state: str
    bytes_downloaded: int
    total_bytes: int | None
    speed_bps: float
    eta_seconds: float | None
    failure_reason: str | None


class DownloadJobsResponse(BaseModel):
    """List of active :class:`DownloadJob` items (PR-044 / issue f)."""

    jobs: list[DownloadJobResponse]


class VerifyChecksumRequest(BaseModel):
    """Body for ``POST /api/model-catalog/verify``."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(..., min_length=1, max_length=128)
    version_id: str = Field(..., min_length=1, max_length=128)
    storage_category: str = Field(..., min_length=1, max_length=32)
    storage_name: str = Field(..., min_length=1, max_length=255)


class VerifyChecksumResponse(BaseModel):
    """Verification outcome (mismatch raises 422 via ``ChecksumMismatchError``)."""

    status: str  # "verified"


class ReleaseManifestEntryDTO(BaseModel):
    """Wire form of a :class:`ReleaseManifestEntry`."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    version_id: str
    checksum_algorithm: str
    checksum_value: str
    size_bytes: int
    download_url: str


class ReleaseManifestResponse(BaseModel):
    """Wire form of a :class:`ReleaseManifest`."""

    manifest_version: str
    fetched_at: str
    entry_count: int
    entries: list[ReleaseManifestEntryDTO]


class ProviderConfigResponse(BaseModel):
    """Wire form of a single provider config row."""

    provider_id: str
    config: dict[str, Any]


class ProviderConfigsResponse(BaseModel):
    """List of provider configs."""

    providers: list[dict[str, Any]]


class CloudModelDTO(BaseModel):
    """Wire form of one cloud-inference model (legacy ``cloud_models.json``).

    Free-form ``provider`` string (``cloud_llm`` / ``provider_b`` / ...)
    is the dropdown grouping key — intentionally NOT the restricted
    :class:`ProviderKind` enum used by download ``entries``.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    name: str
    provider: str
    context_length: int | None = None
    description: str = ""
    supports_streaming: bool = True
    is_local: bool = False
    #: Optional upstream *wire* model id (V1 ``api_model_id``) sent to the
    #: provider's API when it differs from the display ``model_id`` (e.g.
    #: dated ``claude-sonnet-4-20250514``). Absent → send ``model_id`` as-is.
    api_model_id: str | None = None
    params: dict[str, Any] | None = None


class CloudModelsResponse(BaseModel):
    """Plural envelope mirroring the legacy ``/api/models`` cloud merge."""

    models: list[CloudModelDTO]


class CloudModelPermissionsResponse(BaseModel):
    """Envelope for ``GET /api/model-catalog/cloud-models/permissions``.

    Reports the current per-``(provider_id, model_id)`` permission snapshot
    filled in by the lifespan background scan. Values are the string form of
    :class:`qai.model_catalog.application.use_cases.PermissionStatus`
    (``"unknown"`` / ``"allowed"`` / ``"denied"``). ``UNKNOWN`` covers both
    "not probed yet" and "probe failed" — the frontend treats both as "show"
    (never-preset-unavailable). Missing (provider_id, model_id) entries are
    equivalent to ``"unknown"``.

    Contract note (§3.1): purely additive route + response — the frozen
    ``/cloud-models`` list shape is untouched. The scan is best-effort and
    non-blocking; this endpoint can return an empty ``permissions`` object
    at any time (e.g. before the first scan completes) and the frontend
    degrades gracefully.
    """

    permissions: dict[str, dict[str, str]]


class QueryServiceDTO(BaseModel):
    """One internal query service for the chat model dropdown's "查询服务" group.

    A query service is selected like a model but routes via the reserved
    ``query::<id>`` model hint (the ``model_id`` field below). Internal-only:
    this DTO is only ever populated on internal editions (see the gated
    endpoint), so external builds surface an empty list and no dropdown group.
    No credentials / endpoints are returned — only what the UI needs to list +
    select the service.
    """

    service_id: str
    display_name: str
    model_id: str  # the ``query::<service_id>`` hint the dropdown selects


class QueryServicesResponse(BaseModel):
    """Envelope for ``GET /api/model-catalog/query-services``."""

    services: list[QueryServiceDTO]


class UpdateProviderConfigRequest(BaseModel):
    """Body for ``PUT /api/model-catalog/providers/{provider_id}``."""

    config: dict[str, Any]


# ---------------------------------------------------------------------------
# Domain → DTO mappers
# ---------------------------------------------------------------------------


def _version_to_dto(v: ModelVersion) -> _VersionDTO:
    return _VersionDTO(
        version_id=v.version_id.value,
        parent_model_id=v.parent_model_id.value,
        checksum_algorithm=v.checksum.algorithm.value,
        checksum_value=v.checksum.value,
        size_bytes=v.size_bytes.value,
        manifest_url=v.manifest_url.value,
        status=v.status.value,
    )


def _entry_to_dto(entry: ModelEntry) -> ModelEntryResponse:
    return ModelEntryResponse(
        model_id=entry.model_id.value,
        name=entry.name,
        provider=entry.provider.value,
        source_url=entry.source_url.value,
        description=entry.description,
        tags=list(entry.taxonomy.tags),
        current_version_id=(
            entry.current_version_id.value
            if entry.current_version_id is not None
            else None
        ),
        versions=[_version_to_dto(v) for v in entry.versions],
    )


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
#
# The ``DownloadProgress`` → SSE translation (``event: progress`` /
# ``done`` / ``error`` frames) that used to live here as the private
# ``_format_sse_event`` / ``_stream_progress_frames`` helpers has been
# promoted to the shared :mod:`interfaces.http.sse` module now that a
# second context (App Builder weight downloads) needs the identical wire
# contract. ``_stream_progress_frames`` above is a thin import alias for
# ``interfaces.http.sse.stream_progress_frames`` so the route handler text
# (and thus the byte-identical SSE frames + existing tests) is unchanged.


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the model_catalog router bound to the given DI container.

    The router holds no module-level state; it is reconstructed every
    time ``apps.api.main.create_app`` is called.
    """
    router = APIRouter(prefix="/api/model-catalog", tags=["model_catalog"])

    # ── Entries ────────────────────────────────────────────────────────

    @router.get("/entries", response_model=ModelEntriesResponse)
    async def list_entries() -> ModelEntriesResponse:
        entries = await container.model_catalog.list_model_entries_use_case.execute()
        return ModelEntriesResponse(entries=[_entry_to_dto(e) for e in entries])

    @router.post(
        "/entries",
        response_model=ModelEntryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_entry(body: RegisterModelEntryRequest) -> ModelEntryResponse:
        try:
            provider_kind = ProviderKind(body.provider)
        except ValueError as exc:
            raise ValidationError(
                "model_catalog.invalid_provider",
                f"unknown provider {body.provider!r}",
                field_errors={"provider": [str(exc)]},
            ) from exc

        command = RegisterModelEntryCommand(
            model_id=ModelEntryId(body.model_id),
            name=body.name,
            provider=provider_kind,
            source_url=SourceUrl(value=body.source_url),
            description=body.description,
            taxonomy=Taxonomy(tags=tuple(body.tags)),
        )
        entry = await container.model_catalog.register_model_entry_use_case.execute(
            command
        )
        return _entry_to_dto(entry)

    @router.get("/entries/{model_id}", response_model=ModelEntryResponse)
    async def get_entry(model_id: str) -> ModelEntryResponse:
        entry = await container.model_catalog.get_model_entry_use_case.execute(
            ModelEntryId(model_id)
        )
        return _entry_to_dto(entry)

    @router.delete(
        "/entries/{model_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def remove_entry(model_id: str) -> Response:
        await container.model_catalog.remove_model_entry_use_case.execute(
            ModelEntryId(model_id)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/entries/{model_id}/versions",
        response_model=VersionsResponse,
    )
    async def list_versions(model_id: str) -> VersionsResponse:
        # Reuse GetModelEntryUseCase: project to versions only.
        entry = await container.model_catalog.get_model_entry_use_case.execute(
            ModelEntryId(model_id)
        )
        return VersionsResponse(
            model_id=entry.model_id.value,
            versions=[_version_to_dto(v) for v in entry.versions],
        )

    @router.delete(
        "/entries/{model_id}/versions/{version_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def remove_version(model_id: str, version_id: str) -> Response:
        """Delete one :class:`ModelVersion` while keeping the parent entry.

        Issue (f) decision A — PR-044 introduces
        :class:`RemoveVersionUseCase`. The entry-level delete remains
        :meth:`DELETE /entries/{model_id}` (cascade).
        """
        await container.model_catalog.remove_version_use_case.execute(
            RemoveVersionCommand(
                model_id=ModelEntryId(model_id),
                version_id=ModelVersionId(version_id),
            )
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── Download ───────────────────────────────────────────────────────

    @router.post(
        "/download",
        response_model=DownloadJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_download(body: StartDownloadRequest) -> DownloadJobResponse:
        command = StartDownloadCommand(
            model_id=ModelEntryId(body.model_id),
            version_id=ModelVersionId(body.version_id),
            target_filename=body.target_filename,
        )
        job = await container.model_catalog.start_download_use_case.execute(command)
        return DownloadJobResponse(
            job_id=job.job_id.value,
            target_model_version_id=job.target_model_version_id.value,
            state=job.state.value,
            bytes_downloaded=job.progress.bytes_downloaded,
            total_bytes=job.progress.total_bytes,
            speed_bps=job.progress.speed_bps,
            eta_seconds=job.progress.eta_seconds,
            failure_reason=job.failure_reason,
        )

    @router.delete(
        "/download/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def cancel_download(job_id: str) -> Response:
        await container.model_catalog.cancel_download_use_case.execute(
            DownloadJobId(job_id)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/download-jobs",
        response_model=DownloadJobsResponse,
    )
    async def list_download_jobs() -> DownloadJobsResponse:
        """List active (non-terminal) download jobs.

        Issue (f) decision A — PR-044 introduces
        :class:`ListDownloadJobsUseCase` so the legacy
        ``GET /api/aria2c/tasks`` capability is restored under the
        Clean Cutover ``/api/model-catalog/`` prefix.
        """
        jobs = await container.model_catalog.list_download_jobs_use_case.execute()
        return DownloadJobsResponse(
            jobs=[
                DownloadJobResponse(
                    job_id=j.job_id.value,
                    target_model_version_id=j.target_model_version_id.value,
                    state=j.state.value,
                    bytes_downloaded=j.progress.bytes_downloaded,
                    total_bytes=j.progress.total_bytes,
                    speed_bps=j.progress.speed_bps,
                    eta_seconds=j.progress.eta_seconds,
                    failure_reason=j.failure_reason,
                )
                for j in jobs
            ]
        )

    @router.get("/download/{job_id}/progress")
    async def stream_download_progress(job_id: str) -> StreamingResponse:
        # NB: ``execute`` *itself* may raise ``DownloadJobNotFoundError``
        # before the stream begins, in which case the global handler
        # produces a clean 404 envelope. Once the iterator is awaited we
        # have committed to a 200 response and any further failure is
        # surfaced as an ``event: error`` SSE frame.
        iterator = await container.model_catalog.stream_download_progress_use_case.execute(
            DownloadJobId(job_id)
        )
        return StreamingResponse(
            _stream_progress_frames(iterator),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Download (WS variant) ──────────────────────────────────────────

    async def _safe_send_json(ws: WebSocket, data: dict) -> bool:  # type: ignore[type-arg]
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            return False

    @router.websocket("/download/{job_id}/ws")
    async def download_progress_ws(websocket: WebSocket, job_id: str) -> None:
        """WebSocket variant of the model-catalog download progress SSE.

        Wire format:
        ``{"type": "frame", "event": "<name>", "payload": {...}}``
        """
        await websocket.accept()

        # Try to use ProgressBroadcaster from DI if available.
        progress_broadcaster = getattr(
            getattr(container, "app_builder", None), "progress_broadcaster", None
        )
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
            iterator = await container.model_catalog.stream_download_progress_use_case.execute(
                DownloadJobId(job_id)
            )
        except Exception as exc:
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
            async for snapshot in iterator:
                payload = {
                    "bytes_downloaded": snapshot.bytes_downloaded,
                    "total_bytes": snapshot.total_bytes,
                    "speed_bps": snapshot.speed_bps,
                    "eta_seconds": snapshot.eta_seconds,
                    "percent": snapshot.percent,
                    "is_complete": snapshot.is_complete,
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

    # ── Checksum verification ──────────────────────────────────────────

    @router.post("/verify", response_model=VerifyChecksumResponse)
    async def verify_checksum(body: VerifyChecksumRequest) -> VerifyChecksumResponse:
        command = VerifyChecksumCommand(
            model_id=ModelEntryId(body.model_id),
            version_id=ModelVersionId(body.version_id),
            target=StorageKey(
                category=body.storage_category, name=body.storage_name
            ),
        )
        await container.model_catalog.verify_checksum_use_case.execute(command)
        return VerifyChecksumResponse(status="verified")

    # ── Release manifest ───────────────────────────────────────────────

    @router.post(
        "/release-manifest/refresh", response_model=ReleaseManifestResponse
    )
    async def refresh_release_manifest() -> ReleaseManifestResponse:
        manifest = await container.model_catalog.refresh_release_manifest_use_case.execute()
        return _release_manifest_to_dto(manifest)

    # ── Provider configs ───────────────────────────────────────────────

    @router.get("/providers", response_model=ProviderConfigsResponse)
    async def list_providers() -> ProviderConfigsResponse:
        rows = await container.model_catalog.list_provider_configs_use_case.execute()
        return ProviderConfigsResponse(providers=rows)

    @router.put(
        "/providers/{provider_id}",
        response_model=ProviderConfigResponse,
    )
    async def update_provider(
        provider_id: str, body: UpdateProviderConfigRequest
    ) -> ProviderConfigResponse:
        await container.model_catalog.update_provider_config_use_case.execute(
            UpdateProviderConfigCommand(
                provider_id=provider_id, config=body.config
            )
        )
        return ProviderConfigResponse(
            provider_id=provider_id, config=body.config
        )

    # ── Cloud-inference model catalog (functional block 2) ─────────────

    @router.get("/cloud-models", response_model=CloudModelsResponse)
    async def list_cloud_models() -> CloudModelsResponse:
        """List cloud-inference models (legacy ``cloud_models.json::models``).

        New route (additive — does not touch the frozen ``/entries`` or
        ``/providers`` contracts). The chat model dropdown reads this for
        its cloud provider groups (``cloud_llm`` / ``provider_b`` / ...).
        Credentials are never returned here; only the catalog shape.
        """
        rows = await container.model_catalog.list_cloud_models_use_case.execute()
        return CloudModelsResponse(
            models=[CloudModelDTO(**row) for row in rows]
        )

    @router.get(
        "/cloud-models/permissions",
        response_model=CloudModelPermissionsResponse,
    )
    async def list_cloud_model_permissions() -> CloudModelPermissionsResponse:
        """Return the current per-model permission snapshot.

        Additive route (§3.1). The snapshot is populated once by the lifespan
        background task (see ``apps/api/lifespan.py`` — permission scan). The
        response is safe to call at any point in the app's lifetime:

        * before the scan completes → ``permissions`` is ``{}`` (frontend
          treats every model as ``UNKNOWN`` → visible);
        * probe of a given provider failed → that provider's models resolve
          to ``UNKNOWN`` (again → visible);
        * probe succeeded → ``ALLOWED`` / ``DENIED`` per model based on the
          upstream ``GET /v1/models`` response.

        No credentials are read here — the ``SecretStore`` lookup happens
        inside the use case (writer). This route only serialises the snapshot.
        """
        store = container.model_catalog.permission_snapshot_store
        raw = store.get_snapshot()
        # Serialise ``PermissionStatus`` enum values to their string form so
        # the wire payload stays dict-of-dict-of-string (JSON-friendly, no
        # custom serialiser needed).
        wire: dict[str, dict[str, str]] = {}
        for provider_id, per_model in raw.items():
            wire[provider_id] = {
                model_id: status.value
                for model_id, status in per_model.items()
            }
        return CloudModelPermissionsResponse(permissions=wire)

    @router.get("/query-services", response_model=QueryServicesResponse)
    async def list_query_services() -> QueryServicesResponse:
        """List internal query services for the chat dropdown's "查询服务" group.

        New route (additive). A query service is selected like a model but
        routes via the ``query::<id>`` hint. **internal-only**: on external
        editions this returns an empty list (so the dropdown shows no
        "查询服务" group and no service name), because (1) the descriptors live
        in the edition-excluded config and (2) this handler is gated behind
        ``settings.is_internal``. Never returns endpoints/credentials.
        """
        settings = getattr(container, "settings", None)
        if settings is None or not getattr(settings, "is_internal", False):
            return QueryServicesResponse(services=[])
        try:
            from qai.platform.edition import get_query_services
        except Exception:  # pragma: no cover - package excluded on external
            return QueryServicesResponse(services=[])
        services: list[QueryServiceDTO] = []
        for service_id, fields in get_query_services().items():
            # Session-typed query services (e.g. MB Pro) are NOT surfaced in
            # the model dropdown's "查询服务" group: they have their own chat
            # composer mode (the "Pro / 增强" toolbar button) with an explicit
            # connect/disconnect lifecycle + settings dialog, and the mode sets
            # the ``query::<id>`` hint itself (user-invisible). Listing them
            # here too would double-expose the service (dropdown + mode button).
            # Likewise the ``gomaster`` transport: GoMaster is driven by its own
            # "GoMaster 在线 / 一键优化" toolbar mode + optimize panel, NOT by
            # picking a chat model — so it must not appear in this dropdown.
            # Only the NDJSON one-shot services (CEBot) belong in the dropdown.
            if fields.get("transport", "ndjson") in ("session", "gomaster"):
                continue
            display = fields.get("display_name")
            services.append(
                QueryServiceDTO(
                    service_id=service_id,
                    display_name=str(display) if display else service_id,
                    model_id=f"query::{service_id}",
                )
            )
        return QueryServicesResponse(services=services)

    return router


def _release_manifest_to_dto(manifest: Any) -> ReleaseManifestResponse:
    """Map :class:`ReleaseManifest` to its wire form.

    Kept module-private (no leading underscore needed at use site since
    only the router factory consumes it).
    """
    return ReleaseManifestResponse(
        manifest_version=manifest.manifest_version,
        fetched_at=manifest.fetched_at.isoformat(),
        entry_count=manifest.entry_count,
        entries=[
            ReleaseManifestEntryDTO(
                model_id=e.model_id.value,
                version_id=e.version_id.value,
                checksum_algorithm=e.checksum.algorithm.value,
                checksum_value=e.checksum.value,
                size_bytes=e.size_bytes.value,
                download_url=e.download_url.value,
            )
            for e in manifest.entries
        ],
    )


__all__ = ["build_router"]
