# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Abstract ports for the App Builder application layer.

All external dependencies of the use cases live behind ``Protocol``
classes defined here. Adapters in ``src/qai/app_builder/adapters/`` (to
be added by PR-040+) implement these protocols; tests substitute Fake
implementations.

Design rules (S2 spec §4):

* Every port is :class:`typing.Protocol`, ``@runtime_checkable`` where
  cheap.
* No port references concrete adapters or framework types
  (``fastapi.UploadFile``, ``aiosqlite.Connection`` etc.). Inputs are
  described in pure Python (``bytes``, ``AsyncIterator[bytes]``,
  primitive VOs).
* Repositories define **abstract CRUD only**; PR-026 will pick the
  storage backend (qai.db tables) and implement them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.app_project import (
    AppProjectDefinition,
    AppProjectRunInfo,
)
from qai.app_builder.domain.artifact import Artifact, ArtifactKind
from qai.app_builder.domain.feedback import Feedback
from qai.app_builder.domain.import_plan import CommitId, ImportPlan
from qai.app_builder.domain.run import Run, RunFrame
from qai.app_builder.domain.share import Share, ShareToken
from qai.app_builder.domain.value_objects import AppModelId, RunId
from qai.app_builder.domain.voice_preference import VoiceInputPreference
from qai.app_builder.domain.weight_download_config import WeightDownloadConfig

__all__ = [
    "AppModelRepositoryPort",
    "RunRepositoryPort",
    "RunnerPort",
    "ArtifactStorePort",
    "ArtifactBlobReaderPort",
    "AudioUploadPort",
    "VoiceInputPreferenceRepositoryPort",
    "ImportPort",
    "ShareRepositoryPort",
    "WorkerStatusPort",
    "WorkerPoolStatus",
    "LoadedModelInfo",
    "WorkerLifecycleState",
    "FeedbackRepositoryPort",
    "BenchmarkRepositoryPort",
    "BenchmarkRecord",
    "BenchmarkRecordStatus",
    "RunMarkdownRendererPort",
    "DepCheckerPort",
    "ResultCachePort",
    "WeightsPresencePort",
    "PackPresencePort",
    "PackFileCleanupPort",
    "VariantDeleteResult",
    "RunCancellationPort",
    "WeightDownloadConfigPort",
    "AppProjectRepositoryPort",
    "AppProjectProcessPort",
    "AppProjectPackagerPort",
    "AppModelManifestProviderPort",
    "RunnerRegistryPort",
    "WorkerHostPort",
    "PackRemovedCallback",
]


# ---------------------------------------------------------------------------
# AppModelDefinition repository
# ---------------------------------------------------------------------------
@runtime_checkable
class AppModelRepositoryPort(Protocol):
    """CRUD for :class:`AppModelDefinition` entries.

    Persistence is left to PR-026. Adapter implementations may store
    these in ``data/qai.db`` (table ``app_builder_models``) or — for
    truly static defaults — load from
    ``config/app_builder_models.toml``.
    """

    async def list_all(self) -> tuple[AppModelDefinition, ...]:
        """Return all registered definitions, in registry order."""
        ...

    async def get(self, model_id: AppModelId) -> AppModelDefinition:
        """Return the definition with this id.

        Raises :class:`qai.app_builder.domain.errors.AppModelNotFoundError`
        if no entry exists.
        """
        ...

    async def delete(self, model_id: AppModelId) -> None:
        """Remove a definition.

        Raises :class:`qai.app_builder.domain.errors.AppModelNotFoundError`
        if no entry exists.
        """
        ...


# ---------------------------------------------------------------------------
# Run repository
# ---------------------------------------------------------------------------
@runtime_checkable
class RunRepositoryPort(Protocol):
    """CRUD for :class:`Run` aggregates.

    The repository also owns the equivalent of the legacy
    ``data/appbuilder/last_results.json`` cache; specifically
    :meth:`get_last_for_model` returns the most recent terminal run for
    a given :class:`AppModelId`, used by the ``last_results`` UI panel.
    """

    async def save(self, run: Run) -> None:
        """Insert or update a run (key = ``run.id``)."""
        ...

    async def get(self, run_id: RunId) -> Run:
        """Return the run with this id.

        Raises :class:`qai.app_builder.domain.errors.RunNotFoundError`.
        """
        ...

    async def list_by_model(
        self, model_id: AppModelId, *, limit: int = 50
    ) -> tuple[Run, ...]:
        """Return up to ``limit`` runs for ``model_id`` (newest first)."""
        ...

    async def list_active_by_model(
        self, model_id: AppModelId
    ) -> tuple[Run, ...]:
        """Return every non-terminal run for ``model_id``.

        Non-terminal = status in ``{pending, running, streaming}`` (see
        :class:`qai.app_builder.domain.run.RunStatus.is_terminal`). Terminal
        rows (``completed`` / ``failed`` / ``cancelled``) are excluded.

        Backs the P3 "active-run protection" path in
        :class:`~qai.app_builder.application.use_cases.delete_app_model.DeleteAppModelUseCase`:
        before deleting a model we look up in-flight runs so the NPU can be
        released (via :class:`RunCancellationPort`) *before* pack files are
        yanked from under the running worker (§🔴 State-Truth-First — pull
        the cord before killing the socket, not after).

        Idempotent / read-only: returns an empty tuple when nothing is
        active (or when the model has never run). Order is unspecified —
        callers cancel every entry so ordering has no effect on behaviour.
        """
        ...

    async def get_last_for_model(self, model_id: AppModelId) -> Run | None:
        """Return the most recent terminal run for ``model_id`` or ``None``."""
        ...

    async def delete(self, run_id: RunId) -> None:
        """Remove a run aggregate (header + artifact rows + cascades).

        Tail-appended in S9 close to back the
        ``DELETE /api/app-builder/history/runs/{run_id}`` route.
        Adapters MUST cascade-delete any rows that reference the run
        (artifacts, share tokens, feedback, benchmarks) so the operation
        is idempotent against repeated invocation.

        Raises :class:`qai.app_builder.domain.errors.RunNotFoundError`
        when ``run_id`` is unknown — the route layer surfaces that as
        404 instead of silently returning success.
        """
        ...

    async def reconcile_stale_runs(self) -> int:
        """Mark non-terminal runs as FAILED (startup orphan sweep).

        After an unclean API-process exit (crash / kill / power loss) the
        DB can hold runs stuck in PENDING / RUNNING / STREAMING whose
        driving drainer task no longer exists — there is no live process
        that will ever transition them to a terminal state, so the history
        list would show them "running" forever (a State-Truth-First
        violation: the persisted state diverges from reality). This sweep,
        run once at startup before serving traffic, transitions every such
        orphan to FAILED with a clear ``error_message``/``error_code`` so the
        UI shows an honest terminal state.

        Idempotent: a second call (no non-terminal rows) is a no-op.
        Returns the number of rows reconciled.
        """
        ...


# ---------------------------------------------------------------------------
# Runner — produces streaming output frames + artifacts
# ---------------------------------------------------------------------------
@runtime_checkable
class RunnerPort(Protocol):
    """Adapter that actually executes a model run.

    Implementations:

    * ``adapters/runners/python_script`` — invokes a per-model Python
      script under a sandbox.
    * ``adapters/runners/sticky_worker`` — long-running worker pool.
    * ``adapters/runners/browser`` — browser-side WebGPU runners.

    Inventory note (``03-imports-dependencies.md`` SCC #3): the legacy
    ``runner ↔ python_script`` cycle is broken by funneling every
    runner through *this* port; concrete adapters do not import each
    other.

    The method is **async generator-shaped**: returns an
    :class:`AsyncIterator[RunFrame]`. The runner is also responsible
    for writing artifacts via the :class:`ArtifactStorePort` (passed in
    by the use case), not via the return value, so the streaming
    contract stays simple.
    """

    def execute(
        self,
        run: Run,
        model: AppModelDefinition,
        *,
        artifact_store: ArtifactStorePort,
    ) -> AsyncIterator[RunFrame]:
        """Begin execution; yield :class:`RunFrame` chunks.

        On error, the runner raises a domain error (typically
        :class:`qai.app_builder.domain.errors.ArtifactWriteError` or a
        :class:`qai.platform.errors.InfrastructureError` subclass). It
        MUST NOT swallow exceptions silently.
        """
        ...


# ---------------------------------------------------------------------------
# Run cancellation — terminate the in-flight inference on the worker
# ---------------------------------------------------------------------------
@runtime_checkable
class RunCancellationPort(Protocol):
    """Stop the actual inference work for an in-flight run.

    V1 parity: ``POST /api/appbuilder/cancel/{runId}`` terminated the runner
    subprocess so the NPU was released immediately. The DB-state flip alone
    (``CancelRunUseCase``) does NOT stop the worker — without this port a
    "cancelled" run keeps running to completion on the NPU, so the UI's
    "已取消" diverges from the real hardware state (violates State-Truth-First,
    §🔴 铁律 1/2/3).

    The sticky-worker adapter sends ``op:cancel`` to the resident worker
    (``StickyWorkerHost.cancel_run``); a lean test container may wire a no-op.
    Best-effort: a dead/absent worker is a silent no-op (nothing to stop).
    """

    async def cancel_run(self, run_id: str) -> None:
        """Signal the worker to abort the inference for ``run_id``."""
        ...


# ---------------------------------------------------------------------------
# Artifact store — writes output bytes to ``DataPaths``-rooted blobs
# ---------------------------------------------------------------------------
@runtime_checkable
class ArtifactStorePort(Protocol):
    """Persist run-produced bytes to the project blob store.

    Adapters resolve relative paths through
    :class:`qai.platform.config.DataPaths` (``blobs_dir``); the
    domain/use-case layer never touches the filesystem directly.
    """

    async def write(
        self,
        *,
        run_id: RunId,
        relative_path: str,
        kind: ArtifactKind,
        data: bytes,
    ) -> Artifact:
        """Write ``data`` and return the resulting :class:`Artifact`."""
        ...

    async def write_stream(
        self,
        *,
        run_id: RunId,
        relative_path: str,
        kind: ArtifactKind,
        data: AsyncIterator[bytes],
    ) -> Artifact:
        """Stream-write large outputs (e.g. long audio rendering)."""
        ...


# ---------------------------------------------------------------------------
# Audio upload (kept separate from generic artifact writing)
# ---------------------------------------------------------------------------
@runtime_checkable
class AudioUploadPort(Protocol):
    """Persist a user-uploaded audio file under ``DataPaths.upload_dir``.

    Kept distinct from :class:`ArtifactStorePort` because uploads have
    distinct lifecycle (user-owned, retained beyond a single run) and
    quota/safety constraints; the legacy backend exposes them on
    ``POST /api/appbuilder/upload/audio``.
    """

    async def save(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Artifact:
        """Validate and persist; return an :class:`Artifact` describing the file."""
        ...


# ---------------------------------------------------------------------------
# Voice preference repository
# ---------------------------------------------------------------------------
@runtime_checkable
class VoiceInputPreferenceRepositoryPort(Protocol):
    """Single-tenant voice preference storage.

    Backed in PR-026 by either ``data/user_config.toml`` or a row in
    the ``prefs`` table — see ``05-data-config.md`` line 278.
    """

    async def get(self) -> VoiceInputPreference:
        """Return the current preference or the documented default."""
        ...

    async def set(self, pref: VoiceInputPreference) -> None:
        """Replace the stored preference."""
        ...


# ---------------------------------------------------------------------------
# Import (dry-run / commit / rollback)
# ---------------------------------------------------------------------------
@runtime_checkable
class ImportPort(Protocol):
    """Three-state app model import workflow.

    ``dry_run`` is read-only; ``commit`` mutates the registry and
    returns a :class:`CommitId`; ``rollback`` reverts a prior commit.
    The adapter is responsible for atomicity (e.g. wrapping ``commit``
    in a SQLite transaction).
    """

    async def dry_run(self, candidates: Iterable[str]) -> ImportPlan:
        """Inspect ``candidates`` (opaque source ids) and return a plan."""
        ...

    async def commit(self, plan: ImportPlan) -> CommitId:
        """Execute ``plan``; return the resulting :class:`CommitId`."""
        ...

    async def rollback(self, commit_id: CommitId) -> None:
        """Undo a prior commit.

        Raises :class:`qai.app_builder.domain.errors.ImportConflictError`
        if the commit is unknown or already rolled back.
        """
        ...


# ---------------------------------------------------------------------------
# Artifact blob reader — streaming read of bytes previously written by the runner
# ---------------------------------------------------------------------------
@runtime_checkable
class ArtifactBlobReaderPort(Protocol):
    """Stream the bytes of an artifact previously persisted by a Run.

    Companion to :class:`ArtifactStorePort` (which only models writes).
    Promoted into the application layer in PR-045 so the route layer
    no longer depends on an apps-level shim — see
    ``apps/api/app_builder_ports.py`` for the now-deprecated S3 home.

    Implementations MUST stream chunks (no whole-file buffering) so the
    HTTP layer can pipe them directly to a ``StreamingResponse``.
    """

    def open(
        self,
        *,
        run_id: str,
        relative_path: str,
    ) -> AsyncIterator[bytes]:
        """Return an async iterator of byte chunks for the given artifact.

        The implementation MUST raise
        :class:`qai.app_builder.domain.errors.ArtifactWriteError` (or a
        :class:`qai.platform.errors.NotFoundError`) when the artifact
        cannot be located.
        """
        ...


# ---------------------------------------------------------------------------
# Share repository (run-link tokens)
# ---------------------------------------------------------------------------
@runtime_checkable
class ShareRepositoryPort(Protocol):
    """CRUD for :class:`Share` rows backing the public share-link feature.

    Schema reference: ``qai-db-schema.md`` §3.4 (``app_builder_share``).
    """

    async def save(self, share: Share) -> None:
        """Insert or update a share row (key = ``share.token``)."""
        ...

    async def get_by_token(self, token: ShareToken) -> Share:
        """Return the share for a given token.

        Raises :class:`qai.app_builder.domain.errors.ShareNotFoundError`
        when the token is unknown.
        """
        ...

    async def list_for_run(self, run_id: RunId) -> tuple[Share, ...]:
        """Return all shares (active + revoked) created for ``run_id``."""
        ...


# ---------------------------------------------------------------------------
# Worker status (read-only)
# ---------------------------------------------------------------------------
@runtime_checkable
class WorkerStatusPort(Protocol):
    """Report the current state of the runner worker pool.

    Surfaced by ``GET /api/appbuilder/worker/status`` in the legacy
    backend (``02-routes.md`` line 161).
    """

    async def status(self) -> WorkerPoolStatus:
        """Return a snapshot of the pool."""
        ...


from dataclasses import dataclass, field
from typing import Literal


WorkerLifecycleState = Literal[
    "idle",
    "loading",
    "ready",
    "busy",
    "shutting_down",
    "dead",
    "absent",
]
"""Lifecycle states reported by :class:`WorkerStatusPort`.

Mirrors the legacy ``backend/app_builder/runners/sticky_worker.py``
``StickyWorker.state`` literal plus an ``"absent"`` value used when no
worker has been spawned yet (so the route layer can return a uniform
shape regardless of whether warm-up has happened).
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadedModelInfo:
    """Per-model registry entry held by a sticky worker pool.

    Mirrors the SSOT shape documented in
    ``docs/30-ui-ux/voice-input-and-sticky-worker-multimodel.md`` §1.2 /
    §9.1 (``loadedModels[]`` field of ``GET /worker/status``):

    ``{ modelId, variantId, lastUsedAt, ageS, state }``

    The DTO is **transport-shape**, not a domain VO; see
    :class:`WorkerPoolStatus` for the rationale (placed in ``ports.py``
    rather than ``domain/`` because no domain behaviour depends on it).

    Fields:

    * :attr:`model_id` — registry key from the legacy multi-model map
      (``backend/app_builder/runners/sticky_worker.py`` ``loaded_models``
      keys); always equals an :class:`AppModelDefinition`'s id string.
    * :attr:`variant_id` — opaque variant identifier (multi-variant
      pack contract; PR-303 surfaces the full variant DTO). ``None``
      when the pack has no variants list.
    * :attr:`last_used_at` — Unix-epoch float timestamp of the most
      recent ``op:load`` or ``op:run`` for this model.
    * :attr:`age_seconds` — derived snapshot of ``now - last_used_at``;
      a stable view of "how idle is this entry?" for the UI without
      forcing the consumer to do clock arithmetic.
    * :attr:`state` — per-model lifecycle (``"loading" | "ready" |
      "busy"``); the worker-level ``WorkerPoolStatus.state`` covers
      ``"idle" | "shutting_down" | "dead" | "absent"`` cases.
    """

    model_id: str
    variant_id: str | None
    last_used_at: float
    age_seconds: float
    state: Literal["loading", "ready", "busy"]

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty string")
        if self.variant_id is not None and not isinstance(self.variant_id, str):
            raise ValueError("variant_id must be str or None")
        if (
            not isinstance(self.last_used_at, (int, float))
            or isinstance(self.last_used_at, bool)
        ):
            raise ValueError("last_used_at must be a float (epoch seconds)")
        if (
            not isinstance(self.age_seconds, (int, float))
            or isinstance(self.age_seconds, bool)
        ):
            raise ValueError("age_seconds must be a float")
        if self.age_seconds < 0:
            raise ValueError(
                f"age_seconds must be >= 0, got {self.age_seconds}"
            )
        if self.state not in ("loading", "ready", "busy"):
            raise ValueError(
                "LoadedModelInfo.state must be one of "
                "{'loading','ready','busy'}, "
                f"got {self.state!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerPoolStatus:
    """Plain DTO returned by :class:`WorkerStatusPort.status`.

    Defined inside ``ports.py`` (rather than ``domain/``) because it is
    a transport-shape detail of the worker adapter; the domain layer
    has no behaviour that depends on it.

    PR-034 / PR-045 lock the original three fields verbatim
    (``total_workers``, ``busy_workers``, ``queued_runs``); PR-301
    appends new fields at the tail (per v2.7 §3.1 "field-name lock —
    only tail-append additions allowed") to surface the full SSOT
    shape from
    ``docs/30-ui-ux/voice-input-and-sticky-worker-multimodel.md`` §9.1.
    """

    # PR-034 / PR-045 fields — locked, do not rename / remove.
    total_workers: int
    busy_workers: int
    queued_runs: int
    # PR-301 tail-append fields ----------------------------------------
    alive: bool = True
    state: WorkerLifecycleState = "ready"
    active_model_id: str | None = None
    multimodel: bool = False
    loaded_models: tuple[LoadedModelInfo, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name, value in (
            ("total_workers", self.total_workers),
            ("busy_workers", self.busy_workers),
            ("queued_runs", self.queued_runs),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"WorkerPoolStatus.{name} must be int")
            if value < 0:
                raise ValueError(
                    f"WorkerPoolStatus.{name} must be >= 0, got {value}"
                )
        if self.busy_workers > self.total_workers:
            raise ValueError(
                "busy_workers cannot exceed total_workers "
                f"({self.busy_workers} > {self.total_workers})"
            )
        if not isinstance(self.alive, bool):
            raise ValueError("alive must be bool")
        if self.state not in (
            "idle",
            "loading",
            "ready",
            "busy",
            "shutting_down",
            "dead",
            "absent",
        ):
            raise ValueError(
                f"state must be a WorkerLifecycleState, got {self.state!r}"
            )
        if self.active_model_id is not None and (
            not isinstance(self.active_model_id, str)
            or not self.active_model_id
        ):
            raise ValueError("active_model_id must be a non-empty str or None")
        if not isinstance(self.multimodel, bool):
            raise ValueError("multimodel must be bool")
        if not isinstance(self.loaded_models, tuple):
            raise ValueError("loaded_models must be a tuple")
        for i, info in enumerate(self.loaded_models):
            if not isinstance(info, LoadedModelInfo):
                raise ValueError(
                    f"loaded_models[{i}] must be LoadedModelInfo, "
                    f"got {type(info).__name__}"
                )
        # Cross-field consistency: ``alive=False`` should pair with a
        # terminal-ish state and an empty registry — guarantees consumers
        # never see "alive=False but loaded_models still populated".
        if not self.alive:
            if self.state not in ("dead", "absent", "shutting_down"):
                raise ValueError(
                    "alive=False requires state ∈ "
                    "{'dead','absent','shutting_down'}, "
                    f"got {self.state!r}"
                )
            if self.loaded_models:
                raise ValueError(
                    "alive=False requires an empty loaded_models tuple"
                )


# ---------------------------------------------------------------------------
# Feedback repository (S9 close — wires ``POST /feedback`` to persistence)
# ---------------------------------------------------------------------------
@runtime_checkable
class FeedbackRepositoryPort(Protocol):
    """CRUD for :class:`Feedback` rows.

    Schema reference: ``qai-db-schema.md`` §3.8 (``app_builder_feedback``,
    delivered by migration 011). One run can accumulate multiple
    feedback rows over time; ``list_for_run`` returns them ordered
    newest-first so :class:`InjectQualityScoreUseCase` can derive the
    latest signal cheaply.
    """

    async def save(self, feedback: Feedback) -> None:
        """Insert a feedback row.

        The ``feedback.id`` is the primary key; collisions are surfaced
        as :class:`qai.platform.errors.PersistenceError`.
        """
        ...

    async def list_for_run(self, run_id: RunId) -> tuple[Feedback, ...]:
        """Return every feedback row for ``run_id``, newest first.

        Returns an empty tuple when no rows exist (the caller decides
        whether that is informational or an error).
        """
        ...

    async def latest_ratings_for_runs(
        self, run_ids: Iterable[RunId]
    ) -> dict[str, int]:
        """Return ``{run_id_str: latest_rating}`` for the given run ids.

        Batch accessor consumed by the run-history surface (``GET /runs``
        / ``GET /runs/{id}``) so the rating an operator submitted can be
        echoed back next to each historical run without an N+1 fan-out
        (one query covers the whole page rather than one per run).

        Only runs that have at least one feedback row appear in the
        returned mapping; runs without feedback are simply absent (the
        caller treats "absent" as "unrated"). The value is the latest
        feedback's Likert ``1..5`` rating.
        """
        ...


# ---------------------------------------------------------------------------
# Benchmark repository (S9 close — wires ``POST /benchmark`` to persistence)
# ---------------------------------------------------------------------------
BenchmarkRecordStatus = Literal[
    "scheduled",
    "running",
    "completed",
    "failed",
]
"""Lifecycle states reported by :class:`BenchmarkRecord`.

A benchmark row enters ``"scheduled"`` when the route handler persists
it; the harness flips it to ``"running"`` before the first iteration
and finally to ``"completed"`` (with ``stats``) or ``"failed"`` (with
``error_message``).
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkRecord:
    """One row of the ``app_builder_benchmark`` table.

    Captures the inputs + aggregate latency stats for a single
    benchmark invocation; the per-iteration raw latencies are stored as
    a JSON array on the row so post-hoc analysis (p99 vs mean drift,
    warmup effect) keeps working without rebuilding the harness.

    Status transitions:

    * ``"scheduled"`` — row created by the route handler before the
      harness starts;
    * ``"running"`` — harness picked the row up and is iterating;
    * ``"completed"`` — terminal success, ``stats`` populated;
    * ``"failed"`` — terminal failure, ``error_message`` populated.

    All datetimes MUST be tz-aware.
    """

    id: str
    model_id: AppModelId
    iterations: int
    warmup: int
    inputs: dict[str, object] = field(default_factory=dict)
    status: str = "scheduled"
    stats: dict[str, float] = field(default_factory=dict)
    raw_latencies_ms: tuple[float, ...] = field(default_factory=tuple)
    error_message: str | None = None
    created_at: datetime
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("BenchmarkRecord.id must be a non-empty str")
        if (
            not isinstance(self.iterations, int)
            or isinstance(self.iterations, bool)
            or self.iterations < 1
        ):
            raise ValueError("BenchmarkRecord.iterations must be int >= 1")
        if (
            not isinstance(self.warmup, int)
            or isinstance(self.warmup, bool)
            or self.warmup < 0
        ):
            raise ValueError("BenchmarkRecord.warmup must be int >= 0")
        if not isinstance(self.inputs, dict):
            raise ValueError("BenchmarkRecord.inputs must be dict")
        if self.status not in ("scheduled", "running", "completed", "failed"):
            raise ValueError(
                "BenchmarkRecord.status must be one of "
                "{'scheduled','running','completed','failed'}, "
                f"got {self.status!r}"
            )
        if not isinstance(self.stats, dict):
            raise ValueError("BenchmarkRecord.stats must be dict")
        if not isinstance(self.raw_latencies_ms, tuple):
            raise ValueError("BenchmarkRecord.raw_latencies_ms must be tuple")
        for v in self.raw_latencies_ms:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError("raw_latencies_ms entries must be numeric")
        if self.created_at.tzinfo is None:
            raise ValueError("BenchmarkRecord.created_at must be tz-aware")
        if self.finished_at is not None and self.finished_at.tzinfo is None:
            raise ValueError("BenchmarkRecord.finished_at must be tz-aware")


@runtime_checkable
class BenchmarkRepositoryPort(Protocol):
    """CRUD for :class:`BenchmarkRecord` rows.

    Schema reference: ``qai-db-schema.md`` §3.9 (``app_builder_benchmark``,
    delivered by migration 012). The harness persists a row in
    ``"scheduled"`` state, transitions it to ``"running"`` when it
    actually starts, then writes terminal state + stats on completion.
    """

    async def save(self, record: BenchmarkRecord) -> None:
        """Insert or update a row keyed by ``record.id``."""
        ...

    async def get(self, benchmark_id: str) -> BenchmarkRecord:
        """Return the row with ``benchmark_id``.

        Raises :class:`qai.platform.errors.NotFoundError` when unknown.
        """
        ...


# ---------------------------------------------------------------------------
# Run Markdown renderer (S9 close — clean-arch Port for the MD export path)
# ---------------------------------------------------------------------------
@runtime_checkable
class RunMarkdownRendererPort(Protocol):
    """Render a :class:`Run` aggregate as a Markdown report string.

    Defined as a Port so the application layer (the
    :class:`ExportRunMarkdownUseCase` use case) does not import the
    concrete renderer in :mod:`qai.app_builder.infrastructure.run_exporter`
    directly. The ``layered-app_builder`` import-linter contract forbids
    ``application -> infrastructure``; the use case takes a Port and the
    DI root (``apps/api/_app_builder_di.py``) wires the concrete adapter.

    Implementations MUST be pure functions over the input :class:`Run`
    aggregate (no I/O, no clock injection beyond the documented
    "exported at" footer); they MUST NOT mutate the run.
    """

    def render(self, run: Run) -> str:
        """Return the Markdown report for ``run``."""
        ...


# ---------------------------------------------------------------------------
# Dynamic Pack dep checker (S9 close — clean-arch Port for the run-time
# dependency probe / install collaborator)
# ---------------------------------------------------------------------------
@runtime_checkable
class DepCheckerPort(Protocol):
    """Schedule a per-Pack dependency probe + auto-install.

    Defined as a Port so the application layer
    (:class:`RunAppUseCase`) does not import the concrete checker in
    :mod:`qai.app_builder.infrastructure.dep_checker` directly. The
    ``layered-app_builder`` import-linter contract forbids
    ``application -> infrastructure``; the use case takes the Port and
    the DI root wires the concrete adapter.

    The method is **synchronous** by contract: implementations may
    return a fire-and-forget asyncio Task handle (or ``None`` when the
    adapter is disabled / no event loop is running). The caller treats
    the return as opaque — the run-app pipeline never awaits the task
    so a slow / blocked install path cannot stall a model invocation.
    """

    def ensure(
        self, pack_id: str, deps: list[str]
    ) -> object | None:
        """Schedule a probe + install for ``pack_id`` against ``deps``."""
        ...


# ---------------------------------------------------------------------------
# Result cache (S9 close — clean-arch Port for the per-run LRU cache that
# short-circuits identical (model_id, variant_id, inputs, params) repeats)
# ---------------------------------------------------------------------------
@runtime_checkable
class ResultCachePort(Protocol):
    """LRU cache for ``appbuilder_run`` result payloads.

    Defined as a Port so the application layer
    (:class:`RunAppUseCase`) does not import the concrete cache in
    :mod:`qai.app_builder.infrastructure.result_cache` directly. The
    ``layered-app_builder`` import-linter contract forbids
    ``application -> infrastructure``; the use case takes the Port and
    the DI root wires the concrete adapter.

    Key construction (:meth:`make_key`) is part of the Port contract so
    the use case can derive a stable cache key without depending on the
    concrete adapter's hashing strategy. Values are opaque payloads —
    the use case writes a list of :class:`RunFrame` chunks and reads
    them back verbatim, so the cache stays decoupled from the run
    record schema.
    """

    @staticmethod
    def make_key(
        model_id: str,
        variant_id: str | None,
        inputs: "dict[str, object] | None",
        params: "dict[str, object] | None",
    ) -> str:
        """Return the stable SHA-256 cache key for the given quadruple."""
        ...

    async def get(self, run_id: str) -> object | None:
        """Return the cached payload for ``run_id`` or ``None`` on miss."""
        ...

    async def put(self, run_id: str, result: object) -> None:
        """Insert / replace the cache entry for ``run_id``."""
        ...


# ---------------------------------------------------------------------------
# Weights presence probe (Clean-arch Port for the App Builder gallery's
# install-status dot — the filesystem reads the domain MUST NOT do itself)
# ---------------------------------------------------------------------------
@runtime_checkable
class WeightsPresencePort(Protocol):
    """Probe whether a Pack's model weights are installed on disk.

    Hoisted out of the domain so the install-status decision logic
    (:func:`qai.app_builder.domain.model_status.detect_status`) stays a
    pure function: it asks *this* port the high-level questions ("is this
    install path present?", "is the pack's ``weights/`` dir present-yet-
    empty?") and never touches ``pathlib`` / the filesystem itself. The
    concrete adapter lives in
    :mod:`qai.app_builder.infrastructure.weights_presence` and owns the
    ``repo_root`` / ``pack_root`` joins + ``Path.resolve()/.exists()/
    .is_dir()/.iterdir()`` reads; the DI root
    (``apps/api/_app_builder_di.py``) wires it.

    The port's method shapes match the domain's structural
    :class:`qai.app_builder.domain.model_status.WeightsProbe` so a single
    adapter satisfies both (domain depends only on its own structural
    protocol, preserving ``domain ⇍ application``).

    Implementations MAY raise :class:`OSError` on a probe failure
    (permission / IO error); the domain catches it and maps to ``Error``.
    """

    def install_path_present(self, install_path: str) -> bool:
        """Whether ``install_path``'s weights exist on disk.

        ``install_path`` is the manifest's (possibly repo-root-relative)
        ``assets.installPath``; the adapter resolves it against the repo
        root before stat'ing (V1 ``(_repo_root / p).resolve().exists()``).
        """
        ...

    def pack_weights_dir_is_present_but_empty(self, pack_id: str) -> bool:
        """Whether ``<pack_root>/<pack_id>/weights/`` exists yet is empty.

        Mirrors the V1 legacy-pack fallback predicate exactly:
        ``weights_dir.is_dir() and not any(weights_dir.iterdir())`` — the
        sole condition that downgrades a legacy pack to ``NotInstalled``.
        An absent or non-empty dir both yield ``False``.
        """
        ...


# ---------------------------------------------------------------------------
# Pack presence probe (gallery list <-> on-disk pack invariant)
# ---------------------------------------------------------------------------
@runtime_checkable
class PackPresencePort(Protocol):
    """Probe whether a model's on-disk Pack still exists.

    V2 persists model definitions in the ``app_builder_model_definition``
    table, but V1's single source of truth was the on-disk pack directory
    (``features/app-builder/models/<id>/manifest.json``): deleting the pack
    made it vanish from the gallery, importing one made it appear. The DB
    registry must honour that same invariant — a row whose pack directory
    no longer exists on disk is an *orphan* and must NOT be listed (it would
    otherwise show as a phantom "Ready" model that cannot run).

    This port lets the list use case + the startup reconcile sweep ask the
    high-level question "does this pack still exist on disk?" without the
    application/domain layers touching ``pathlib`` (§🔴 State-Truth-First:
    disk is the truth, the DB row is only a cache).
    """

    def pack_dir_present(self, pack_id: str) -> bool:
        """Whether ``<pack_root>/<pack_id>/manifest.json`` exists on disk.

        Returns ``True`` only when the pack directory holds a readable
        ``manifest.json`` (the V1 ``_scan_packs`` admission test). An absent
        ``pack_root`` configuration yields ``True`` (fail-open: a lean
        container with no pack root must not hide every model).
        """
        ...


# ---------------------------------------------------------------------------
# Pack on-disk file cleanup (delete)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class VariantDeleteResult:
    """Outcome of a per-variant delete (V1 ``importer.delete_variants``).

    ``mode`` mirrors V1:

    * ``"partial"`` — the requested variants were removed and the manifest
      was rewritten with the survivors (a new default is promoted when the
      old default was among the deleted).
    * ``"would_be_empty"`` — removing the requested variants would leave the
      pack with zero variants; the caller MUST fall back to a full-pack
      delete instead (nothing was written).
    * ``"noop"`` — nothing matched (no ``variants[]`` or only unknown ids);
      the manifest is untouched.
    """

    mode: str
    deleted: tuple[str, ...]
    remaining: tuple[str, ...]
    new_default: str | None
    errors: tuple[str, ...] = ()


@runtime_checkable
class PackFileCleanupPort(Protocol):
    """Remove an imported pack's on-disk artifacts (V1 ``deleteFiles=true``).

    The import commit physically installs a pack under
    ``pack_root/<id>/`` (manifest / runner / weights tree) and stages each
    variant's weights ``.bin`` under the manifest ``installPath`` anchor
    (``repo_root/models/<id>/<bin>``). Deleting only the DB row would leak
    those files; this port deletes them so a delete is symmetric with an
    import (State-Truth-First: the disk reflects the registry).

    A lean test container that does not configure ``pack_root`` / ``repo_root``
    may wire a no-op implementation; the use case then degrades to DB-only
    delete (the legacy behaviour before this port existed).
    """

    def delete_pack_files(self, model_id: str) -> tuple[str, ...]:
        """Delete the entire pack (``pack_root/<id>`` + every variant's weights).

        Returns a tuple of non-fatal warning strings (e.g. an ``unlink``
        that failed); an empty tuple means a clean removal. Missing files are
        not errors (idempotent — V1 used ``ignore_errors`` / ``is_file``
        guards).
        """
        ...

    def delete_variant_files(
        self, model_id: str, variant_ids: tuple[str, ...]
    ) -> VariantDeleteResult:
        """Delete a subset of a pack's variants without touching the rest.

        For each requested variant: removes its weights ``.bin`` (both the
        ``repo_root/models/<id>/<bin>`` install copy and the
        ``pack_root/<id>/weights/<bin>`` staged copy) and drops its entry from
        ``manifest.variants[]``, then rewrites the manifest (promoting a new
        default + mirroring ``runtime`` / ``assets`` / ``metrics`` when the old
        default was deleted). See :class:`VariantDeleteResult` for the modes.
        """
        ...


# ---------------------------------------------------------------------------
# Weight-download config provider (per-Pack ``weights.json`` reader)
# ---------------------------------------------------------------------------
@runtime_checkable
class WeightDownloadConfigPort(Protocol):
    """Provide the per-Pack :class:`WeightDownloadConfig` for a model id.

    Backs the App Builder "download model weights" use case
    (:class:`qai.app_builder.application.use_cases.download_weights.DownloadModelWeightsUseCase`).
    The concrete adapter
    (:mod:`qai.app_builder.infrastructure.weight_download_config_reader`)
    reads ``repo_root/factory/app_builder/models/<id>/weights.json``, but
    the use case only ever sees this Port + the returned VO — no path /
    filesystem knowledge leaks into the application layer (satisfies the
    ``layered-app_builder`` contract).
    """

    def get(self, model_id: str) -> WeightDownloadConfig | None:
        """Return the model's weight-download config, or ``None``.

        Returns ``None`` when the pack has no ``weights.json`` or the file
        is absent / malformed (the caller maps that to a 404-able
        "no downloadable weights" signal rather than crashing).
        """
        ...


# ---------------------------------------------------------------------------
# Standalone fullstack app projects (data/app_builder/<app_id>/)
# ---------------------------------------------------------------------------
@runtime_checkable
class AppProjectRepositoryPort(Protocol):
    """Read-only access to generated standalone app projects on disk.

    Backs the App Builder "standalone fullstack app" feature: each app is
    a directory under ``data/app_builder/<app_id>/`` carrying an
    ``app.yaml`` the host treats as the single machine-readable entry
    point. The concrete adapter
    (:mod:`qai.app_builder.infrastructure.app_project_repository`) scans
    that root, parses ``app.yaml`` into
    :class:`~qai.app_builder.domain.app_project.AppProjectDefinition`, and
    enforces the ``^[a-z0-9][a-z0-9_-]{1,63}$`` id rule + path-traversal
    containment — the application layer only ever sees the Port + the VO
    (no filesystem knowledge leaks in; satisfies ``layered-app_builder``).
    """

    async def list_projects(self) -> list[AppProjectDefinition]:
        """Return every directory carrying a valid ``app.yaml``.

        Malformed / missing ``app.yaml`` directories are skipped (never
        raised), so a single bad app cannot blank the whole list. Ordered
        newest-first by directory mtime.
        """
        ...

    async def get_project(self, app_id: str) -> AppProjectDefinition:
        """Return one app project by id.

        Raises
        :class:`~qai.app_builder.domain.app_project.AppProjectNotFoundError`
        when the id is invalid / escapes the apps root / the directory is
        missing, and
        :class:`~qai.app_builder.domain.app_project.AppProjectInvalidError`
        when the directory exists but its ``app.yaml`` is missing / invalid.
        """
        ...

    async def delete_project(self, app_id: str) -> None:
        """Delete the app project directory (recursive).

        Path-traversal safe (same containment guard as ``get_project``,
        strictly under the apps root). Raises
        :class:`~qai.app_builder.domain.app_project.AppProjectNotFoundError`
        when the id is invalid / escapes the root / the dir is missing, and
        :class:`~qai.app_builder.domain.app_project.AppProjectDeleteFailedError`
        on an IO failure. Removes only the dev project under
        ``data/app_builder/``; packaged zips in the workspace are untouched.
        """
        ...


@runtime_checkable
class AppProjectProcessPort(Protocol):
    """Managed run lifecycle for a standalone app project.

    Backs ``POST|DELETE /api/app-builder/apps/{id}/run`` +
    ``GET /apps/{id}/logs``. The concrete adapter
    (:mod:`qai.app_builder.infrastructure.app_project_process_manager`)
    spawns the app's FastAPI process through the shared
    :class:`qai.platform.background_process` manager (PID / log ring
    buffer / Win32 Job Object reused, not re-implemented), allocates a
    bindable port via :mod:`qai.platform.net.port_allocator`, injects the
    QAIRT / ``PYTHONPATH`` / ``APP_ROOT`` runtime env, and waits for
    ``/health`` to answer twice consecutively before reporting ``ready``.
    All status is read back from the real process manager (never an
    optimistic cache — State-Truth-First).
    """

    async def run(
        self,
        definition: AppProjectDefinition,
        *,
        port: int | None,
    ) -> AppProjectRunInfo:
        """Start (or return the already-running) managed process.

        Same ``app_id`` may only have one managed process; a second call
        while running returns the current
        :class:`~qai.app_builder.domain.app_project.AppProjectRunInfo`
        rather than spawning again. Raises
        ``AppProjectPortInUseError`` (explicit port unbindable),
        ``AppProjectNoBindablePortError`` (auto pool exhausted), or
        ``AppProjectStartFailedError`` (spawn / readiness failure).
        """
        ...

    async def stop(self, app_id: str) -> AppProjectRunInfo:
        """Stop the managed process (kills the whole tree).

        Raises ``AppProjectNotRunningError`` when nothing is running for
        ``app_id``.
        """
        ...

    async def status(self, app_id: str) -> AppProjectRunInfo:
        """Return the current live run status (from the process manager).

        Returns a ``stopped`` info when no managed process exists.
        """
        ...

    async def logs(self, app_id: str) -> str:
        """Return the retained stdout/stderr tail of the managed process.

        Raises ``AppProjectNotRunningError`` when nothing is/was running.
        """
        ...


@runtime_checkable
class AppProjectPackagerPort(Protocol):
    """Package a standalone app project into a workspace-rooted ``.zip``.

    Backs ``POST /api/app-builder/apps/{id}/package`` + its SSE progress
    stream (plan §5.6 / §10). The concrete adapter
    (:mod:`qai.app_builder.infrastructure.app_project_packager`) walks the
    app dir under a strict include whitelist (backend/frontend/README/
    run.bat/requirements/app.yaml + helpers), copies the app's actual
    model/weight minimal set (expanding ``${APP_ROOT}`` to the install
    root) + pack manifest/weights/SKILL/assets, adds ``package_manifest.json``
    + ``RUNNING.md``, and excludes ``.venv`` / ``__pycache__`` / large logs /
    uploads / the package output dir itself. It never follows a symlink out
    of the app dir (§5.8). Output lands at
    ``<workspace>/app_builder_packages/<app_id>-<timestamp>.zip``.

    ``package`` is an async generator yielding progress snapshots (shaped
    ``phase`` / ``percent`` / ``message`` / ``zip_path`` / ``size_bytes`` /
    ``is_complete``) so the SSE route can stream a long weight copy; the
    final snapshot has ``is_complete=True`` + ``zip_path`` + ``size_bytes``.
    Raises
    :class:`~qai.app_builder.domain.app_project.AppProjectPackageFailedError`
    (``app_builder.package_failed``) on path-escape / IO failure.
    """

    def package(
        self, definition: AppProjectDefinition
    ) -> AsyncIterator[object]:
        """Yield packaging progress snapshots to completion."""
        ...


# ---------------------------------------------------------------------------
# Runtime cache invalidation on Pack removal (P2 — State-Truth-First 铁律 1)
# ---------------------------------------------------------------------------
# Problem: after ``DeleteAppModelUseCase`` removes a DB row + on-disk pack
# files, the following in-process caches keep stale entries for the removed
# model id, causing "phantom runnable" behaviour on the next request:
#
#   * ``_RefreshableManifestProvider._by_id[model_id]`` — manifest lookups
#     (``GET /models/{id}/manifest``, SKILL aggregation) still succeed.
#   * ``InMemoryRunnerCommandRegistry._specs[model_id]`` — the command
#     resolver still returns a launch spec so a subprocess is spawned even
#     though ``pack_root/<id>/runner.py`` is gone (⇒ ``FileNotFoundError``).
#   * ``StickyWorkerHost._loaded_models[model_id]`` — the resident NPU worker
#     still holds the model's Genie/QNN context; a subsequent ``op:run`` for
#     that id would either reuse the freed weights or hard-crash the worker
#     (native teardown mid-use, cf. §🔴 State-Truth-First 铁律 1).
#
# These three ports form the symmetric counterpart to the post-install
# refresh callback (``FileSystemAppImportAdapter.on_pack_installed``): they
# let the composition layer (``apps/api/_app_builder_di.py``) invalidate the
# three caches after a successful delete, in the same "runtime state now
# matches disk" spirit.
#
# All three ports MUST be idempotent (removing an already-absent entry is a
# silent no-op) so retry / replay scenarios stay safe.
# ---------------------------------------------------------------------------


@runtime_checkable
class AppModelManifestProviderPort(Protocol):
    """Runtime pack-manifest lookup with runtime remove.

    Backs the ``AppModelId -> PackManifest | None`` callable used by
    ``GET /models/{id}/manifest``, the Schema-driven UI, SKILL aggregation
    and the gallery status resolver. The concrete adapter is
    :class:`apps.api._app_builder_di._RefreshableManifestProvider`.

    This port only formalises the **remove** contract (Sub-A / P2 patch).
    The existing lookup call (``provider(model_id)``) and the ``add()``
    refresh hook already exist on the concrete adapter and remain
    duck-typed for backwards compatibility — a lean container that ships
    a plain callable provider (no ``remove``) continues to work; the DI
    root skips the invalidation with a warning.

    State-Truth-First semantics
    ---------------------------
    After :meth:`remove`, a subsequent lookup for ``model_id`` MUST return
    ``None`` (i.e. "no manifest for this model") so the UI treats the id
    as gone rather than as a phantom "Ready" pack.
    """

    def remove(self, model_id: AppModelId) -> None:
        """Drop the manifest entry for ``model_id`` (idempotent).

        Removing a model id that is not currently registered is a silent
        no-op — the caller (a delete flow) does not need to know whether
        the entry was ever added; only that "after this call, the cache
        is clean for this id".

        Implementations SHOULD log an info line each time an entry is
        actually removed so operators can trace runtime cache clears.
        """
        ...


@runtime_checkable
class RunnerRegistryPort(Protocol):
    """Runtime runner-command registry with runtime unregister.

    Backs the ``model.id -> RunnerSpec`` map consumed by the process
    runner's command resolver
    (:func:`qai.app_builder.infrastructure.command_resolver.registry.build_command_resolver`).
    The concrete adapter is
    :class:`qai.app_builder.infrastructure.command_resolver.registry.InMemoryRunnerCommandRegistry`.

    This port formalises only the **unregister** contract (Sub-A / P2
    patch); registration (``register``) already exists on the concrete
    adapter and is invoked from
    :func:`qai.app_builder.infrastructure.app_manifest.registry_bridge.populate_runner_registry_from_manifests`.

    State-Truth-First semantics
    ---------------------------
    After :meth:`unregister`, a subsequent ``get(model, run)`` for that
    ``model.id`` MUST return ``None`` so the command resolver falls back
    to the ``no_command`` path (rather than trying to spawn a runner from
    a stale spec that points at a now-deleted ``runner.py``).

    Note on naming: the concrete adapter historically exposes a
    ``deregister`` method (see :class:`InMemoryRunnerCommandRegistry`).
    Adapters MAY keep the legacy spelling; the DI root discovers the
    method by name via ``getattr(registry, "unregister", registry.deregister)``.
    """

    def unregister(self, model_id: str) -> None:
        """Drop the runner spec for ``model_id`` (idempotent).

        Removing an unregistered id is a silent no-op — the delete flow
        does not need to know whether the pack was ever registered
        (e.g. a user-imported pack whose commit failed leaves the DB
        row but no registry entry; deleting it must still succeed).
        """
        ...


@runtime_checkable
class WorkerHostPort(Protocol):
    """Sticky worker cache invalidation on model deletion.

    Backs the resident NPU worker pool
    (:class:`qai.app_builder.infrastructure.sticky_worker.host.StickyWorkerHost`)
    from the perspective of the delete flow. The concrete host has a much
    wider surface (``spawn`` / ``load`` / ``execute_run`` / ``cancel_run``
    / ``release_model`` / ``shutdown`` / ``ping`` / …); this port narrows
    it to the single operation the delete-flow needs.

    Why this is a separate port
    ---------------------------
    The delete flow does NOT need — and must not accidentally couple to —
    the runner / cancellation / status surfaces already covered by
    :class:`RunnerPort`, :class:`RunCancellationPort`, :class:`WorkerStatusPort`.
    Isolating the eviction contract keeps the ``DeleteAppModelUseCase``
    Clean-Architecture-happy: it holds a narrow port whose only method is
    "please forget this model id".

    State-Truth-First semantics (§🔴 铁律 1)
    ---------------------------------------
    After :meth:`evict`, the worker MUST no longer report ``model_id`` in
    its ``loaded_models_snapshot()`` and any Genie/QNN context it held
    for that model MUST be released. Implementations SHOULD reuse
    :meth:`StickyWorkerHost.release_model` (which serialises the
    ``op:release`` frame on the ``_op_lock`` so it cannot interleave with
    a mid-flight ``op:run`` and tear a native model down mid-use — the
    exact race that crashes the worker with ``0xFFFFFFFF``).

    A dead / absent worker is a silent no-op (nothing to evict).
    A worker that is currently mid-``op:run`` on ``model_id`` MUST NOT be
    force-killed: the eviction is best-effort; the model will be evicted
    on the next idle-release sweep once the run completes. The delete
    flow accepts this transient window because the DB row + pack files
    are already gone, so no *new* run for that id can start.
    """

    async def evict(self, model_id: str) -> None:
        """Release ``model_id`` from the resident worker (idempotent).

        Best-effort: an absent / dead worker, an unloaded id, or a
        currently-busy id all resolve to a silent no-op. Never raises
        so the delete flow's fire-and-forget invalidation cannot
        surface a spurious 5xx to the caller.
        """
        ...


PackRemovedCallback = Callable[[str], "None | Awaitable[None]"]
"""Composition-layer callback invoked after a Pack is deleted.

Symmetric counterpart to the ``on_pack_installed`` callback threaded
through :class:`FileSystemAppImportAdapter`. Receives the plain-string
``model_id`` of the just-deleted pack; MAY be either a synchronous
function or a coroutine function (the caller ``await``s the return when
inspection shows an awaitable — see the ``inspect.isawaitable`` shim in
``apps/api/_app_builder_di.py``).

The callback is invoked AFTER the DB row + on-disk files are removed
(so a runtime cache clear during the delete cannot re-populate itself
from a stale disk state); a failure inside the callback MUST NOT abort
the delete — the caller wraps invocation in a ``try/except`` and logs
a warning so the invalidation stays best-effort (State-Truth-First:
the persisted state is already clean; the caches will eventually
converge on the next full refresh even if this invocation fails).
"""




