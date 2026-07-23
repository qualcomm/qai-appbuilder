# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder route DTOs + shared mappers / helpers.

Pure architectural split of the former single-file
``interfaces/http/routes/app_builder.py`` (S-cohesion). All request /
response DTO classes, the domain→DTO mappers, the SSE frame encoder and
the module-level validation / cache-control helpers live here so the
per-group ``_register_*`` modules can import them without duplication.

No behaviour change: class names, field order semantics, wire shapes and
helper bodies are byte-for-byte identical to the pre-split module (the
OpenAPI snapshot sorts ``paths`` + ``schemas`` so neither cross-module
registration order nor DTO definition order perturbs the schema SHA).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.responses import Response
from pydantic import BaseModel, Field

from qai.app_builder.application.model_status_view import AppModelStatusInfo
from qai.app_builder.domain.aggregated_metrics import AggregatedMetrics
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.import_plan import (
    ImportAction,
    ImportPlan,
    ImportPlanItem,
)
from qai.app_builder.domain.run import Run
from qai.app_builder.domain.value_objects import AppModelId, RunId
from qai.platform.errors import ValidationError


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------


class _ArtifactPayload(BaseModel):
    """Embedded artifact descriptor in run / model responses."""

    path: str
    size_bytes: int
    kind: str
    checksum: str | None = None


class _VariantStatusPayload(BaseModel):
    """One per-variant install-status row inside :class:`AppModelResponse`."""

    id: str
    status: str


class AppModelResponse(BaseModel):
    """``AppModelDefinition`` wire shape."""

    id: str
    title: str
    taxonomy: list[str]
    enabled: bool
    pinned: bool
    input_presets: list[dict[str, Any]]
    required_catalog_ids: list[str]
    user_imported: bool = False
    # V1 parity (``GET /api/appbuilder/models`` augmented rows): install +
    # dependency status so the gallery can render the status badge. Tail-
    # appended optional fields (v2.7 §3.1 — additions only). ``status`` is
    # ``Ready`` / ``NotInstalled`` / ``Error`` (weights present on disk?);
    # ``deps_status`` is ``ready`` / ``missing`` / ``installing`` / ``None``
    # (omitted ⇒ not yet probed → frontend treats as "checking");
    # ``variant_status`` lists per-variant install state for multi-variant
    # packs (empty for legacy single-variant); ``category`` mirrors the
    # manifest's short category code; ``icon`` is the taxonomy group icon.
    # ``auto_download`` marks a built-in pack whose runner fetches its weights
    # on the first Run — lets the UI say "auto-downloads on first run" instead
    # of the bare ``NotInstalled`` a needs-conversion import also shows.
    status: str = "Ready"
    deps_status: str | None = None
    variant_status: list[_VariantStatusPayload] = Field(default_factory=list)
    category: str | None = None
    icon: str | None = None
    auto_download: bool = False


class AppModelListResponse(BaseModel):
    items: list[AppModelResponse]


class RunCreateRequest(BaseModel):
    """``POST /runs`` payload."""

    # ``model_id`` is the legitimate domain term; opt out of pydantic's
    # ``model_*`` protected-namespace warning.
    model_config = {"protected_namespaces": ()}

    model_id: str = Field(..., min_length=1, max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    """Wire shape of a :class:`Run` aggregate (REST view)."""

    model_config = {"protected_namespaces": ()}

    id: str
    model_id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    inputs: dict[str, Any]
    artifacts: list[_ArtifactPayload]
    error_message: str | None = None
    # Tail-appended (v2.7 §3.1) — latest user rating for this run, echoed
    # so the history panel can re-render the rating emoji after reload.
    # Value is the Likert ``1..5`` feedback rating (5 = 👍, 1 = 👎); the
    # frontend maps it back to its internal -1/0/1 thumb semantic. ``None``
    # when the run has no feedback row yet.
    rating: int | None = None
    # Tail-appended (v2.7 §3.1, PR-F1 / F-15) — structured failure code
    # surfaced verbatim from the runner subprocess's NDJSON ``error``
    # event (V1 ``_UserError`` parity). Examples: ``"WEIGHTS_NOT_INSTALLED"``
    # (frontend dispatches to ``voiceInput.weightsMissing``),
    # ``"AUDIO_DECODE_ERROR"`` (→ ``voiceInput.encodeFailed``). ``None``
    # for non-FAILED runs and for FAILED runs whose underlying failure
    # was an unstructured exception (only ``error_message`` populated).
    error_code: str | None = None


class CancelRunResponse(BaseModel):
    run_id: str
    status: str


class ArtifactListResponse(BaseModel):
    run_id: str
    items: list[_ArtifactPayload]


class UploadAudioResponse(BaseModel):
    artifact: _ArtifactPayload


class VoicePreferenceResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enabled: bool
    preferred_model_id: str | None = None
    # V1 parity (tail-append, v2.7 §3.1): the pinned variant so a restored
    # preference warms the exact engine variant the user picked. ``None`` →
    # adapter resolves a default variant.
    preferred_variant_id: str | None = None


class VoicePreferenceRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    enabled: bool
    preferred_model_id: str | None = None
    preferred_variant_id: str | None = None


class ImportDryRunRequest(BaseModel):
    candidates: list[str] = Field(default_factory=list)


class ImportPlanItemPayload(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_id: str
    action: str
    source: str
    reason: str | None = None
    # V1 parity (tail-appended, v2.7 §3.1): presentation-only metadata for
    # the promote candidate card — human-readable name + generation
    # timestamp. ``None`` when the candidate has no manifest metadata.
    display_name: str | None = None
    generated_at: str | None = None
    # V1 dry_run parity (tail-appended): hard validation ``errors`` (✗ — block
    # import), ``conflicts`` (⚠ — id already exists), ``suggested_version``
    # (next semver under bump). All default empty so existing callers are
    # unaffected.
    errors: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    suggested_version: str | None = None
    # V1 parity: conflict resolution policy the user picked on the promote
    # card (``bump`` / ``replace`` / ``cancel``). Sent on commit so the
    # importer can bump the version / replace-with-backup / abort. Defaults to
    # ``bump`` (V1 promote-card default).
    conflict_policy: str = "bump"


class ImportPlanResponse(BaseModel):
    items: list[ImportPlanItemPayload]
    is_empty: bool
    is_noop: bool


class ImportCommitRequest(BaseModel):
    items: list[ImportPlanItemPayload] = Field(default_factory=list)


class ImportCommitResponse(BaseModel):
    commit_id: str


class ImportRollbackRequest(BaseModel):
    commit_id: str


class ImportRollbackResponse(BaseModel):
    commit_id: str
    status: str


# ---- PR-045: share + worker status DTOs --------------------------------


class ShareCreateRequest(BaseModel):
    """``POST /share`` payload."""

    run_id: str = Field(..., min_length=1, max_length=64)
    ttl_seconds: int | None = Field(default=None, gt=0)


class ShareResponse(BaseModel):
    """Wire shape of an :class:`app_builder_share` row."""

    token: str
    run_id: str
    created_at: str
    expires_at: str | None = None
    revoked: bool = False


class ShareViewResponse(BaseModel):
    """Combined share + run payload for ``GET /share/{token}``."""

    share: ShareResponse
    run: RunResponse


class LoadedModelDTO(BaseModel):
    """Per-loaded-model DTO surfaced inside :class:`WorkerStatusResponse`.

    Wire shape mirrors SSOT §9.1's ``loadedModels[]`` element:
    ``{ modelId, variantId, lastUsedAt, ageS, state }``. We use
    snake_case here for consistency with the rest of the OpenAPI surface;
    the legacy frontend can map field names client-side. Adding new
    fields at the tail is allowed by v2.7 §3.1.
    """

    model_id: str
    variant_id: str | None = None
    last_used_at: float
    age_seconds: float
    state: str


class WorkerStatusResponse(BaseModel):
    # PR-045 fields — locked, do not rename / remove (v2.7 §3.1).
    total_workers: int
    busy_workers: int
    queued_runs: int
    # PR-301 tail-append fields surfacing the SSOT shape from
    # docs/30-ui-ux/voice-input-and-sticky-worker-multimodel.md §9.1.
    alive: bool = True
    state: str = "ready"
    active_model_id: str | None = None
    multimodel: bool = False
    loaded_models: list[LoadedModelDTO] = Field(default_factory=list)


# ---- PR-304: deferred-route DTOs ---------------------------------------


class TaxonomyNodeResponse(BaseModel):
    """One row of ``GET /taxonomy``."""

    path: list[str]
    model_count: int


class TaxonomyTreeTaskResponse(BaseModel):
    """One task leaf of ``GET /taxonomy/tree``."""

    id: str
    label: str
    description: str
    io: list[str]
    model_count: int


class TaxonomyTreeGroupResponse(BaseModel):
    """One group node of ``GET /taxonomy/tree``."""

    id: str
    label: str
    icon: str
    tasks: list[TaxonomyTreeTaskResponse]


class TaxonomyTreeResponse(BaseModel):
    """``GET /taxonomy/tree`` body (full static tree, V1 parity).

    Mirrors the legacy ``GET /api/appbuilder/taxonomy`` shape
    (``{version, groups: [...]}``) so the setup-bar picker can render the
    complete catalogue of group / task labels, icons, descriptions and IO
    kinds — including tasks with zero installed models.
    """

    version: str
    groups: list[TaxonomyTreeGroupResponse]


class DepsStatusResponse(BaseModel):
    """``GET /deps-status`` body."""

    qairt_env_present: bool
    pack_root_present: bool
    shared_dir_present: bool
    sticky_worker_alive: bool
    registered_pack_count: int


class PackDepProgressResponse(BaseModel):
    """One pack's dependency-install progress row (V1 ``depsStatus`` shape).

    Mirrors the legacy ``GET /api/appbuilder/deps-status`` per-pack entry
    (``backend/app_builder/dep_checker.py`` ``_dep_status`` dict). The
    camelCase ``errorKind`` / ``errorHint`` / ``errorRaw`` fields preserve
    the V1 front-end contract (``useAppBuilderRegistry.js:287-309``).
    """

    satisfied: bool
    missing: list[str] = Field(default_factory=list)
    installing: bool = False
    errorKind: str | None = None  # noqa: N815 — V1 wire field name
    errorHint: str | None = None  # noqa: N815 — V1 wire field name
    errorRaw: str | None = None  # noqa: N815 — V1 wire field name


class DepsProgressResponse(BaseModel):
    """``GET /deps-status/packs`` body — per-pack install progress.

    Additive companion to :class:`DepsStatusResponse` (the environment
    overview). Restores the V1 逐 pack 进度 shape so the gallery can poll
    every 5s and flip each model's badge "installing → ready / missing".
    ``checking`` is true while a background probe/install task is in flight
    (V1 ``data.checking``).
    """

    checking: bool = False
    packs: dict[str, PackDepProgressResponse] = Field(default_factory=dict)


class PreloadRequest(BaseModel):
    """Optional ``POST /voice-input/preload`` request body.

    V1 parity (``api_routes.py:979`` ``{ "modelId": ..., "variantId": ... }``):
    the chat toolbar passes the currently-selected engine so the warm-up is
    parameter-driven. Both fields are optional — when omitted (e.g. the
    startup warm-up), the use case falls back to the persisted
    ``preferred_model_id``. Tail-append-only optional body per v2.7 §3.1
    (route path/method and the response shape are unchanged).
    """

    model_id: str | None = None
    variant_id: str | None = None


class PreloadResultResponse(BaseModel):
    """``POST /voice-input/preload`` body."""

    status: str
    model_id: str | None = None
    variant_id: str | None = None
    detail: str = ""


class RunMetricsResponse(BaseModel):
    """``GET /metrics/{run_id}`` body."""

    run_id: str
    status: str
    artifact_count: int
    duration_ms: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    # 缺口 #6 — append-only (v2.7 §3.1). Runner-reported pure-inference
    # latency (``metrics.latencyMs``), distinct from end-to-end
    # ``duration_ms``. ``None`` for runs with no recorded latency (older
    # runs / runner emitted no ``metrics`` event); the frontend then falls
    # back to ``duration_ms`` / "—" (HistoryPanel.js:237-238 parity).
    latency_ms: float | None = None


class AggregatedLatencyPayload(BaseModel):
    """Latency percentile summary inside :class:`AggregatedMetricsResponse`.

    camelCase keys match the frontend ``AggregatedMetrics`` contract
    (``frontend/src/composables/app-builder/useAggregatedMetrics.ts:35``).
    """

    p50: float | None = None
    p90: float | None = None
    p99: float | None = None
    mean: float | None = None
    max: float | None = None


class AggregatedMemoryPayload(BaseModel):
    """Peak-memory summary inside :class:`AggregatedMetricsResponse`."""

    mean: float | None = None
    max: float | None = None


class AggregatedRatingPayload(BaseModel):
    """User-rating fold inside :class:`AggregatedMetricsResponse`.

    Field names match the frontend contract
    (``useAggregatedMetrics.ts:46``): ``thumbsUp / thumbsDown /
    qualityScore / count / avg``.
    """

    thumbsUp: int
    thumbsDown: int
    qualityScore: float
    count: int
    avg: float | None = None


class AggregatedMetricsResponse(BaseModel):
    """``GET /metrics/model/{model_id}`` body (model-level aggregate).

    Restores the V1 ``GET /api/appbuilder/metrics/{model_id}`` shape
    (``backend/app_builder/telemetry.py:27-42``). camelCase field names
    are the already-landed frontend contract
    (``useAggregatedMetrics.ts`` ``AggregatedMetrics`` interface), so the
    composable / ``MetricsView`` consume the body with no view-side
    change. ``latencyMs`` / ``memoryMB`` / ``rating`` are ``null`` when
    the corresponding sample is empty; ``count == 0`` (HTTP 200) means
    "no successful runs yet" and the panel stays hidden.
    """

    modelId: str
    variantId: str | None = None
    count: int
    latencyMs: AggregatedLatencyPayload | None = None
    memoryMB: AggregatedMemoryPayload | None = None
    rating: AggregatedRatingPayload | None = None


class CacheStatusResponse(BaseModel):
    """``GET /cache/status`` body."""

    blob_count: int
    total_bytes: int
    blob_dir: str


class CacheClearResponse(BaseModel):
    """``DELETE /cache`` body."""

    deleted_files: int


class BinScanResultResponse(BaseModel):
    """One row of ``POST /import/scan-bins`` body.

    Tail-appended fields (v2.7 §3.1 — additions only) surface the
    precision-artifact metadata the multi-variant PromoteCard checklist
    needs (V1 ``scan-bins`` parity): ``precision`` (plan-form token),
    ``label`` (UI display label), ``mtime`` (ISO-8601 UTC).
    """

    path: str
    size_bytes: int
    suspected_model_id: str | None = None
    precision: str | None = None
    label: str | None = None
    mtime: str | None = None


class NeedsNormalizePayload(BaseModel):
    """Set when the readiness scan found NO variants under ``output/`` but the
    workdir holds a downloaded-but-not-normalized AI Hub model (a weight +
    metadata.json, typically in a nested ``<model>-qnn_dlc-*`` subfolder).

    Lets the UI show an actionable "detected an un-normalized model — run Step
    6.5 to make it importable" guidance instead of a blank Import panel. The
    backend does NOT auto-normalize (inferred fields need human confirmation);
    it only surfaces the signal. Tail-appended / optional — omitted (``None``)
    in the normal case, so the ``results`` contract is unchanged.
    """

    model_workdir: str
    detected_weight: str


class BinScanResponse(BaseModel):
    results: list[BinScanResultResponse]
    # Optional (tail-appended): present only when results is empty AND an
    # un-normalized AI Hub package was detected in the workdir.
    needs_normalize: NeedsNormalizePayload | None = None


class ScanBinsRequestBody(BaseModel):
    """``POST /import/scan-bins`` request body.

    Optional ``model_workdir`` enables workspace mode: scans
    ``<model_workdir>/output/<model>_<label>.bin`` precision artifacts
    and decodes each into ``{precision, label, mtime}``. When omitted
    the route falls back to the legacy fingerprint-free ``scan_root``
    directory listing (so existing no-body callers keep working).
    """

    model_workdir: str | None = Field(default=None, max_length=4096)


class BatchRunRequestPayload(BaseModel):
    """One row of the ``POST /batch`` request body."""

    model_id: str = Field(..., min_length=1, max_length=128)
    inputs: dict[str, object] = Field(default_factory=dict)


class BatchRunRequestBody(BaseModel):
    """``POST /batch`` request body envelope."""

    runs: list[BatchRunRequestPayload]


class BatchRunResultResponse(BaseModel):
    """One row of the ``POST /batch`` response body."""

    model_id: str
    run_id: str = ""
    error: str = ""


class BatchRunResponseBody(BaseModel):
    """``POST /batch`` response body envelope."""

    results: list[BatchRunResultResponse]


class FeedbackRequestBody(BaseModel):
    """``POST /feedback`` request body — accepted but stored opaquely."""

    run_id: str | None = Field(default=None, max_length=64)
    rating: int | None = Field(default=None, ge=1, le=5)
    text: str | None = Field(default=None, max_length=4000)
    extra: dict[str, object] = Field(default_factory=dict)


class FeedbackResponseBody(BaseModel):
    """``POST /feedback`` response body.

    The legacy backend returned ``{"ok": True}``; we keep the
    ``accepted`` boolean (locked by v2.7 §3.1 — wire-shape additions
    only) and tail-append ``feedback_id`` so clients can correlate
    the persisted row. ``note`` carries an informational status string
    only — it is not parsed by any client.
    """

    accepted: bool = True
    note: str = "feedback recorded"
    feedback_id: str = ""


class BenchmarkRequestBody(BaseModel):
    """``POST /benchmark`` request body."""

    model_id: str = Field(..., min_length=1, max_length=128)
    iterations: int = Field(default=1, ge=1, le=100)


class BenchmarkResponseBody(BaseModel):
    """``POST /benchmark`` response body.

    Returns the persisted benchmark id so clients can poll
    ``GET /api/app-builder/benchmark/{benchmark_id}`` for terminal
    status; the harness itself runs in the background. ``accepted``
    + ``note`` are kept for wire-shape stability (v2.7 §3.1) and
    carry informational state.
    """

    accepted: bool = True
    note: str = "benchmark scheduled"
    benchmark_id: str = ""


class BenchmarkStatusResponse(BaseModel):
    """``GET /benchmark/{benchmark_id}`` response body.

    Mirrors :class:`qai.app_builder.application.ports.BenchmarkRecord`
    on the wire. ``stats`` carries the p50/p90/p99/min/max/mean/std/
    count aggregate once ``status="completed"``; ``raw_latencies_ms``
    holds the per-iteration timings in chronological order.
    """

    model_config = {"protected_namespaces": ()}

    id: str
    model_id: str
    iterations: int
    warmup: int
    status: str
    stats: dict[str, float] = Field(default_factory=dict)
    raw_latencies_ms: list[float] = Field(default_factory=list)
    error_message: str | None = None
    created_at: str
    finished_at: str | None = None


class AutoExportRequestBody(BaseModel):
    """``POST /import/auto-export`` request body.

    Wire-shape rules (v2.7 §3.1: tail-append only):

    * ``source_path`` — required absolute path to the Model Builder
      workspace directory (typically under ``C:/WoS_AI/<model>/``).
      Equivalent to the legacy ``modelWorkdir`` field; new field
      name kept consistent with the rest of the App Builder import
      surface.
    * ``model_name`` — optional override for ``MODEL_NAME``; defaults
      to the workspace directory name.
    * ``precisions`` — optional list of plan-form (``fp16`` / ``w8a8``
      / ``w4a16``) or label-form (``int8`` / ``int4``) precision
      tokens to export as ``manifest.variants[]`` entries. When empty
      the workspace reader auto-detects a single precision from the
      first available ``<model>_<label>.bin`` under ``output/``.
    * ``default_precision`` — optional explicit default for the
      multi-variant case; must be one of ``precisions`` (when set).
    * ``category_override`` / ``display_name_override`` /
      ``input_kind_override`` / ``output_kind_override`` — short-
      circuit the auto-inference so callers can bypass the taxonomy
      classifier when the model is misclassified.
    * ``pack_id_override`` — optional explicit ``manifest.modelId``
      (must be kebab-case if provided).
    """

    source_path: str = Field(..., min_length=1, max_length=4096)
    model_name: str | None = Field(default=None, max_length=256)
    precisions: list[str] = Field(default_factory=list, max_length=16)
    default_precision: str | None = Field(default=None, max_length=32)
    category_override: str | None = Field(default=None, max_length=64)
    display_name_override: str | None = Field(default=None, max_length=256)
    input_kind_override: str | None = Field(default=None, max_length=32)
    output_kind_override: str | None = Field(default=None, max_length=32)
    pack_id_override: str | None = Field(default=None, max_length=128)


class AutoExportResponseBody(BaseModel):
    """``POST /import/auto-export`` response body.

    Wire-shape rules (v2.7 §3.1: tail-append only):

    * ``accepted`` — kept verbatim for compatibility with clients that
      treat the route as fire-and-forget. Always ``True`` once the
      request parsed successfully — non-2xx HTTP status carries the
      validation / infrastructure failures.
    * ``note`` — short informational message; ``"export complete"``
      on success, ``"export failed"`` on soft-validation failure.
    * ``success`` — mirrors :attr:`AutoExportJobResult.success`
      (i.e. ``_candidate.json:ready``).
    * ``pack_id`` — emitted ``manifest.modelId``.
    * ``display_name`` — emitted ``manifest.displayName``.
    * ``source_workdir`` — absolute path to the emitted
      ``<workdir>/app_pack/`` directory; the App Builder import flow
      passes this back into ``POST /import/dry-run`` and
      ``POST /import/commit``.
    * ``output`` — last ~1 KiB of the export log (legacy field).
    * ``errors`` — soft-failure messages (missing precisions, asset
      copy errors, etc.); empty when ``success`` is ``True``.
    """

    accepted: bool = True
    note: str = "auto-export request acknowledged"
    success: bool = False
    pack_id: str = ""
    display_name: str = ""
    source_workdir: str = ""
    output: str = ""
    errors: list[str] = Field(default_factory=list)


class AutoExportStatusRequestBody(BaseModel):
    """``POST /import/auto-export/status`` request body.

    Cheap on-disk probe the Import panel polls on (re)open so an
    in-flight generation survives closing the window: the synchronous
    ``auto-export`` route keeps no in-memory job, and the frontend's
    ``exporting`` flag is lost on close, so the durable state lives in
    ``<source_path>/app_pack/`` (``.generating`` sentinel while running,
    ``_candidate.json`` once finished).
    """

    source_path: str = Field(..., min_length=1, max_length=4096)


class AutoExportStatusResponse(BaseModel):
    """``POST /import/auto-export/status`` response body.

    ``status`` is one of:

    * ``"generating"`` — an export is running (fresh ``.generating``
      sentinel, no candidate yet) → the panel keeps showing "生成中...";
    * ``"generated"`` — ``_candidate.json`` exists (export finished) →
      the panel advances to the commit / import stage;
    * ``"idle"`` — never generated, or a stale/abandoned run → the panel
      shows the pick-precision / Generate stage.
    """

    status: str = "idle"


class PackManifestResponse(BaseModel):
    """``GET /models/{model_id}/manifest`` body.

    Surfaces a flattened JSON view of the :class:`PackManifest` aggregate.
    Frontend uses this to render Schema-driven UI (PR-305 will deepen
    the consumption end).
    """

    schema_version: int
    model_id: str
    display_name: str
    version: str
    vendor: str
    description: str
    long_description: str
    tags: list[str]
    runtime: dict[str, object]
    runner: dict[str, object]
    capabilities: dict[str, bool]
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None
    params: list[dict[str, object]]
    metrics: dict[str, float]
    assets: dict[str, object]
    skill: dict[str, object]
    variants: list[dict[str, object]]
    # V1 parity: manifest ``taxonomy.tags`` (curated display tags shown in the
    # CLASSIFICATION block / info drawer), distinct from top-level ``tags``.
    taxonomy_tags: list[str] = Field(default_factory=list)
    # V1 parity: manifest ``examples`` (preset inputs for one-click apply).
    examples: list[dict[str, Any]] = Field(default_factory=list)
    # Trilingual ``{en, zh-CN, zh-TW}`` default "Send to Chat" prompt a
    # generated WebUI app pre-fills by locale. Tail-appended optional field
    # (v2.7 §3.1 — additions only).
    send_to_chat_prompt: dict[str, str] = Field(default_factory=dict)


class RunsListResponse(BaseModel):
    """``GET /runs`` body — paginated list of runs."""

    runs: list[RunResponse]
    limit: int
    offset: int


# ---- PR-305: SKILL.md + Schema + appbuilder_run DTOs --------------------


class ModelSchemaResponse(BaseModel):
    """``GET /models/{model_id}/schema`` body — schema-only view."""

    model_id: str
    title: str
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None
    variants: list[dict[str, object]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Mappers (domain → DTO)
# ---------------------------------------------------------------------------


def _model_to_dto(
    model: AppModelDefinition,
    status_info: "AppModelStatusInfo | None" = None,
) -> AppModelResponse:
    info = status_info or AppModelStatusInfo()
    return AppModelResponse(
        id=str(model.id),
        title=model.title,
        taxonomy=list(model.taxonomy.segments),
        enabled=model.enabled,
        pinned=model.pinned,
        input_presets=[
            {"name": preset.name, "payload": preset.payload}
            for preset in model.input_presets
        ],
        required_catalog_ids=list(model.required_catalog_ids),
        user_imported=model.user_imported,
        status=info.status,
        deps_status=info.deps_status,
        variant_status=[
            _VariantStatusPayload(id=v.id, status=v.status)
            for v in info.variant_status
        ],
        category=info.category,
        icon=info.icon,
        auto_download=info.auto_download,
    )


def _run_to_dto(run: Run, *, rating: int | None = None) -> RunResponse:
    return RunResponse(
        id=str(run.id),
        model_id=str(run.model_id),
        status=run.status.value,
        created_at=run.created_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        inputs=dict(run.inputs),
        artifacts=[
            _ArtifactPayload(
                path=a.path,
                size_bytes=a.size_bytes,
                kind=a.kind.value,
                checksum=a.checksum.value if a.checksum is not None else None,
            )
            for a in run.artifacts
        ],
        error_message=run.error_message,
        rating=rating,
        error_code=run.error_code,
    )


def _aggregated_metrics_to_dto(
    agg: AggregatedMetrics,
) -> "AggregatedMetricsResponse":
    """Map the domain :class:`AggregatedMetrics` to its camelCase wire DTO.

    ``latency`` / ``memory`` / ``rating`` map to ``null`` payloads when
    their sample was empty (the presenter hides the corresponding row).
    """
    latency = (
        AggregatedLatencyPayload(
            p50=agg.latency.p50,
            p90=agg.latency.p90,
            p99=agg.latency.p99,
            mean=agg.latency.mean,
            max=agg.latency.max,
        )
        if agg.latency is not None
        else None
    )
    memory = (
        AggregatedMemoryPayload(mean=agg.memory.mean, max=agg.memory.max)
        if agg.memory is not None
        else None
    )
    rating = (
        AggregatedRatingPayload(
            thumbsUp=agg.rating.thumbs_up,
            thumbsDown=agg.rating.thumbs_down,
            qualityScore=agg.rating.quality_score,
            count=agg.rating.count,
            avg=agg.rating.avg,
        )
        if agg.rating is not None
        else None
    )
    return AggregatedMetricsResponse(
        modelId=agg.model_id,
        variantId=agg.variant_id,
        count=agg.count,
        latencyMs=latency,
        memoryMB=memory,
        rating=rating,
    )


async def _ratings_for_runs(
    services: Any, runs: "list[Run] | tuple[Run, ...]"
) -> dict[str, int]:
    """Batch-resolve ``{run_id: latest_rating}`` for the given runs.

    Goes through :meth:`FeedbackRepositoryPort.latest_ratings_for_runs`
    so the history surface stays a single query regardless of page size
    (avoids the N+1 fan-out of one ``latest_for_run`` per run). Returns
    an empty mapping when the feedback repository is not wired (stripped
    test containers) so the route degrades to "no ratings" rather than
    failing.
    """
    repo = getattr(services, "feedback_repository", None)
    if repo is None or not runs:
        return {}
    return await repo.latest_ratings_for_runs([r.id for r in runs])


def _plan_item_payload_to_domain(item: ImportPlanItemPayload) -> ImportPlanItem:
    try:
        action = ImportAction(item.action)
    except ValueError as exc:  # pragma: no cover — caught by handler below
        raise ValidationError(
            "app_builder.import_action_invalid",
            f"unknown import action {item.action!r}",
            field_errors={"action": [str(exc)]},
        ) from exc
    return ImportPlanItem(
        model_id=AppModelId(value=item.model_id),
        action=action,
        source=item.source,
        reason=item.reason,
        # V1 parity: carry the user's conflict policy through to commit so the
        # importer can bump the version / replace-with-backup / abort. Other
        # presentation fields (display_name / generated_at / errors / conflicts
        # / suggested_version) are re-derived by the importer's own validation
        # on commit, so we don't round-trip them from the client (avoids a
        # client forging "no errors").
        conflict_policy=item.conflict_policy,
    )


def _plan_to_dto(plan: ImportPlan) -> ImportPlanResponse:
    return ImportPlanResponse(
        items=[
            ImportPlanItemPayload(
                model_id=str(item.model_id),
                action=item.action.value,
                source=item.source,
                reason=item.reason,
                display_name=item.display_name,
                generated_at=item.generated_at,
                errors=list(item.errors),
                conflicts=list(item.conflicts),
                suggested_version=item.suggested_version,
                conflict_policy=item.conflict_policy,
            )
            for item in plan.items
        ],
        is_empty=plan.is_empty,
        is_noop=plan.is_noop,
    )


# ---------------------------------------------------------------------------
# SSE frame helpers
# ---------------------------------------------------------------------------


def _sse_event(event: str, payload: dict[str, Any]) -> bytes:
    """Format one SSE frame.

    Per S3-sub-agent-spec §4.4: ``event: <name>\\ndata: <json>\\n\\n``.
    """
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Module-level route helpers (no ``container`` capture)
# ---------------------------------------------------------------------------


def _no_store(response: Response) -> None:
    # App Builder responses (taxonomy / models / schemas / runs) are dynamic
    # registry data that changes when packs are installed/removed or the
    # backend is upgraded. Without an explicit cache directive browsers apply
    # *heuristic* caching to these GET responses, so a client that fetched an
    # older shape (e.g. a pre-upgrade taxonomy with fewer groups) keeps serving
    # the stale body from disk cache even after a hard reload — the data never
    # refreshes until a cache-busting query string is added. Force
    # ``Cache-Control: no-store`` on every response from this router so the
    # client always reflects the live registry state.
    response.headers["Cache-Control"] = "no-store"


def _validate_app_model_id(raw: str) -> AppModelId:
    try:
        return AppModelId(value=raw)
    except ValueError as exc:
        raise ValidationError(
            "app_builder.app_model_id_invalid",
            str(exc),
            field_errors={"model_id": [str(exc)]},
        ) from exc


def _validate_run_id(raw: str) -> RunId:
    try:
        return RunId(value=raw)
    except ValueError as exc:
        raise ValidationError(
            "app_builder.run_id_invalid",
            str(exc),
            field_errors={"run_id": [str(exc)]},
        ) from exc
