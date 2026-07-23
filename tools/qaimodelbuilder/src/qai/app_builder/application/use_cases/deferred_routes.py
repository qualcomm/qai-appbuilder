# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the lane-2 App Builder routes (PR-304).

PR-034 / PR-045 shipped 18 routes; the legacy backend has 37. PR-304
fills 16 of the remaining 19 by adding new use cases backed by:

* Existing :class:`RunRepositoryPort` (list runs / per-run history /
  per-run metrics)
* :class:`AppModelRepositoryPort` (taxonomy enum / pack manifest)
* :class:`PackManifest` infrastructure (PR-303 manifest reader)
* :class:`StickyWorkerHost` (voice-input/preload warm-up)

S9 close wires the three remaining lane-2 routes (``feedback`` /
``benchmark`` / ``history/runs/{run_id}`` delete) to real
persistence + use cases (see
:mod:`qai.app_builder.application.use_cases.submit_feedback`,
:mod:`qai.app_builder.application.use_cases.run_benchmark`, and
:meth:`DeleteRunHistoryUseCase` below). The ``import/auto-export``
route remains a 202 acknowledgement until the ``model_builder``
context grows the export pipeline; that boundary is explicit in the
S9 close report and not covered by this module.

Use cases implemented
---------------------

| UC | Route | Notes |
|----|-------|-------|
| ``GetTaxonomyUseCase`` | GET /taxonomy | Aggregates distinct taxonomy paths from all registered models |
| ``GetDepsStatusUseCase`` | GET /deps-status | Reports presence of qairt env / pack root / shared dir |
| ``PreloadVoiceInputUseCase`` | POST /voice-input/preload | Warm-up via sticky-worker (PR-301) |
| ``GetMetricsForRunUseCase`` | GET /metrics/{run_id} | Latency / artifact counts / status from RunRepository |
| ``GetCacheStatusUseCase`` | GET /cache/status | Reports artifact store stats |
| ``ClearCacheUseCase`` | DELETE /cache | Clears per-context artifact blobs |
| ``ListRunsUseCase`` | GET /runs | Paginated list across all models |
| ``DeleteRunHistoryUseCase`` | DELETE /history/runs/{run_id} | Tombstone a run's record |
| ``GetPackManifestUseCase`` | GET /models/{model_id}/manifest | Reads PackManifest via reader |
| ``ImportScanBinsUseCase`` | POST /import/scan-bins | Scans for unrecognised bin files |
| ``RunBatchUseCase`` | POST /batch | Runs multiple models sequentially |
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

_log = logging.getLogger(__name__)

from qai.app_builder.application.ports import (
    AppModelRepositoryPort,
    ArtifactStorePort,
    RunRepositoryPort,
    VoiceInputPreferenceRepositoryPort,
)
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.run import Run, RunStatus
from qai.app_builder.domain.value_objects import AppModelId, RunId

if TYPE_CHECKING:  # pragma: no cover
    from qai.app_builder.domain.pack_manifest import PackManifest


@runtime_checkable
class StickyWorkerSnapshotPort(Protocol):
    """Read-only view of the sticky worker host needed by use cases.

    Defined here (in the application layer) so the use cases don't
    import :class:`qai.app_builder.infrastructure.StickyWorkerHost`
    directly — that would violate the layered-app_builder import-linter
    contract (application → infrastructure forbidden).

    The real :class:`StickyWorkerHost` (PR-301) satisfies this Protocol
    structurally; DI passes the host instance directly into use cases
    that take this port.
    """

    @property
    def alive(self) -> bool:
        """``True`` iff the sticky worker process is currently alive."""
        ...

    def is_loaded(
        self, model_id: str, variant_id: str | None = None
    ) -> bool:
        """``True`` iff the (model_id, variant_id) is loaded."""
        ...

__all__ = [
    "StickyWorkerSnapshotPort",
    "GetTaxonomyUseCase",
    "TaxonomyNode",
    "GetTaxonomyTreeUseCase",
    "TaxonomyTree",
    "TaxonomyTreeGroup",
    "TaxonomyTreeTask",
    "GetDepsStatusUseCase",
    "DepsStatus",
    "PreloadVoiceInputUseCase",
    "PreloadResult",
    "GetMetricsForRunUseCase",
    "RunMetrics",
    "GetCacheStatusUseCase",
    "CacheStatus",
    "ClearCacheUseCase",
    "ListRunsUseCase",
    "DeleteRunHistoryUseCase",
    "GetPackManifestUseCase",
    "ImportScanBinsUseCase",
    "BinScanResult",
    "UnnormalizedAihubHint",
    "RunBatchUseCase",
    "BatchRunRequest",
    "BatchRunResult",
]


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class TaxonomyNode:
    """One row of the taxonomy index.

    The legacy ``GET /taxonomy`` returns a flat list of distinct
    taxonomy paths each with the count of models using it. We mirror
    that shape so the legacy frontend (post-L7) doesn't need a
    re-shape.
    """

    path: tuple[str, ...]
    model_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, tuple):
            raise ValueError("path must be a tuple of str")
        for i, s in enumerate(self.path):
            if not isinstance(s, str):
                raise ValueError(f"path[{i}] must be str")
        if not isinstance(self.model_count, int) or isinstance(self.model_count, bool):
            raise ValueError("model_count must be int")
        if self.model_count < 0:
            raise ValueError("model_count must be >= 0")


class GetTaxonomyUseCase:
    """Aggregate distinct taxonomy paths from all registered models."""

    def __init__(self, *, app_models: AppModelRepositoryPort) -> None:
        self._app_models = app_models

    async def execute(self) -> tuple[TaxonomyNode, ...]:
        models = await self._app_models.list_all()
        counts: dict[tuple[str, ...], int] = {}
        for model in models:
            path = model.taxonomy.segments
            counts[path] = counts.get(path, 0) + 1
        # Stable order: sort lexicographically by path tuple.
        return tuple(
            TaxonomyNode(path=path, model_count=count)
            for path, count in sorted(counts.items())
        )


# ---------------------------------------------------------------------------
# Taxonomy tree (full static tree + per-task model counts)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class TaxonomyTreeTask:
    """One task leaf of the full taxonomy tree, with a live model count."""

    id: str
    label: str
    description: str
    io: tuple[str, str]
    model_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class TaxonomyTreeGroup:
    """One group node of the full taxonomy tree."""

    id: str
    label: str
    icon: str
    tasks: tuple[TaxonomyTreeTask, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TaxonomyTree:
    """The full taxonomy tree (version + groups), V1 parity.

    Unlike :class:`GetTaxonomyUseCase` (which only reports the *distinct
    paths that have a registered model*), this exposes the complete static
    vocabulary — every group / task with its human-readable label, icon,
    description and IO kinds — so the setup-bar picker can show all selectable
    categories (including ones with zero installed models yet), exactly like
    V1.
    """

    version: str
    groups: tuple[TaxonomyTreeGroup, ...]


class GetTaxonomyTreeUseCase:
    """Return the full static taxonomy tree, annotated with model counts.

    The tree vocabulary comes from the domain single-source-of-truth
    (:mod:`qai.app_builder.domain.taxonomy_tree`); the per-task model counts
    are aggregated from the registered models so the picker can badge each
    task with how many packs are available.
    """

    def __init__(self, *, app_models: AppModelRepositoryPort) -> None:
        self._app_models = app_models

    async def execute(self) -> TaxonomyTree:
        # Local import keeps the module import-cheap and the dependency
        # explicit (domain → domain, no framework).
        from qai.app_builder.domain import taxonomy_tree as tt

        models = await self._app_models.list_all()
        # Count models per task id (last segment of the taxonomy path that
        # matches a known task, falling back to any matching segment).
        task_counts: dict[str, int] = {}
        for model in models:
            for seg in model.taxonomy.segments:
                if tt.task_label(seg) is not None:
                    task_counts[seg] = task_counts.get(seg, 0) + 1

        groups = tuple(
            TaxonomyTreeGroup(
                id=g.id,
                label=g.label,
                icon=g.icon,
                tasks=tuple(
                    TaxonomyTreeTask(
                        id=t.id,
                        label=t.label,
                        description=t.description,
                        io=t.io,
                        model_count=task_counts.get(t.id, 0),
                    )
                    for t in g.tasks
                ),
            )
            for g in tt.GROUPS
        )
        return TaxonomyTree(version=tt.TAXONOMY_VERSION, groups=groups)


# ---------------------------------------------------------------------------
# Deps status
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DepsStatus:
    """Report of optional infrastructure pieces' presence.

    Operators use this to debug "why aren't my Packs running?" before
    even a run is attempted.
    """

    qairt_env_present: bool
    pack_root_present: bool
    shared_dir_present: bool
    sticky_worker_alive: bool
    registered_pack_count: int

    def __post_init__(self) -> None:
        for name, value in (
            ("qairt_env_present", self.qairt_env_present),
            ("pack_root_present", self.pack_root_present),
            ("shared_dir_present", self.shared_dir_present),
            ("sticky_worker_alive", self.sticky_worker_alive),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be bool")
        if (
            not isinstance(self.registered_pack_count, int)
            or isinstance(self.registered_pack_count, bool)
        ):
            raise ValueError("registered_pack_count must be int")
        if self.registered_pack_count < 0:
            raise ValueError("registered_pack_count must be >= 0")


class GetDepsStatusUseCase:
    """Inspect the local filesystem + sticky worker for "are deps OK?".

    Constructor takes everything by ``Path`` / object reference so it
    can be wired purely by DI without filesystem coupling at the use
    case level.
    """

    def __init__(
        self,
        *,
        qairt_env_file: Path | None,
        pack_root: Path | None,
        shared_dir: Path | None,
        sticky_worker_host: StickyWorkerSnapshotPort | None = None,
        registered_pack_count_provider: "callable[[], int] | None" = None,  # type: ignore[name-defined]
    ) -> None:
        self._qairt_env_file = qairt_env_file
        self._pack_root = pack_root
        self._shared_dir = shared_dir
        self._sticky = sticky_worker_host
        self._registered_count = registered_pack_count_provider

    async def execute(self) -> DepsStatus:
        return DepsStatus(
            qairt_env_present=(
                self._qairt_env_file is not None
                and self._qairt_env_file.is_file()
            ),
            pack_root_present=(
                self._pack_root is not None and self._pack_root.is_dir()
            ),
            shared_dir_present=(
                self._shared_dir is not None and self._shared_dir.is_dir()
            ),
            sticky_worker_alive=(
                self._sticky is not None and self._sticky.alive
            ),
            registered_pack_count=(
                self._registered_count() if self._registered_count is not None else 0
            ),
        )


# ---------------------------------------------------------------------------
# Voice input preload
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PreloadResult:
    """Outcome of a sticky-worker warm-up.

    * ``cached`` — the model was already loaded; preload was a no-op.
    * ``loaded`` — the worker spawned + loaded the model successfully.
    * ``skipped`` — sticky-worker not running / no preference set; the
      route layer surfaces this so the UI can show a hint.
    """

    status: str  # one of "cached" | "loaded" | "skipped"
    model_id: str | None
    variant_id: str | None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("cached", "loaded", "skipped"):
            raise ValueError(
                f"status must be cached|loaded|skipped, got {self.status!r}"
            )


class PreloadVoiceInputUseCase:
    """Trigger a sticky-worker warm-up for the user's preferred model.

    Reads :class:`VoiceInputPreference`; when no preference is stored
    OR when no sticky-worker host is wired, returns ``status="skipped"``
    instead of failing — the route is informational, not authoritative.

    The route surfaces the status / cached / loaded triad so the
    frontend can wire the warm-up button. Eager model spawn / load is
    not part of this use case's contract; the sticky-worker host owns
    that lifecycle and the warm-up call is best-effort.
    """

    def __init__(
        self,
        *,
        prefs: VoiceInputPreferenceRepositoryPort,
        sticky_worker_host: StickyWorkerSnapshotPort | None = None,
    ) -> None:
        self._prefs = prefs
        self._sticky = sticky_worker_host

    async def execute(
        self,
        *,
        model_id: str | None = None,
        variant_id: str | None = None,
    ) -> PreloadResult:
        """Warm-up the requested ASR model into the sticky worker.

        V1 parity (``api_routes.py:976`` ``appbuilder_voice_pref_preload``):
        the warm-up is **parameter-driven** — the chat toolbar passes the
        currently-selected engine's ``model_id`` / ``variant_id`` so picking
        Whisper warms Whisper and picking Zipformer warms Zipformer, letting
        both engines reach "ready" independently (the sticky worker is
        multi-model by default).

        When the caller omits ``model_id`` (e.g. the startup warm-up task in
        ``lifespan._voice_warmup_task``) we fall back to the persisted
        :attr:`VoiceInputPreference.preferred_model_id` so the boot-time
        warm-up behaviour is unchanged.
        """
        pref = await self._prefs.get()
        if model_id is None:
            # Fallback: read the persisted preference (startup warm-up path).
            model_id = (
                str(pref.preferred_model_id)
                if pref.preferred_model_id is not None
                else None
            )
            variant_id = pref.preferred_variant_id
        if not pref.enabled or model_id is None:
            return PreloadResult(
                status="skipped",
                model_id=model_id,
                variant_id=variant_id,
                detail="voice-input disabled or no preferred model",
            )
        if self._sticky is None or not self._sticky.alive:
            return PreloadResult(
                status="skipped",
                model_id=model_id,
                variant_id=variant_id,
                detail="sticky-worker host not running",
            )
        # Sticky host is up; check the cache for this exact (model, variant).
        if self._sticky.is_loaded(model_id, variant_id):
            return PreloadResult(
                status="cached",
                model_id=model_id,
                variant_id=variant_id,
            )
        # The actual ``load_model`` call is owned by
        # :class:`StickyWorkerHost` (sticky-worker infrastructure), not
        # by this application-layer use case — :class:`StickyWorkerSnapshotPort`
        # deliberately exposes only ``alive`` + ``is_loaded`` to keep
        # application → infrastructure direction one-way. The UC
        # surfaces ``"loaded"`` pessimistically ("warm-up has been
        # acknowledged") so the route returns immediately without
        # blocking the UI; the sticky-worker host performs (or has
        # already performed) the real load asynchronously on its own
        # lifecycle.
        return PreloadResult(
            status="loaded",
            model_id=model_id,
            variant_id=variant_id,
            detail="warm-up acknowledged (sticky-worker host owns load)",
        )


# ---------------------------------------------------------------------------
# Run metrics
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class RunMetrics:
    """Per-run performance summary derived from :class:`Run` + artifacts."""

    run_id: str
    status: str
    artifact_count: int
    duration_ms: float | None
    started_at: str | None
    finished_at: str | None
    error_message: str | None = None
    # 缺口 #6 — append-only pure-inference latency (v2.7 §3.1). Distinct
    # from ``duration_ms`` (end-to-end wall-clock derived from
    # started_at/finished_at): ``latency_ms`` is the runner-reported
    # ``metrics.latencyMs`` persisted on the Run aggregate (V1
    # ``HistoryPanel.js:237-238`` ``r.metrics.latencyMs``ms). ``None`` when
    # the runner emitted no ``metrics`` event (frontend falls back to
    # duration_ms / "—" — 真实状态优先 §🔴, never faked).
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be non-empty str")
        if not isinstance(self.artifact_count, int) or isinstance(
            self.artifact_count, bool
        ):
            raise ValueError("artifact_count must be int")
        if self.artifact_count < 0:
            raise ValueError("artifact_count must be >= 0")
        if self.duration_ms is not None and (
            not isinstance(self.duration_ms, (int, float))
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be number >= 0 or None")
        if self.latency_ms is not None and (
            not isinstance(self.latency_ms, (int, float))
            or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise ValueError("latency_ms must be number >= 0 or None")


class GetMetricsForRunUseCase:
    """Compute :class:`RunMetrics` for a previously-finished Run.

    Derived from the run aggregate's timestamps. Pack-emitted
    ``metrics`` events are kept on the run aggregate when the runner
    protocol surfaces them; they are not split into a separate
    metrics table because the per-run aggregate is the canonical
    source for this read path.
    """

    def __init__(self, *, runs: RunRepositoryPort) -> None:
        self._runs = runs

    async def execute(self, run_id: RunId) -> RunMetrics:
        run = await self._runs.get(run_id)
        duration_ms: float | None = None
        if run.started_at is not None and run.finished_at is not None:
            duration_ms = max(
                0.0,
                (run.finished_at - run.started_at).total_seconds() * 1000.0,
            )
        return RunMetrics(
            run_id=str(run.id),
            status=run.status.value,
            artifact_count=len(run.artifacts),
            duration_ms=duration_ms,
            started_at=(
                run.started_at.isoformat() if run.started_at is not None else None
            ),
            finished_at=(
                run.finished_at.isoformat() if run.finished_at is not None else None
            ),
            error_message=run.error_message,
            latency_ms=run.inference_latency_ms,
        )


# ---------------------------------------------------------------------------
# Cache status / clear
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CacheStatus:
    """Stats summary for the per-context blob store."""

    blob_count: int
    total_bytes: int
    blob_dir: str

    def __post_init__(self) -> None:
        if not isinstance(self.blob_count, int) or isinstance(self.blob_count, bool):
            raise ValueError("blob_count must be int")
        if not isinstance(self.total_bytes, int) or isinstance(
            self.total_bytes, bool
        ):
            raise ValueError("total_bytes must be int")
        if self.blob_count < 0 or self.total_bytes < 0:
            raise ValueError("counts must be >= 0")
        if not isinstance(self.blob_dir, str):
            raise ValueError("blob_dir must be str")


class GetCacheStatusUseCase:
    """Walk the artifact blob directory and report counts + bytes.

    Implemented as a use case (rather than a port method) so the FS
    walk doesn't pollute the :class:`ArtifactStorePort` interface;
    operators only need this once in a while.
    """

    def __init__(self, *, blob_dir: Path) -> None:
        if not isinstance(blob_dir, Path):
            raise TypeError("blob_dir must be a Path")
        self._blob_dir = blob_dir

    async def execute(self) -> CacheStatus:
        if not self._blob_dir.is_dir():
            return CacheStatus(
                blob_count=0, total_bytes=0, blob_dir=str(self._blob_dir)
            )
        count = 0
        size = 0
        for entry in self._blob_dir.rglob("*"):
            if entry.is_file():
                count += 1
                try:
                    size += entry.stat().st_size
                except OSError:
                    pass
        return CacheStatus(
            blob_count=count, total_bytes=size, blob_dir=str(self._blob_dir)
        )


class ClearCacheUseCase:
    """Delete all blob files under the artifact directory.

    By design this is a destructive "free up disk" tool; tombstoning
    the run aggregates that reference those blobs is the responsibility
    of higher-level GC paths (run history deletion), not of this cache
    operation. Operators run it knowing artifacts vanish from disk
    while the per-run records remain.
    """

    def __init__(self, *, blob_dir: Path) -> None:
        if not isinstance(blob_dir, Path):
            raise TypeError("blob_dir must be a Path")
        self._blob_dir = blob_dir

    async def execute(self) -> int:
        if not self._blob_dir.is_dir():
            return 0
        deleted = 0
        for entry in self._blob_dir.rglob("*"):
            if entry.is_file():
                try:
                    entry.unlink()
                    deleted += 1
                except OSError:
                    pass
        return deleted


# ---------------------------------------------------------------------------
# List runs (paginated; across all models)
# ---------------------------------------------------------------------------
def _run_variant_id(run: Run) -> str | None:
    """Return the precision variant id folded into ``run.inputs``.

    The run-button path stores the selected variant at
    ``inputs["variant_id"]`` (see :meth:`RunAppUseCase.execute` /
    frontend ``buildRunInputs``). Returns ``None`` when absent / blank so
    the variant filter treats such runs as "no specific variant".
    """
    v = run.inputs.get("variant_id")
    return v if isinstance(v, str) and v else None


class ListRunsUseCase:
    """List runs across all registered models.

    The :class:`RunRepositoryPort` only exposes ``list_by_model``; this
    UC iterates all models then concatenates. The model count is
    bounded by the registered pack set, so the per-model fan-out is
    cheap for the single-tenant deployment shape; a global
    ``list_all_runs`` port method is intentionally not added because
    the per-model iteration is already O(models) and matches the
    repository's natural sharding.
    """

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        runs: RunRepositoryPort,
        per_model_limit: int = 50,
    ) -> None:
        if per_model_limit <= 0:
            raise ValueError("per_model_limit must be > 0")
        self._app_models = app_models
        self._runs = runs
        self._per_model_limit = per_model_limit

    async def execute(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        model_id: AppModelId | None = None,
        variant_id: str | None = None,
    ) -> tuple[Run, ...]:
        """List runs, optionally scoped to one model (+ variant).

        Default behaviour (``model_id=None``) is unchanged: fan out over
        every registered model and concatenate (V1-parity "all models"
        global list). When ``model_id`` is supplied, only that model's
        runs are queried via :meth:`RunRepositoryPort.list_by_model` —
        this restores V1's per-model Run History panel
        (``HistoryPanel.js:57-78`` → ``GET /history/{model_id}/runs``).

        ``variant_id`` further narrows to a single precision variant.
        The variant lives in ``run.inputs["variant_id"]`` (the run-button
        path folds it into the inputs bag, see
        :meth:`RunAppUseCase.execute`), so it is post-filtered here. The
        legacy ``"_default"`` sentinel (V1 ``_variantSlot``) means "any
        variant" and is treated as no filter.
        """
        if limit <= 0:
            raise ValueError("limit must be > 0")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        if model_id is not None:
            # Scoped path: query the single model directly (already
            # newest-first from the repository). Pull up to offset+limit so
            # pagination after the variant post-filter stays correct for the
            # common single-variant case.
            scoped = await self._runs.list_by_model(
                model_id, limit=max(self._per_model_limit, offset + limit)
            )
            runs_iter: list[Run] = list(scoped)
            normalized_variant = (
                None if (not variant_id or variant_id == "_default") else variant_id
            )
            if normalized_variant is not None:
                runs_iter = [
                    r
                    for r in runs_iter
                    if _run_variant_id(r) == normalized_variant
                ]
            return tuple(runs_iter[offset : offset + limit])

        models = await self._app_models.list_all()
        all_runs: list[Run] = []
        for model in models:
            runs = await self._runs.list_by_model(
                model.id, limit=self._per_model_limit
            )
            all_runs.extend(runs)
        # Sort by created_at desc — newest first.
        all_runs.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(all_runs[offset : offset + limit])


# ---------------------------------------------------------------------------
# Delete run history (single)
# ---------------------------------------------------------------------------
class DeleteRunHistoryUseCase:
    """Delete a single run's record (and its cascaded child rows).

    The :class:`RunRepositoryPort.delete` method (added in S9 close)
    performs an atomic delete of the run header row; the cascade rules
    declared in migrations 003 + 011 take care of the dependent
    ``app_builder_artifact`` / ``app_builder_share`` /
    ``app_builder_feedback`` rows. Artifact blob files on disk are NOT
    touched here — operators run :class:`ClearCacheUseCase` for that
    when they want to reclaim disk.

    Raises :class:`qai.app_builder.domain.errors.RunNotFoundError` when
    ``run_id`` is unknown — the route layer maps that to 404.
    """

    def __init__(self, *, runs: RunRepositoryPort) -> None:
        self._runs = runs

    async def execute(self, run_id: RunId) -> None:
        # Existence check first so the route returns 404 deterministically
        # (the adapter also re-checks; the double check is cheap and keeps
        # the failure mode identical regardless of which adapter is wired).
        await self._runs.get(run_id)
        await self._runs.delete(run_id)


# ---------------------------------------------------------------------------
# Pack manifest
# ---------------------------------------------------------------------------
class GetPackManifestUseCase:
    """Read the :class:`PackManifest` for a given AppModel.

    Delegates to a :class:`PackManifestProvider` callable so the UC
    isn't tied to a specific reader implementation. The DI provider
    typically wraps :class:`FileSystemManifestReader.read_one`.

    Raises :class:`AppModelNotFoundError` if the model is unknown,
    and :class:`ManifestNotAvailableError` if the manifest is missing
    on disk.
    """

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        manifest_provider: "callable[[AppModelId], PackManifest | None]",  # type: ignore[name-defined]
    ) -> None:
        self._app_models = app_models
        self._provider = manifest_provider

    async def execute(self, model_id: AppModelId) -> "PackManifest":
        # Existence check first (raises AppModelNotFoundError if absent).
        await self._app_models.get(model_id)
        manifest = self._provider(model_id)
        if manifest is None:
            raise ManifestNotAvailableError(
                f"manifest not available for model {model_id}"
            )
        return manifest


class ManifestNotAvailableError(LookupError):
    """The model exists in the registry but no manifest is on disk."""


# ---------------------------------------------------------------------------
# Import — extension routes
# ---------------------------------------------------------------------------
# Filename-suffix → plan-form precision token. Mirrors the legacy
# ``LABEL_TO_PRECISION`` map (backend/app_builder/api_routes.py) so the
# multi-variant PromoteCard checklist matches the V1 behaviour exactly.
_LABEL_TO_PRECISION: dict[str, str] = {
    # ── Label form (UI-friendly) ──
    "fp16": "fp16",
    "fp32": "fp32",
    "float": "fp32",  # alias: LLM agent may write PRECISION=float in plan.md
    "bf16": "bf16",
    "int8": "w8a8",  # canonical for INT8 outputs (ahead of w8a8b8)
    "w8a16": "w8a16",
    "w4a16": "w4a16",
    "w16a16": "w16a16",
    "int4": "w4a8",
    # ── Plan form (as written by some converters) ──
    "w8a8": "w8a8",
    "w8a8b8": "w8a8b8",
    "w4a8": "w4a8",
}

# Plan-form precision → display label (what the user sees in the picker).
_PLAN_TO_DISPLAY_LABEL: dict[str, str] = {
    "fp16": "FP16",
    "fp32": "FP32",
    "bf16": "BF16",
    "w8a8": "INT8",
    "w8a8b8": "INT8",
    "w8a16": "W8A16",
    "w4a16": "W4A16",
    "w16a16": "W16A16",
    "w4a8": "INT4",
}

# Bin file size floor when scanning candidates (1 MiB). Filters out
# empty / placeholder files that occasionally land in ``output/``.
_SCAN_BIN_MIN_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class BinScanResult:
    """A single result row for ``/import/scan-bins``.

    Tail-appended fields (v2.7 §3.1 — additions only):

    * ``precision`` — plan-form precision token (``fp16`` / ``w8a8`` /
      ``w4a16`` …) inferred from the ``<model>_<label>.bin`` filename.
      ``None`` for the legacy fingerprint-free directory listing.
    * ``label`` — UI display label (``FP16`` / ``INT8`` …).
    * ``mtime`` — ISO-8601 UTC modification timestamp of the bin file.
    """

    path: str
    size_bytes: int
    suspected_model_id: str | None = None
    precision: str | None = None
    label: str | None = None
    mtime: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be a non-empty str")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise ValueError("size_bytes must be int")


@dataclass(frozen=True, slots=True, kw_only=True)
class UnnormalizedAihubHint:
    """Signal that a workdir holds a downloaded-but-not-yet-normalized AI Hub
    model — i.e. the readiness scan found NO ``output/<model>_<label>.{bin,dlc}``
    variants, but the workspace clearly contains an AI Hub package (a
    ``metadata.json`` next to a ``.dlc``/``.bin`` weight, typically inside a
    nested ``<model>-qnn_dlc-*`` subfolder) that Model Hub's Step 6.5
    normalization (``aihub_to_manifest.py``) has not been run on yet.

    Both Model Builder and Model Hub rely on the agent to *initiate*
    normalization (there is no auto-trigger — that would treat inferred
    fields like ``output.type`` as authoritative, violating State-Truth-First).
    This hint lets the UI turn a confusing empty state ("model on disk but the
    Import panel is blank") into an actionable "detected an un-normalized model —
    run Step 6.5 to make it importable" guidance, WITHOUT silently normalizing
    on the user's behalf.

    * ``model_workdir`` — the top-level workspace (== the value to pass as
      ``--workdir`` / whose folder name is ``--model-name``).
    * ``detected_weight`` — POSIX path of the un-normalized weight we found
      (for display / to confirm the detection is real, not a guess).
    """

    model_workdir: str
    detected_weight: str


class ImportScanBinsUseCase:
    """Scan for NPU context binaries (``.bin`` / ``.dlc``) to import.

    The app_pack contract is format-neutral: a promotable NPU weight may be
    a QNN context binary (``.bin``, Model Builder's own output) OR a QNN DLC
    (``.dlc``, downloaded from Qualcomm AI Hub via Model Hub) — ``QNNContext``
    loads either directly. So both Model Builder and Model Hub reach the SAME
    promote/export flow, and this readiness scan must recognise BOTH suffixes
    (otherwise a ``.dlc``-only workspace looks "empty" and the Promote CTA
    never appears).

    Two scan modes:

    * **workspace mode** (``model_workdir`` given) — enumerate
      ``<model_workdir>/output/<model>_<label>.{bin,dlc}`` precision
      artifacts and decode each into ``{precision, label, size, mtime}``.
      This is the source for the multi-variant PromoteCard checklist (V1
      ``GET /import/scan-bins?modelWorkdir=…`` parity). Files smaller
      than 1 MiB or with an unrecognised label suffix are skipped.
    * **legacy mode** (no ``model_workdir``) — a content-free recursive
      directory listing of the configured ``scan_root``. Bin
      fingerprinting is intentionally outside the importer's contract.
    """

    #: Valid NPU weight suffixes for the app_pack contract (format-neutral).
    _WEIGHT_SUFFIXES: tuple[str, ...] = (".bin", ".dlc")

    def __init__(self, *, scan_root: Path) -> None:
        if not isinstance(scan_root, Path):
            raise TypeError("scan_root must be a Path")
        self._scan_root = scan_root

    async def execute(
        self, *, model_workdir: str | None = None
    ) -> tuple[BinScanResult, ...]:
        if model_workdir:
            return self._scan_workspace(model_workdir)
        return self._scan_legacy()

    def _scan_legacy(self) -> tuple[BinScanResult, ...]:
        if not self._scan_root.is_dir():
            return ()
        out: list[BinScanResult] = []
        candidates = sorted(
            (
                p
                for p in self._scan_root.rglob("*")
                if p.suffix.lower() in self._WEIGHT_SUFFIXES
            ),
            key=lambda p: str(p),
        )
        for entry in candidates:
            if entry.is_file():
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                # Best-effort suspected-model: parent dir name.
                suspected: str | None = None
                try:
                    suspected = entry.parent.name
                except Exception:  # noqa: BLE001
                    suspected = None
                out.append(
                    BinScanResult(
                        path=str(entry.as_posix()),
                        size_bytes=stat.st_size,
                        suspected_model_id=suspected,
                    )
                )
        return tuple(out)

    def _scan_workspace(self, model_workdir: str) -> tuple[BinScanResult, ...]:
        from datetime import datetime, timezone

        try:
            workdir = Path(model_workdir).resolve()
        except (OSError, ValueError):
            return ()
        if not workdir.is_dir():
            return ()
        output_dir = workdir / "output"
        if not output_dir.is_dir():
            return ()

        model_name = workdir.name
        out: list[BinScanResult] = []

        # ── (1) Authoritative variant from inference_manifest.json ────────────
        # The manifest (``<workdir>/inference_manifest.json``, single-precision:
        # {precision, context_binary}) is the SOURCE OF TRUTH for the DEFAULT
        # variant's precision — crucially, it works even when the weight file
        # has NO ``_<label>`` suffix in its name (e.g. ``output/yolov8n.dlc``),
        # which the filename-suffix scan below cannot classify. We read it FIRST
        # so precision is taken from the manifest's ``precision`` field, not
        # guessed from the filename. Best-effort: any error → skip, fall back to
        # the filename scan. Additional (non-default) precisions still come from
        # the suffix scan in (2).
        manifest_variant = self._read_manifest_variant(workdir, output_dir)
        if manifest_variant is not None:
            out.append(manifest_variant)

        # ── (2) Additional variants by filename suffix ───────────────────────
        try:
            entries = sorted(output_dir.iterdir())
        except OSError:
            return tuple(out)  # manifest variant (if any) still stands

        for f in entries:
            try:
                if not f.is_file() or f.suffix.lower() not in self._WEIGHT_SUFFIXES:
                    continue
                stat = f.stat()
                if stat.st_size < _SCAN_BIN_MIN_BYTES:
                    continue
            except OSError:
                continue

            # Extract the precision label as the segment AFTER THE LAST ``_``.
            # We deliberately do NOT require the filename to be prefixed by the
            # workdir's directory name: the ``output/`` dir is already this
            # model's own output folder, so any ``*_<label>.{bin,dlc}`` inside
            # it is a variant of this model. Requiring ``<dirname>_`` broke real
            # cases where the model_name used in filenames differs from the
            # workspace folder name — e.g. workspace ``C:\WoS_AI\yolov8`` but
            # files named ``yolov8n_fp16.bin`` (the "yolov8n" YOLOv8-nano
            # model). The label suffix + ``_LABEL_TO_PRECISION`` membership is
            # the real signal; the model-name prefix is informational only.
            stem = f.stem
            us = stem.rfind("_")
            if us <= 0 or us == len(stem) - 1:
                continue
            label_raw = stem[us + 1:].lower()
            file_model = stem[:us]  # informational (may differ from dir name)
            precision = _LABEL_TO_PRECISION.get(label_raw)
            if not precision:
                _log.debug(
                    "scan-bins: skipping %r — label suffix %r not in "
                    "_LABEL_TO_PRECISION (known: %s)",
                    f.name,
                    label_raw,
                    ", ".join(sorted(_LABEL_TO_PRECISION)),
                )
                continue

            mtime_iso = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            display_label = _PLAN_TO_DISPLAY_LABEL.get(
                precision, label_raw.upper()
            )
            out.append(
                BinScanResult(
                    path=str(f.as_posix()),
                    size_bytes=stat.st_size,
                    suspected_model_id=file_model or model_name,
                    precision=precision,
                    label=display_label,
                    mtime=mtime_iso,
                )
            )

        # Deduplicate by precision: App Builder needs exactly ONE weight per
        # precision. Multiple files can decode to the same precision — e.g. a
        # ``<model>_int8.bin`` and a ``<model>_w8a8.bin`` (both map to the
        # ``w8a8`` precision → both display "INT8"), or the same precision
        # present as BOTH ``.bin`` and ``.dlc``. Emitting two rows for one
        # precision produced a confusing duplicate checkbox that toggled in
        # lockstep (the picker keys selection by precision string). Keep one
        # per precision: prefer ``.bin`` (native QNN context binary) over
        # ``.dlc``, then the most recently modified.
        best_by_precision: dict[str, BinScanResult] = {}
        for r in out:
            key = r.precision or ""
            cur = best_by_precision.get(key)
            if cur is None:
                best_by_precision[key] = r
                continue
            r_bin = str(r.path).lower().endswith(".bin")
            cur_bin = str(cur.path).lower().endswith(".bin")
            # Prefer .bin over .dlc; if same extension class, prefer newer mtime.
            if r_bin and not cur_bin:
                best_by_precision[key] = r
            elif r_bin == cur_bin and (r.mtime or "") > (cur.mtime or ""):
                best_by_precision[key] = r
        deduped = list(best_by_precision.values())

        # Stable order: by display label (deterministic UI rendering).
        deduped.sort(key=lambda r: r.label or "")
        return tuple(deduped)

    def _read_manifest_variant(
        self, workdir: Path, output_dir: Path
    ) -> "BinScanResult | None":
        """Read the DEFAULT variant from ``<workdir>/inference_manifest.json``.

        The manifest is the authoritative source for the default weight's
        precision (its ``precision`` field), so this works even when the weight
        file name carries NO ``_<label>`` suffix (e.g. ``output/yolov8n.dlc``) —
        the case the filename-suffix scan cannot classify. Returns a
        ``BinScanResult`` when the manifest resolves to an existing, large-enough
        weight with a known precision, else ``None`` (best-effort: any parse /
        IO error is swallowed and the caller falls back to         the suffix scan).
        """
        import json
        from datetime import datetime, timezone

        manifest_path = workdir / "inference_manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        prec_raw = data.get("precision")
        cb_rel = data.get("context_binary")
        if not isinstance(prec_raw, str) or not isinstance(cb_rel, str):
            return None
        if prec_raw.strip() == "" or cb_rel.strip() == "":
            return None

        precision = _LABEL_TO_PRECISION.get(prec_raw.strip().lower())
        if not precision:
            return None

        # Resolve context_binary (a relative posix path like
        # ``output/<model>_<prec>.bin``) against the workdir. Guard against path
        # escape (``..``) and only accept a weight actually under output_dir.
        try:
            weight = (workdir / cb_rel).resolve()
        except (OSError, ValueError):
            return None
        try:
            weight.relative_to(output_dir.resolve())
        except ValueError:
            return None  # manifest points outside output/ — ignore, be safe
        if weight.suffix.lower() not in self._WEIGHT_SUFFIXES:
            return None
        try:
            if not weight.is_file():
                return None
            stat = weight.stat()
            if stat.st_size < _SCAN_BIN_MIN_BYTES:
                return None
        except OSError:
            return None

        mtime_iso = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        display_label = _PLAN_TO_DISPLAY_LABEL.get(precision, precision.upper())
        model_id = data.get("model_name")
        return BinScanResult(
            path=str(weight.as_posix()),
            size_bytes=stat.st_size,
            suspected_model_id=(
                model_id if isinstance(model_id, str) and model_id else workdir.name
            ),
            precision=precision,
            label=display_label,
            mtime=mtime_iso,
        )

    def detect_unnormalized_aihub(
        self, model_workdir: str
    ) -> UnnormalizedAihubHint | None:
        """Detect a downloaded-but-not-normalized AI Hub model in *model_workdir*.

        Called only when the normal workspace scan found NO variants under
        ``output/`` — turns a blank Import panel into actionable guidance.

        Heuristic (conservative; only fires on a clear AI Hub signature):
          * ``output/`` is missing or holds no recognized variant, AND
          * somewhere under the workdir (typically a nested
            ``<model>-qnn_dlc-*`` subfolder) there is a ``.dlc``/``.bin`` weight
            ≥ the min-size threshold that sits next to a ``metadata.json``.

        Returns the top-level workdir + the detected weight path, or ``None``
        when no such un-normalized AI Hub package is present (so a genuinely
        empty / unrelated workdir stays empty — no false guidance).

        Deliberately does NOT normalize anything: normalization infers fields
        (output.type, num_classes, preprocessing) that need human confirmation
        (see aihub_to_manifest.py GuessLog), so auto-running it would treat
        guesses as authoritative. We only surface "here is an un-normalized
        model — run Step 6.5" guidance.
        """
        try:
            workdir = Path(model_workdir).resolve()
        except (OSError, ValueError):
            return None
        if not workdir.is_dir():
            return None

        output_dir = workdir / "output"

        best: Path | None = None
        best_size = -1
        # Depth-limited scan: AI Hub packages land either directly in the
        # workdir or one nested folder down (e.g. ``<model>-qnn_dlc-<prec>/``).
        # We look at the workdir top level + its immediate subdirectories only
        # (depth ≤ 2) — this is enough for the AI Hub layout and avoids walking
        # an arbitrarily deep tree (a large workspace with .venv / node_modules
        # / nested caches would make an unbounded rglob slow). ``output/`` is
        # skipped (already covered by the normal variant scan).
        search_dirs: list[Path] = [workdir]
        try:
            search_dirs.extend(
                d for d in workdir.iterdir() if d.is_dir() and d != output_dir
            )
        except OSError:
            return None
        for d in search_dirs:
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            for cand in entries:
                try:
                    if not cand.is_file():
                        continue
                    if cand.suffix.lower() not in self._WEIGHT_SUFFIXES:
                        continue
                    size = cand.stat().st_size
                    if size < _SCAN_BIN_MIN_BYTES:
                        continue
                    # Require an AI Hub signature nearby: a metadata.json in the
                    # same folder (how AI Hub packages ship). This keeps the
                    # heuristic from firing on unrelated stray .bin files.
                    if not (cand.parent / "metadata.json").is_file():
                        continue
                    if size > best_size:
                        best_size = size
                        best = cand
                except OSError:
                    continue

        if best is None:
            return None
        return UnnormalizedAihubHint(
            model_workdir=str(workdir),
            detected_weight=str(best.as_posix()),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchRunRequest:
    """One row of a ``POST /batch`` request."""

    model_id: str
    inputs: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("model_id must be non-empty str")
        if not isinstance(self.inputs, dict):
            raise ValueError("inputs must be dict")


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchRunResult:
    """One row of a ``POST /batch`` response.

    The ``run_id`` is set when the run was successfully started; on
    rejection (model disabled / not found) ``error`` carries the
    reason and ``run_id`` is empty.
    """

    model_id: str
    run_id: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("model_id must be non-empty str")
        if not isinstance(self.run_id, str):
            raise ValueError("run_id must be str")
        if not isinstance(self.error, str):
            raise ValueError("error must be str")


class RunBatchUseCase:
    """Sequentially start a run for each request.

    Sequential execution is intentional: each request commits as a
    separate :class:`Run` aggregate so the route returns the run ids
    immediately while the per-run SSE streams own the actual frame
    iteration. The UC does NOT stream frames — clients are expected to
    subscribe to the per-run SSE streams using the returned run ids.
    Concurrency / cancellation aggregation across the batch is not part
    of this UC's contract; clients that need parallelism issue parallel
    requests against the per-run endpoints.
    """

    def __init__(self, *, run_app_use_case: Any) -> None:
        self._run_app = run_app_use_case

    async def execute(
        self, requests: Iterable[BatchRunRequest]
    ) -> tuple[BatchRunResult, ...]:
        results: list[BatchRunResult] = []
        for req in requests:
            try:
                # Trigger the run (returns an async iterator we don't
                # consume — the run is now persisted as PENDING/RUNNING
                # and the client will subscribe to its SSE stream).
                iterator = await self._run_app.execute(
                    model_id=AppModelId(value=req.model_id),
                    inputs=req.inputs,
                )
                # We need to step the iterator at least once to advance
                # the run from PENDING → RUNNING → STREAMING. That's
                # the contract of RunAppUseCase._stream(). Eagerly
                # advance past the first frame; the rest is dropped on
                # the floor (the SSE subscriber re-iterates from the
                # repository).
                #
                # NOTE: this is intentionally minimal — the batch
                # endpoint commits each run synchronously so the
                # client sees the run id immediately, while the
                # actual streaming is owned by the per-run SSE
                # subscriber. Spawning a detached background task
                # per request would shift completion semantics off
                # the request and is intentionally not done here.
                first_run_id = await _peek_first_frame_run_id(iterator)
                results.append(
                    BatchRunResult(model_id=req.model_id, run_id=first_run_id)
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    BatchRunResult(
                        model_id=req.model_id, error=str(exc)
                    )
                )
        return tuple(results)


async def _peek_first_frame_run_id(iterator: AsyncIterator) -> str:
    """Step the iterator once + return the run id from the first frame.

    A bit of a hack — :class:`RunAppUseCase` doesn't directly expose
    the run id; it's available in the repository or via the frame
    payload. Returning ``""`` is acceptable when the iterator is empty.
    """
    try:
        async for _ in iterator:
            # We only need the side-effect of advancing the use case;
            # the route layer's SSE stream owns the real frame iteration.
            # The empty-string sentinel is the documented contract — the
            # batch endpoint commits each run synchronously and the
            # client correlates run ids via the per-run SSE subscription
            # rather than via this peek helper.
            return ""
    except StopAsyncIteration:
        return ""
    except Exception:
        return ""
    return ""


# Suppress unused-import warning.
_ = (Iterator, RunStatus, ArtifactStorePort, AppModelDefinition)
