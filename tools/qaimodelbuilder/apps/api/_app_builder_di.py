# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``app_builder`` bounded context (PR-034 + PR-045 + PR-301).

PR-034 (S3) injected eight ``_Fake<Port>`` adapters; PR-045 (S4)
replaced all eight with real adapters and added five missing use cases.
PR-301 (S7.5) introduces the sticky-worker subsystem:

* :class:`qai.app_builder.infrastructure.StickyWorkerHost` —
  long-running multi-model worker host (asyncio side);
* :class:`qai.app_builder.adapters.StickyWorkerStatusAdapter` —
  ``WorkerStatusPort`` implementation that reads from the host.

Lifespan integration (apps/api/lifespan.py is owned by the I1
integration lane; this PR's manifest §10 lists the warm-up hook).
``container.sticky_worker_host`` is optional; the
:class:`StickyWorkerStatusAdapter` reads through a lazy host provider so
the ``WorkerStatusPort`` reports ``alive=False`` until the host is wired.

Existing :class:`AppBuilderServices` field names (PR-034 §11 lock) are
preserved verbatim. PR-301 only **adds** the optional
``sticky_worker_host`` field at the tail of the dataclass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qai.app_builder.adapters import (
    SqliteAppModelRepository,
    SqliteBenchmarkRepository,
    SqliteFeedbackRepository,
    SqliteRunRepository,
    SqliteShareRepository,
    SqliteVoicePrefRepository,
    StickyWorkerStatusAdapter,
)
from qai.app_builder.application.model_status_view import (
    AppModelStatusInfo,
    VariantStatusView,
)
from qai.app_builder.application.ports import (
    ArtifactBlobReaderPort,
    BenchmarkRepositoryPort,
    FeedbackRepositoryPort,
    RunRepositoryPort,
    RunnerPort,
    ShareRepositoryPort,
    WorkerStatusPort,
)
from qai.app_builder.application.run_stream_broadcaster import (
    RunStreamBroadcaster,
)
from qai.app_builder.infrastructure.progress_broadcaster import (
    ProgressBroadcaster,
)
from qai.app_builder.application.use_cases.cancel_run import CancelRunUseCase
from qai.app_builder.application.use_cases.app_projects import (
    GetAppProjectUseCase,
    ListAppProjectsUseCase,
)
from qai.app_builder.application.use_cases.app_projects import (
    DeleteAppProjectUseCase,
    GetAppProjectLogsUseCase,
    GetAppProjectStatusUseCase,
    PackageAppProjectUseCase,
    RunAppProjectUseCase,
    StopAppProjectUseCase,
)
from qai.app_builder.application.use_cases.delete_app_model import (
    DeleteAppModelUseCase,
)
from qai.app_builder.application.use_cases.deferred_routes import (
    ClearCacheUseCase,
    DeleteRunHistoryUseCase,
    GetCacheStatusUseCase,
    GetDepsStatusUseCase,
    GetMetricsForRunUseCase,
    GetPackManifestUseCase,
    GetTaxonomyUseCase,
    GetTaxonomyTreeUseCase,
    ImportScanBinsUseCase,
    ListRunsUseCase,
    PreloadVoiceInputUseCase,
    RunBatchUseCase,
)
from qai.app_builder.application.use_cases.export_run_markdown import (
    ExportRunMarkdownUseCase,
)
from qai.app_builder.application.use_cases.get_aggregated_metrics import (
    GetAggregatedMetricsForModelUseCase,
)
from qai.app_builder.application.use_cases.inject_quality_score import (
    InjectQualityScoreUseCase,
)
from qai.app_builder.application.use_cases.run_benchmark import (
    GetBenchmarkUseCase,
    RunBenchmarkUseCase,
)
from qai.app_builder.application.use_cases.skill_and_schema import (
    GeneratePackCatalogUseCase,
    GetModelSchemaUseCase,
    ResolveModelInferenceCodeUseCase,
    ResolveSkillFilesUseCase,
)
from qai.app_builder.application.use_cases.get_app_model import (
    GetAppModelUseCase,
)
from qai.app_builder.application.use_cases.get_run import GetRunUseCase
from qai.app_builder.application.use_cases.get_worker_status import (
    GetWorkerStatusUseCase,
)
from qai.app_builder.application.use_cases.import_workflow import (
    ImportCommitUseCase,
    ImportDryRunUseCase,
    ImportRollbackUseCase,
)
from qai.app_builder.application.use_cases.list_app_models import (
    ListAppModelsUseCase,
)
from qai.app_builder.application.use_cases.list_run_artifacts import (
    ListRunArtifactsUseCase,
)
from qai.app_builder.application.use_cases.run_app import RunAppUseCase
from qai.app_builder.application.use_cases.share import (
    CreateShareUseCase,
    GetShareByTokenUseCase,
)
from qai.app_builder.application.use_cases.submit_feedback import (
    SubmitFeedbackUseCase,
)
from qai.app_builder.application.use_cases.upload_audio import (
    UploadAudioUseCase,
)
from qai.app_builder.application.use_cases.voice_preference import (
    GetVoicePreferenceUseCase,
    SetVoicePreferenceUseCase,
)
from qai.app_builder.domain import model_status as _model_status
from qai.app_builder.domain import taxonomy_tree as _taxonomy_tree
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.infrastructure import (
    FileSystemAppImportAdapter,
    FileSystemArtifactBlobReader,
    FileSystemArtifactStore,
    FileSystemAudioUpload,
    FileSystemPackFileCleanup,
    InMemoryRunnerCommandRegistry,
    ProcessBackedAppRunner,
    RunnerCommandRegistryPort,
    StickyBackedAppRunner,
    StickyWorkerHost,
    build_command_resolver,
    build_sticky_load_resolver,
    populate_runner_registry_from_manifests,
)
from qai.app_builder.infrastructure.app_manifest import (
    select_runner_interpreter,
)
from qai.app_builder.domain.app_project import AppProjectStartFailedError
from qai.app_builder.infrastructure.app_project_repository import (
    FileSystemAppProjectRepository,
)
from qai.app_builder.infrastructure.app_project_process_manager import (
    AppProjectProcessManager,
)
from qai.app_builder.infrastructure.app_project_packager import (
    FileSystemAppProjectPackager,
)
from qai.app_builder.infrastructure.app_manifest.reader import (
    FileSystemManifestReader,
)
from qai.app_builder.infrastructure.dep_checker import (
    DynamicPackDepChecker,
    PackDepDescriptor,
)
from qai.app_builder.infrastructure.result_cache import ResultCache
from qai.app_builder.application.use_cases.download_weights import (
    DownloadModelWeightsUseCase,
)
from qai.app_builder.infrastructure.run_exporter import MarkdownRunExporter
from qai.app_builder.infrastructure.skill_paths import (
    FilesystemSkillPathLocator,
)
from qai.app_builder.infrastructure.shared_weight_downloader import (
    SharedWeightDownloader,
)
from qai.app_builder.infrastructure.weight_download_config_reader import (
    FileSystemWeightDownloadConfigReader,
)
from qai.app_builder.infrastructure.weights_presence import (
    FileSystemWeightsPresence as _FileSystemWeightsPresence,
)
from qai.platform.download import (
    Aria2cDownloadEngine,
    Aria2cRpcDownloadEngine,
)
from qai.platform.process.subprocess_runner import SubprocessProcessRunner
from qai.platform.tasks import TaskRegistry

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = ["AppBuilderServices", "build_app_builder_services"]


# ---------------------------------------------------------------------------
# Lazy sticky-worker host proxy (State-Truth-First)
# ---------------------------------------------------------------------------
class _LazyStickyHost:
    """Forward host reads to ``container.sticky_worker_host`` live.

    The persistent :class:`StickyWorkerHost` is spawned by
    ``lifespan._spawn_sticky_worker`` AFTER this container (and its App
    Builder services) are built, so a build-time snapshot would always be
    ``None``. The :class:`StickyBackedAppRunner` already reads the host
    lazily via a ``host_provider`` lambda; the worker-status adapter and
    the voice-preload use case must do the same or they observe a stale
    "no host" forever (the voice-engine UI dot then never flips to
    "ready"). This proxy resolves the host on **every** attribute access so
    both surfaces report the real residency state (State-Truth-First).

    Duck-types the subset of :class:`StickyWorkerHost` consumed by
    :class:`StickyWorkerStatusAdapter` (``alive`` / ``state`` /
    ``active_model_id`` / ``multimodel`` / ``loaded_models_snapshot``) and
    by :class:`PreloadVoiceInputUseCase` (``alive`` / ``is_loaded``). When
    no host is spawned yet it reports ``alive=False`` / empty snapshot —
    the same graceful "not running" shape the inert placeholder produced.
    """

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def _host(self) -> StickyWorkerHost | None:
        return getattr(self._container, "sticky_worker_host", None)

    @property
    def alive(self) -> bool:
        h = self._host
        return bool(h is not None and h.alive)

    @property
    def state(self) -> str:
        h = self._host
        return h.state if h is not None else "absent"

    @property
    def active_model_id(self) -> str | None:
        h = self._host
        return h.active_model_id if h is not None else None

    @property
    def multimodel(self) -> bool:
        h = self._host
        return bool(h.multimodel) if h is not None else False

    def loaded_models_snapshot(self) -> tuple:  # mirrors host shape
        h = self._host
        return h.loaded_models_snapshot() if h is not None else ()

    def is_loaded(self, model_id: str, variant_id: str | None = None) -> bool:
        h = self._host
        return bool(h is not None and h.is_loaded(model_id, variant_id))

    async def cancel_run(self, run_id: str) -> None:
        """Stop the real work for ``run_id`` (satisfies RunCancellationPort).

        Two runner paths, two kill mechanisms (V1 ``runner.py:300-327`` killed
        both — ``op:cancel`` for the resident worker AND ``proc.terminate()``
        for the one-shot subprocess):

        1. **Sticky NPU worker**: forward ``op:cancel`` to the live host so the
           resident worker aborts inference + frees the NPU.
        2. **One-shot subprocess runner** (model-builder scripts): cancel the
           broadcaster's background drain task for this run, which closes the
           runner iterator → triggers ``SubprocessProcessRunner``'s ``finally``
           ``proc.kill()`` + tree-kill. Without this a "cancelled" one-shot run
           keeps its subprocess running to completion (§🔴 State-Truth-First) —
           and since the one-shot runner defaults to NO timeout, it would
           otherwise be unkillable.

        Both are best-effort + independent: a dead/absent host or a
        finished/absent drain task is a silent no-op; the DB-state flip in
        ``CancelRunUseCase`` still proceeds either way.
        """
        h = self._host
        if h is not None:
            await h.cancel_run(run_id)
        # Cancel the one-shot subprocess drain task (best-effort; resolved
        # lazily so DI construction order does not matter).
        app_builder = getattr(self._container, "app_builder", None)
        broadcaster = getattr(app_builder, "run_stream_broadcaster", None)
        if broadcaster is not None:
            try:
                broadcaster.cancel_drain(run_id)
            except Exception:  # noqa: BLE001 — never block the DB cancel
                pass


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AppBuilderServices:
    """Application services / ports for the ``app_builder`` namespace.

    Field-name lock (PR-034 §11): every PR-034 field is preserved
    verbatim. PR-045 only **adds** new fields:

    * ``share_repository``, ``worker_status`` — new ports;
    * ``get_run_use_case``, ``list_run_artifacts_use_case``,
      ``get_worker_status_use_case``, ``create_share_use_case``,
      ``get_share_by_token_use_case`` — new use cases (issue d).

    PR-301 only **adds** at the tail:

    * ``sticky_worker_host`` — optional reference to the running
      :class:`StickyWorkerHost`; ``None`` when sticky-worker is
      disabled or has not yet been spawned by the lifespan hook.
    """

    # use cases (PR-034)
    list_app_models_use_case: ListAppModelsUseCase
    get_app_model_use_case: GetAppModelUseCase
    delete_app_model_use_case: DeleteAppModelUseCase
    run_app_use_case: RunAppUseCase
    cancel_run_use_case: CancelRunUseCase
    upload_audio_use_case: UploadAudioUseCase
    get_voice_pref_use_case: GetVoicePreferenceUseCase
    set_voice_pref_use_case: SetVoicePreferenceUseCase
    import_dry_run_use_case: ImportDryRunUseCase
    import_commit_use_case: ImportCommitUseCase
    import_rollback_use_case: ImportRollbackUseCase
    # direct ports for read paths (PR-034)
    run_repository: RunRepositoryPort
    runner: RunnerPort
    artifact_blob_reader: ArtifactBlobReaderPort
    # NEW (PR-045 / issue d)
    share_repository: ShareRepositoryPort
    worker_status: WorkerStatusPort
    get_run_use_case: GetRunUseCase
    list_run_artifacts_use_case: ListRunArtifactsUseCase
    get_worker_status_use_case: GetWorkerStatusUseCase
    create_share_use_case: CreateShareUseCase
    get_share_by_token_use_case: GetShareByTokenUseCase
    # NEW (PR-301)
    sticky_worker_host: StickyWorkerHost | None = None
    # NEW (PR-302) — runner command registry; populated via DI / PR-303
    # manifest reader. Tail-append.
    runner_command_registry: RunnerCommandRegistryPort | None = None
    # NEW (PR-304) — deferred-route use cases. Tail-append.
    get_taxonomy_use_case: GetTaxonomyUseCase | None = None
    get_deps_status_use_case: GetDepsStatusUseCase | None = None
    preload_voice_input_use_case: PreloadVoiceInputUseCase | None = None
    get_metrics_for_run_use_case: GetMetricsForRunUseCase | None = None
    # NEW (3-M1) — model-level historical metrics aggregate backing
    # ``GET /api/app-builder/metrics/model/{model_id}``. Tail-append.
    get_aggregated_metrics_use_case: (
        GetAggregatedMetricsForModelUseCase | None
    ) = None
    get_cache_status_use_case: GetCacheStatusUseCase | None = None
    clear_cache_use_case: ClearCacheUseCase | None = None
    list_runs_use_case: ListRunsUseCase | None = None
    delete_run_history_use_case: DeleteRunHistoryUseCase | None = None
    get_pack_manifest_use_case: GetPackManifestUseCase | None = None
    import_scan_bins_use_case: ImportScanBinsUseCase | None = None
    run_batch_use_case: RunBatchUseCase | None = None
    # NEW (PR-305) — SKILL.md aggregation + Schema-driven UI + appbuilder_run.
    get_model_schema_use_case: GetModelSchemaUseCase | None = None
    # NEW (PR-094 §17.5 #14) — Markdown run-report export.
    export_run_markdown_use_case: ExportRunMarkdownUseCase | None = None
    # NEW (PR-094 §17.5 #11 / #12) — wired collaborators consumed by
    # :class:`RunAppUseCase`. Both are tail-appended optional fields:
    # ``ResultCache`` becomes ``None`` when
    # ``Settings.app_builder.result_cache_enabled`` is False;
    # ``DynamicPackDepChecker`` becomes ``None`` when
    # ``Settings.app_builder.dep_checker_enabled`` is False.
    result_cache: ResultCache | None = None
    dep_checker: DynamicPackDepChecker | None = None
    # NEW (V1 deps-status 逐 pack 进度 parity) — provider returning the
    # ``PackDepDescriptor`` list (one per scanned pack with a declared
    # ``requirements.txt``) so the ``GET /deps-status/packs`` route can
    # proactively schedule the background probe + install on poll (V1
    # triggered this on ``GET /models``). Tail-appended optional field;
    # ``None`` on stripped-down test containers / when the dep checker is
    # disabled (the route then returns an empty, not-checking snapshot).
    pack_dep_descriptors: "Callable[[], list[PackDepDescriptor]] | None" = None
    # NEW (PR-094 §17.5 #15) — quality-score injection use case used by
    # the catalog prompt builder to bias model selection toward packs
    # with positive user ratings.
    inject_quality_score_use_case: InjectQualityScoreUseCase | None = None
    # NEW (S9 close) — feedback persistence wires the previously
    # surface-only ``POST /api/app-builder/feedback`` route to a real
    # repository; tail-appended ports + use cases per v2.7 §3.1.
    feedback_repository: FeedbackRepositoryPort | None = None
    submit_feedback_use_case: SubmitFeedbackUseCase | None = None
    # NEW (S9 close) — benchmark harness + persistent results back the
    # ``POST /api/app-builder/benchmark`` and
    # ``GET /api/app-builder/benchmark/{benchmark_id}`` routes.
    benchmark_repository: BenchmarkRepositoryPort | None = None
    run_benchmark_use_case: RunBenchmarkUseCase | None = None
    get_benchmark_use_case: GetBenchmarkUseCase | None = None
    # NEW (V1 taxonomy parity) — full static taxonomy tree (group/task
    # label/icon/description/io + per-task model counts) backing
    # ``GET /api/app-builder/taxonomy/tree``. Tail-appended optional field.
    get_taxonomy_tree_use_case: GetTaxonomyTreeUseCase | None = None
    # NEW (V1 models-status parity) — resolves the per-model install +
    # dependency status (weights present on disk? deps ready/missing/
    # installing?) the gallery badge needs. A callable
    # ``AppModelDefinition -> AppModelStatusInfo`` wired in DI from the
    # pack manifest provider + pack-root probe + dependency checker.
    # ``None`` on stripped-down test containers (rows fall back to the
    # default ``Ready`` status). Tail-appended optional field.
    app_model_status_resolver: (
        "Callable[[AppModelDefinition], AppModelStatusInfo] | None"
    ) = None
    # NEW (R17) — application-layer run-stream broadcaster: holds the
    # per-run SSE broadcast registry + TTL eviction + background drainer
    # + replay state machine that previously lived as module-level
    # mutable state in ``interfaces/http/routes/app_builder.py``. A
    # single instance is shared process-wide so the ``POST /runs``
    # drainer and ``GET /runs/{run_id}/stream`` subscribers see the same
    # in-process frame buffers. Tail-appended optional field; ``None``
    # on stripped-down test containers that build the namespace by hand
    # (the route layer treats absence as "register a fresh broadcaster"
    # only via DI, so production always wires it).
    run_stream_broadcaster: RunStreamBroadcaster | None = None
    # NEW (R-3) — background-task registry holding strong refs to
    # fire-and-forget tasks (e.g. the benchmark drive task) so they are
    # not GC'd mid-flight and are cancelled on app shutdown. Tail-appended
    # optional field; ``None`` on stripped-down test containers (the route
    # layer falls back to a bare ``create_task`` when absent).
    background_tasks: "TaskRegistry | None" = None
    # NEW (V1 app-builder chat-prompt parity) — per-model SKILL path
    # resolver + capability-catalog generator consumed by the chat
    # context's ``app_builder`` system-prompt branch (via the
    # ``apps/api/_chat_di.py`` resolver/provider bridge). Both tail-appended
    # optional fields; ``None`` on stripped-down test containers.
    #
    # * ``resolve_skill_files_use_case`` — V1
    #   ``skill_resolver.resolve_skill_files``: returns the [top-level SKILL,
    #   selected-Pack SKILL] paths to inline (gated by ``skill.enabled``).
    # * ``generate_pack_catalog_use_case`` — V1
    #   ``skill_resolver.generate_pack_catalog_prompt``: the "可调用的本地
    #   AI 模型" capability list (I/O / params / metrics / ratings / variants
    #   + 6 usage rules) injected so the LLM knows what ``appbuilder_run``
    #   can drive.
    resolve_skill_files_use_case: ResolveSkillFilesUseCase | None = None
    generate_pack_catalog_use_case: GeneratePackCatalogUseCase | None = None
    # NEW (multi-model chat-prompt parity) — per-selected-model inference
    # code (``runner.py``) resolver. Consumed by the chat context's
    # ``app_builder`` system-prompt branch via the ``apps/api/_chat_di.py``
    # code-provider bridge so the Agent can help build a WebUI around the
    # selected model(s). ``None`` on stripped-down test containers.
    resolve_model_inference_code_use_case: (
        ResolveModelInferenceCodeUseCase | None
    ) = None
    # NEW (weight-download) — "download model weights" use case driving the
    # shared multi-threaded aria2c engine + shared extract helper. Backs the
    # ``POST/GET/DELETE /api/app-builder/weights/download`` routes.
    # Tail-appended optional field; ``None`` on stripped-down test
    # containers that build the namespace by hand (the route then 500s only
    # if that endpoint is exercised, never at wiring time).
    download_model_weights_use_case: DownloadModelWeightsUseCase | None = None
    # NEW (standalone fullstack app projects) — list/detail of generated
    # apps under ``data/app_builder/<app_id>/``, driven by
    # :class:`FileSystemAppProjectRepository`. Backs
    # ``GET /api/app-builder/apps`` + ``GET /api/app-builder/apps/{id}``.
    # Tail-appended optional fields; ``None`` on stripped-down test
    # containers (the ``/apps`` list route then returns an empty list
    # gracefully; ``/apps/{id}`` 503s only if exercised).
    list_app_projects_use_case: ListAppProjectsUseCase | None = None
    get_app_project_use_case: GetAppProjectUseCase | None = None
    # NEW (Phase 3 — managed run lifecycle) — start/stop/status/logs of a
    # generated app's FastAPI process, driven by AppProjectProcessManager
    # (which reuses the shared background_process manager + port allocator).
    # Backs ``POST|DELETE /apps/{id}/run`` + ``GET /apps/{id}/logs``.
    run_app_project_use_case: RunAppProjectUseCase | None = None
    stop_app_project_use_case: StopAppProjectUseCase | None = None
    get_app_project_status_use_case: GetAppProjectStatusUseCase | None = None
    get_app_project_logs_use_case: GetAppProjectLogsUseCase | None = None
    # NEW (Phase 5 — packaging) — zip a generated app + its model/weight
    # minimal set to ``<workspace>/app_builder_packages/``. Backs
    # ``POST /apps/{id}/package`` + SSE progress + cancel.
    package_app_project_use_case: PackageAppProjectUseCase | None = None
    # NEW (delete) — stop-if-running then remove the dev project dir under
    # ``data/app_builder/<id>/``. Backs ``DELETE /apps/{id}``. Needs both the
    # repository (delete) + the process manager (stop first), so it is wired
    # alongside the Phase-3 run use cases.
    delete_app_project_use_case: DeleteAppProjectUseCase | None = None
    # NEW (WS dual-transport) — generic multi-subscriber progress broadcaster
    # for download/packaging progress streams. Allows both SSE and WS
    # transports to share the same source iterator. Tail-appended optional
    # field; ``None`` on stripped-down test containers.
    progress_broadcaster: "ProgressBroadcaster | None" = None


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_app_builder_services(container: "Container") -> AppBuilderServices:
    """Wire ``container.app_builder`` with PR-045 / PR-301 real adapters.

    Uses ``container.{database, clock, ids, settings, events, data_paths}``.

    PR-301: the worker-status port is a :class:`StickyWorkerStatusAdapter`
    wrapping a lazy sticky-worker host provider. When
    ``container.sticky_worker_host`` is not yet set up (I1 integration
    lane's lifespan hook), the adapter surfaces ``alive=False`` /
    empty ``loaded_models`` until the live host registry is wired.
    """
    db = container.database
    clock = container.clock
    ids = container.ids
    events = container.events
    data_paths = container.data_paths
    settings = container.settings

    # PR-094 §17.5 #11 / #12 — wire optional production collaborators.
    # The cache is gated by ``app_builder.result_cache_enabled``; the
    # dep checker is gated by ``app_builder.dep_checker_enabled``. Both
    # ``None`` keeps :class:`RunAppUseCase` byte-for-byte equivalent to
    # the pre-PR-094 path (the use case is the single consumer).
    result_cache: ResultCache | None = None
    if settings.app_builder.result_cache_enabled:
        result_cache = ResultCache(
            max_entries=int(
                settings.app_builder.result_cache_max_entries or 64
            ),
            ttl_seconds=int(
                settings.app_builder.result_cache_ttl_seconds or 3600
            ),
            enabled=True,
        )

    dep_checker: DynamicPackDepChecker | None = None
    if settings.app_builder.dep_checker_enabled:
        # ``sys.executable`` is the venv interpreter when ``qai-serve``
        # boots through ``Setup.bat`` (the canonical dev path).
        # Production deployments running the ARM64 venv in-process see
        # the same path. ``uv_exe`` defaults to ``None`` so the checker
        # falls back to ``python -m pip install`` — uv is opt-in and
        # not part of the runtime install footprint.
        import sys as _sys
        from pathlib import Path as _Path

        dep_checker = DynamicPackDepChecker(
            python_exe=_Path(_sys.executable),
            uv_exe=None,
            enabled=True,
        )

    # Repositories
    app_models = SqliteAppModelRepository(db=db, clock=clock)
    runs = SqliteRunRepository(db=db, ids=ids)
    voice_prefs = SqliteVoicePrefRepository(db=db, clock=clock)
    shares = SqliteShareRepository(db=db)
    # S9 close — feedback + benchmark persistence (migration 011).
    feedbacks = SqliteFeedbackRepository(db=db)
    benchmarks = SqliteBenchmarkRepository(db=db)

    # Filesystem-backed infrastructure
    artifact_store = FileSystemArtifactStore(data_paths=data_paths)
    artifact_blob_reader = FileSystemArtifactBlobReader(data_paths=data_paths)
    audio_uploads = FileSystemAudioUpload(data_paths=data_paths, clock=clock)

    # Process runner — try to reuse container.process_runner (PR-041);
    # else build a default SubprocessProcessRunner locally.
    process_runner = getattr(container, "process_runner", None)
    if process_runner is None:
        process_runner = SubprocessProcessRunner()

    # App Builder Pack root resolution. The Pack root holds one
    # subdirectory per built-in model (``<root>/<model_id>/manifest.json``
    # + ``runner.py`` + ``SKILL.md``). V1 scanned ``features/app-builder/
    # models/`` directly; the v2.7 install layout ships the same assets
    # under ``factory/app_builder/models/``. When the lifespan hook
    # (or a test) has not injected ``container.app_builder_pack_root``,
    # fall back to the bundled factory directory rooted at the repo so
    # the manifest reader / runner registry / SKILL loader are wired with
    # real data instead of the inert null fallbacks. This is the
    # bootstrap that makes ``GET /api/app-builder/models`` (after the
    # lifespan DB seed) and ``GET /models/{id}/manifest`` return the
    # built-in models — parity with V1's disk-scan registry.
    pack_root_for_scan = _resolve_app_builder_pack_root(container)

    # P4 分层方案 C — user-imported Packs live in writable data storage
    # (``<data_dir>/app_builder/user_models/<id>/`` for the Pack tree +
    # ``<data_dir>/app_builder/user_model_weights/models/<id>/<bin>`` for the
    # staged weights). Built-in factory Packs stay in the release-contracted
    # factory tree (``pack_root_for_scan``) unchanged. Both roots feed the
    # runtime side-by-side: the manifest provider / runner registry /
    # weights probe / delete cleanup all union built-in + user (built-in
    # wins on id-collision — see ``_read_pack_manifests_union`` below).
    #
    # A ``None`` DataPaths (some lean test wiring) leaves the user roots at
    # ``None`` and the runtime degrades to the built-in-only path, matching
    # the legacy behaviour test fixtures still rely on.
    user_pack_root_for_scan: Path | None = None
    user_weights_root_for_install: Path | None = None
    if data_paths is not None:
        _u_pack = getattr(data_paths, "app_builder_user_pack_root", None)
        _u_weights = getattr(data_paths, "app_builder_user_weights_root", None)
        if isinstance(_u_pack, Path):
            user_pack_root_for_scan = _u_pack
        if isinstance(_u_weights, Path):
            user_weights_root_for_install = _u_weights

    # Read every ``<pack_root>/<id>/manifest.json`` once. Bad manifests
    # are logged and skipped (strict=False) so a single malformed pack
    # never blanks the whole gallery. Empty tuple when pack_root is None
    # or missing — preserves the inert fallback for stripped-down test
    # containers.
    #
    # P4 union scan: read from BOTH the built-in factory root and the user
    # data root. ``_read_pack_manifests_union`` returns a merged tuple with
    # built-in taking precedence on id-collision (release-contracted names
    # win over user imports; a warning is logged for the shadowed user
    # entry). Empty tuples on both sides degrade to the legacy behaviour.
    pack_manifests, pack_manifest_origin = _read_pack_manifests_union(
        builtin_root=pack_root_for_scan,
        user_root=user_pack_root_for_scan,
    )

    # Edition runtime gate (edition-dual-form-design.md §3 layer 1 / §4.1).
    # ``ppocrv4`` (OCR) is an internal-only Pack: the internal edition ships
    # the model, the external edition does NOT bundle it and has no download
    # source. Drop internal-only built-in Packs from the scanned manifests
    # when running the external edition so the gallery / manifest provider /
    # runner registry / dep descriptors all uniformly omit them — even if a
    # not-yet-physically-stripped tree still carries the Pack dir (defence in
    # depth on top of the layer-2 manifest.toml [exclude] removal). When the
    # model is absent (external physical strip) this is a harmless no-op; the
    # filesystem reader already returned nothing for it. The internal edition
    # (the dev source tree you run by default) keeps every built-in Pack.
    pack_manifests = _filter_internal_only_packs(
        pack_manifests, is_internal=settings.is_internal
    )

    # V1 deps-status 逐 pack 进度 parity — build the ``PackDepDescriptor``
    # list (model_id + absolute requirements.txt path) from the scanned
    # manifests so ``GET /deps-status/packs`` can proactively schedule the
    # background probe + install on poll (V1 fired this on ``GET /models``;
    # see ``backend/app_builder/api_routes.py:835`` /
    # ``frontend/js/composables/useAppBuilderRegistry.js:269-342``). Only
    # packs that declare a ``runner.requirements`` path participate. The
    # provider is a thin closure (re-derives the list on each poll) so a
    # Pack imported at runtime is picked up without re-wiring DI.
    def _build_pack_dep_descriptors() -> list[PackDepDescriptor]:
        from pathlib import Path as _Path

        out: list[PackDepDescriptor] = []
        for m in pack_manifests:
            req = getattr(getattr(m, "runner", None), "requirements", "")
            if not isinstance(req, str) or not req.strip():
                continue
            try:
                out.append(
                    PackDepDescriptor(
                        model_id=str(m.model_id),
                        requirements_path=_Path(req),
                    )
                )
            except Exception:  # noqa: BLE001 — a bad row must not break the poll
                continue
        return out

    pack_dep_descriptors_provider = (
        _build_pack_dep_descriptors if dep_checker is not None else None
    )

    # PR-302: runner command registry. Read from container if present
    # (PR-303 manifest reader populates it on startup); otherwise build
    # one from the scanned manifests so on-device runs resolve a real
    # command. Falls back to an empty in-memory registry when no pack
    # root / manifests are available (resolver returns None ⇒
    # ProcessBackedAppRunner emits ``no_command``, PR-045 behaviour).
    runner_registry: RunnerCommandRegistryPort | None = getattr(
        container, "runner_command_registry", None
    )
    if runner_registry is None:
        # P4 dual-root — populate the registry from BOTH the built-in
        # factory Packs (anchored at ``pack_root_for_scan``) and any
        # user-imported Packs (anchored at ``user_pack_root_for_scan``).
        # The bridge is called once per root so each ``model_id`` resolves
        # its ``runner.py`` under the correct anchor; when either root
        # holds no manifests the corresponding call is skipped.
        if pack_manifests and (
            pack_root_for_scan is not None
            or user_pack_root_for_scan is not None
        ):
            runner_registry = InMemoryRunnerCommandRegistry()
            builtin_manifests = tuple(
                m
                for m in pack_manifests
                if pack_manifest_origin.get(m.model_id) == "built-in"
            )
            user_manifests = tuple(
                m
                for m in pack_manifests
                if pack_manifest_origin.get(m.model_id) == "user"
            )
            if builtin_manifests and pack_root_for_scan is not None:
                populate_runner_registry_from_manifests(
                    manifests=builtin_manifests,
                    pack_root=pack_root_for_scan,
                    repo_root=container.repo_root,
                    registry=runner_registry,
                    qairt_env_file=getattr(container, "qairt_env_file", None),
                    extra_pythonpath=_pack_shared_pythonpath(container),
                )
            if user_manifests and user_pack_root_for_scan is not None:
                populate_runner_registry_from_manifests(
                    manifests=user_manifests,
                    pack_root=user_pack_root_for_scan,
                    repo_root=container.repo_root,
                    registry=runner_registry,
                    qairt_env_file=getattr(container, "qairt_env_file", None),
                    extra_pythonpath=_pack_shared_pythonpath(container),
                )
        else:
            runner_registry = InMemoryRunnerCommandRegistry()
    # 缺口 10: build the live global-proxy provider (mechanism B) so the
    # whisper / zipformer / melotts weight downloads route through the
    # configured proxy. Function-local import mirrors the sibling DI modules
    # (``_ai_coding_di``) and keeps module load free of circular imports.
    from ._global_proxy import build_global_proxy_provider

    _weight_proxy_provider = build_global_proxy_provider(container)
    command_resolver = build_command_resolver(
        registry=runner_registry,
        # Match the interpreter policy the registry_bridge used when
        # populating ``runner_registry``: when a ``qairt_env.json`` is
        # configured we reuse the same QairtEnvJsonResolver so the
        # resolver-level QAIRT extras (``QAIRT_ROOT`` / ``QNN_SDK_ROOT``
        # / SDK ``bin``+``lib`` on PATH) flow into every spawn —
        # otherwise the Pack subprocess imports ``qai_appbuilder`` and
        # crashes with ``QAI_APPBUILDER_UNAVAILABLE`` because the QNN
        # runtime DLLs aren't on PATH.
        interpreter=select_runner_interpreter(
            qairt_env_file=getattr(container, "qairt_env_file", None),
            repo_root=container.repo_root,
        ),
        # Batch E: feed the runner's stdin a JSON envelope carrying
        # ``repoRoot`` / ``packDir`` / ``inputs`` / ``params`` /
        # ``variant`` so ``runner_protocol.read_request()`` can drive
        # the 4 built-in Pack runners. Without ``repo_root`` the
        # subprocess immediately dies with ``RuntimeError("no request
        # received on stdin")``.
        repo_root=container.repo_root,
        # Resolve logical upload paths (``uploads/audio/…`` etc.) to
        # absolute physical paths under the data blob root before the
        # runner request is serialised (voice/OCR input-path fix).
        blobs_dir=data_paths.blobs_dir,
        # 缺口 10: inject the live global proxy into the runner spawn env
        # (HTTPS_PROXY / ALL_PROXY) so weight S3 downloads route through it.
        proxy_provider=_weight_proxy_provider,
    )
    oneshot_runner = ProcessBackedAppRunner(
        runner=process_runner, command_resolver=command_resolver
    )

    # Importer is constructed AFTER the manifest provider + runner registry
    # (below) so its post-commit refresh callback can update both at runtime.
    # See the ``importer = FileSystemAppImportAdapter(...)`` block after
    # ``manifest_provider`` is built.

    # PR-301 / voice-input fix: the sticky-worker host is spawned by the
    # lifespan hook AFTER this container is built, so a build-time snapshot
    # is always ``None``. Wrap a lazy proxy that resolves
    # ``container.sticky_worker_host`` on every access so the worker-status
    # adapter and the voice-preload use case observe the real residency
    # state once the host comes up (State-Truth-First). The
    # ``StickyBackedAppRunner`` below keeps its own lazy ``host_provider``.
    sticky_host: StickyWorkerHost | None = getattr(
        container, "sticky_worker_host", None
    )
    lazy_sticky = _LazyStickyHost(container)
    worker_status: WorkerStatusPort = StickyWorkerStatusAdapter(lazy_sticky)

    # PR-304: deferred-route use cases. Built from the same primitives
    # already available — DataPaths for filesystem-rooted UCs, the
    # registry for deps status, the sticky host for preload.
    blob_dir = data_paths.blobs_dir / "app_builder"
    runner_registry_count_provider = (
        (lambda: len(runner_registry))
        if hasattr(runner_registry, "__len__")
        else None
    )
    # Resolve shared_dir with the same fallback the PYTHONPATH builder uses
    # (factory/app_builder/shared) so deps-status reports the directory
    # as present whenever the bundled shared/ helpers ship with the install.
    shared_dir = getattr(container, "app_builder_shared_dir", None)
    if shared_dir is None:
        repo_root = getattr(container, "repo_root", None)
        if repo_root is not None:
            candidate = Path(repo_root).joinpath(
                "factory", "app_builder", "shared"
            )
            if candidate.is_dir():
                shared_dir = candidate
    deps_status_uc = GetDepsStatusUseCase(
        qairt_env_file=getattr(container, "qairt_env_file", None),
        pack_root=pack_root_for_scan,
        shared_dir=shared_dir,
        sticky_worker_host=lazy_sticky,
        registered_pack_count_provider=runner_registry_count_provider,
    )

    # The pack manifest provider is a callable ``AppModelId -> PackManifest
    # | None``. Prefer one injected by the lifespan hook; otherwise build
    # one from the manifests scanned above so ``GET /models/{id}/manifest``,
    # the Schema-driven UI and the SKILL aggregation all resolve real
    # data. When no manifests are available the provider returns None for
    # every id ⇒ ManifestNotAvailableError ⇒ 503 (PR-303 behaviour).
    manifest_provider = getattr(container, "app_builder_manifest_provider", None)
    if manifest_provider is None:
        manifest_provider = _build_manifest_provider(
            pack_manifests, origin_by_id=pack_manifest_origin
        )

    # ── Import-commit physical install + runtime refresh (V1 parity) ──────
    # The importer copies a committed Pack into ``pack_root/<id>/`` and stages
    # its weights under the manifest installPath anchor (``repo_root/models/
    # <id>/<bin>``) so the manifest provider / runner registry / weights probe
    # actually find the model. The ``on_pack_installed`` callback then refreshes
    # those runtime structures so the just-imported model is runnable WITHOUT a
    # restart (V1 ``importer.refresh_after_import``). The callback lives here in
    # the composition layer — it touches the filesystem + infrastructure
    # registries, which ``qai.app_builder.domain``/``application`` must not
    # (domain-purity); the importer (infrastructure) only invokes the opaque
    # callback (no cross-context / no FS in the use case).
    repo_root_for_install = getattr(container, "repo_root", None)

    def _on_pack_installed(model_id_value: str, pack_dir: Path) -> None:
        """Refresh runtime registries after a Pack is installed (best-effort)."""
        # 1. Read the freshly-written manifest and register it with the
        #    refreshable manifest provider (gallery status / manifest endpoint
        #    / SKILL). Skip silently when the provider isn't refreshable
        #    (injected provider / lean container) or the manifest is unreadable.
        #
        #    P4: fresh imports physically land under ``user_pack_root_for_scan``
        #    (the importer's ``_target_pack_root``), so ``pack_dir.parent`` is
        #    the user root; ``add()`` marks the new entry with
        #    ``origin="user"`` so downstream consumers pick the paired user
        #    weights anchor. When ``user_pack_root_for_scan`` is not wired
        #    (legacy test container) ``pack_dir.parent`` is the built-in
        #    ``pack_root_for_scan`` and we default to ``origin="user"`` anyway
        #    because a fresh import is a user import by definition.
        try:
            new_manifests = _read_pack_manifests(pack_dir.parent)
            this = next(
                (m for m in new_manifests if m.model_id == model_id_value),
                None,
            )
            add_fn = getattr(manifest_provider, "add", None)
            if this is not None and callable(add_fn):
                # ``add`` on the P4 refreshable provider accepts an ``origin``
                # kwarg; the plain fallback shim (injected test provider)
                # may not — fall back to positional-only in that case.
                try:
                    add_fn(this, origin="user")
                except TypeError:
                    add_fn(this)
        except Exception:  # noqa: BLE001 — refresh must never fail the commit
            _logger.warning(
                "app_import.refresh_manifest_failed: id=%s",
                model_id_value,
                exc_info=True,
            )
            this = None
        # 2. Register the runner spec so the model is immediately runnable.
        #    The Pack physically sits under ``pack_dir.parent`` (whichever
        #    anchor the importer chose — user root in production, built-in
        #    in legacy tests); pass that same anchor to the bridge so
        #    ``<anchor>/<id>/runner.py`` resolves correctly.
        try:
            register_fn = getattr(runner_registry, "register", None)
            if (
                this is not None
                and callable(register_fn)
                and repo_root_for_install is not None
            ):
                populate_runner_registry_from_manifests(
                    manifests=(this,),
                    pack_root=pack_dir.parent,
                    repo_root=repo_root_for_install,
                    registry=runner_registry,
                    qairt_env_file=getattr(container, "qairt_env_file", None),
                    extra_pythonpath=_pack_shared_pythonpath(container),
                )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "app_import.refresh_runner_failed: id=%s",
                model_id_value,
                exc_info=True,
            )

    importer = FileSystemAppImportAdapter(
        db=db,
        app_models=app_models,
        clock=clock,
        ids=ids,
        pack_root=pack_root_for_scan,
        repo_root=repo_root_for_install,
        # P4: route fresh imports to the user anchors when configured
        # (production path); the importer falls back to the built-in
        # anchors when either is ``None`` (lean test container).
        user_pack_root=user_pack_root_for_scan,
        user_weights_root=user_weights_root_for_install,
        on_pack_installed=_on_pack_installed,
    )

    # Symmetric counterpart to the importer's physical install: delete removes
    # the same on-disk artifacts (pack dir + staged weights) so a deleted model
    # leaves no orphaned files (V1 ``deleteFiles=true``). Same pack_root /
    # repo_root anchors the importer used; None on lean test containers → the
    # delete use case degrades to DB-only removal. P4: also carries the user
    # anchors so ``_locate_pack`` picks the correct root for user-imported
    # Packs (State-Truth-First — probes disk to find which anchor holds the
    # pack dir, no in-process ``user_imported`` flag needed).
    pack_file_cleanup = FileSystemPackFileCleanup(
        pack_root=pack_root_for_scan,
        repo_root=repo_root_for_install,
        user_pack_root=user_pack_root_for_scan,
        user_weights_root=user_weights_root_for_install,
    )

    # P2 / Sub-A — runtime cache invalidation on Pack deletion.
    # Symmetric counterpart to ``_on_pack_installed`` above: after
    # ``DeleteAppModelUseCase`` removes the DB row + on-disk files, the
    # three in-process caches keyed by ``model_id`` (manifest provider,
    # runner command registry, sticky-worker resident model map) still
    # hold stale entries; a subsequent request for that id would then
    # succeed against phantom state (§🔴 State-Truth-First 铁律 1: the
    # in-process cache diverges from the real disk / worker state).
    # This callback clears all three so a fresh request for the just-
    # deleted id fails cleanly.
    #
    # The manifest provider / runner registry may be plain callables
    # (lean test containers) — we duck-type via ``getattr`` so the
    # callback degrades to "clear only what supports invalidation".
    async def _on_pack_removed(model_id_value: str) -> None:
        """Clear runtime caches after a Pack is deleted (best-effort)."""
        # 1. Manifest provider (``AppModelManifestProviderPort.remove``).
        remove_fn = getattr(manifest_provider, "remove", None)
        if callable(remove_fn):
            try:
                remove_fn(model_id_value)
            except Exception:  # noqa: BLE001 — never fail the delete
                _logger.warning(
                    "app_builder.pack_removed.manifest_clear_failed: id=%s",
                    model_id_value,
                    exc_info=True,
                )
        # 2. Runner command registry (``RunnerRegistryPort.unregister``).
        # Legacy adapters expose ``deregister``; the new port name is
        # ``unregister``. Prefer the new spelling, fall back to legacy.
        unregister_fn = getattr(runner_registry, "unregister", None)
        if not callable(unregister_fn):
            unregister_fn = getattr(runner_registry, "deregister", None)
        if callable(unregister_fn):
            try:
                unregister_fn(model_id_value)
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "app_builder.pack_removed.runner_registry_clear_failed: id=%s",
                    model_id_value,
                    exc_info=True,
                )
        # 3. Sticky worker host — evict the resident Genie/QNN context
        # so a freshly-imported model with the same id gets a clean
        # native load (rather than reusing a freed weights buffer).
        # The host is spawned lazily; ``getattr`` returns ``None`` when
        # sticky-worker is disabled or has not started yet.
        host = getattr(container, "sticky_worker_host", None)
        if host is not None:
            release_fn = getattr(host, "release_model", None)
            if callable(release_fn):
                try:
                    await release_fn(model_id_value)
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "app_builder.pack_removed.worker_evict_failed: id=%s",
                        model_id_value,
                        exc_info=True,
                    )

    # PR-302 wiring — make every run prefer the resident sticky worker
    # (model stays loaded on the NPU across runs) and fall back to the
    # one-shot subprocess only when the worker is unavailable. This is
    # the V1 ``python_script.run_pack`` two-layer structure (sticky →
    # oneshot fallback) and eliminates the per-run ``model_destroy`` +
    # NPU ``Error 0x200`` churn the one-shot-only path caused.
    #
    # The host is read **lazily** (``getattr(container, ...)``) on every
    # run because the lifespan hook spawns it *after* the container (and
    # hence this runner) is built — State-Truth-First: the runner reads
    # the live host reference, not a build-time ``None`` snapshot.
    sticky_load_resolver = build_sticky_load_resolver(
        registry=runner_registry,
        repo_root=container.repo_root,
        manifest_provider=manifest_provider,
    )
    runner: RunnerPort = StickyBackedAppRunner(
        host_provider=lambda: getattr(container, "sticky_worker_host", None),
        fallback=oneshot_runner,
        load_resolver=sticky_load_resolver,
        blobs_dir=data_paths.blobs_dir,
    )

    # PR-094 §17.5 #15 — single instance shared between the catalog
    # tool descriptor (which surfaces ``quality_score`` per model row)
    # and the AppBuilderServices field (consumers that only need the
    # raw scores, e.g. the UI catalog page).
    #
    # Gap #7 — the catalog the LLM sees must reflect user feedback. The
    # S9 cutover moved ratings into the append-only ``app_builder_feedback``
    # table (``feedbacks`` below), so the quality-score injector reads its
    # authoritative signal from there (falling back to legacy inline
    # ``run.inputs`` ratings). V2 builds the catalog live on every chat
    # request (no TTL cache), so wiring ``feedbacks`` here is sufficient:
    # a freshly-submitted rating shows up in the LLM catalog on the very
    # next request, with no stale cache to invalidate (V1 needed an
    # explicit ``_catalog_cache_invalidate()`` after feedback —
    # ``api_routes.py:1685`` — because it cached the catalog for 10s).
    inject_quality_score_uc = InjectQualityScoreUseCase(
        runs=runs,
        feedbacks=feedbacks,
    )

    # S9 close — share a single :class:`RunAppUseCase` instance across
    # the three consumers that need it (the public ``run_app_use_case``
    # field, the batch wrapper, and the new benchmark harness). The use
    # case is internally stateless beyond its constructor args so a
    # single instance is safe to re-use; this also keeps the dep-checker
    # / result-cache wiring consistent for all entry points.
    run_app_uc = RunAppUseCase(
        app_models=app_models,
        runs=runs,
        runner=runner,
        artifact_store=artifact_store,
        events=events,
        clock=clock,
        ids=ids,
        result_cache=result_cache,
        dep_checker=dep_checker,
        manifest_resolver=manifest_provider,
    )

    # V1 models-status parity — per-model install + dependency status
    # resolver consumed by the ``GET /api/app-builder/models`` mapper. Wired
    # from the manifest provider + pack-root probe (weights present?) and the
    # dependency checker's cached status (deps ready/missing/installing).
    repo_root_for_status = getattr(container, "repo_root", None)
    app_model_status_resolver = _build_status_resolver(
        manifest_provider=manifest_provider,
        pack_root=pack_root_for_scan,
        repo_root=(
            repo_root_for_status
            if isinstance(repo_root_for_status, Path)
            else None
        ),
        user_pack_root=user_pack_root_for_scan,
        user_weights_root=user_weights_root_for_install,
        dep_checker=dep_checker,
    )

    # V1 app-builder chat-prompt parity — per-model SKILL path resolver +
    # capability-catalog generator. The SKILL path locator is rooted at the
    # same pack root the SKILL *body* loader uses; when the pack root is
    # absent (stripped-down test container) both use cases stay ``None`` so
    # the chat bridge degrades to the empty-result behaviour.
    resolve_skill_files_uc: ResolveSkillFilesUseCase | None = None
    generate_pack_catalog_uc: GeneratePackCatalogUseCase | None = None
    resolve_model_inference_code_uc: (
        ResolveModelInferenceCodeUseCase | None
    ) = None
    if pack_root_for_scan is not None:
        # P4 双根修复：locator 需同时覆盖 built-in + user 两根，否则用户
        # 导入的 pack 的 SKILL.md / runner.py 会解析不到（见 modules
        # ``skill_paths.py`` / ``skill_and_schema.py`` 的双 anchor 文档）。
        resolve_skill_files_uc = ResolveSkillFilesUseCase(
            locator=FilesystemSkillPathLocator(
                pack_root=pack_root_for_scan,
                user_pack_root=user_pack_root_for_scan,
            ),
            manifest_provider=manifest_provider,
        )
        resolve_model_inference_code_uc = ResolveModelInferenceCodeUseCase(
            locator=FilesystemSkillPathLocator(
                pack_root=pack_root_for_scan,
                user_pack_root=user_pack_root_for_scan,
            ),
            manifest_provider=manifest_provider,
            app_models=app_models,
        )
    generate_pack_catalog_uc = GeneratePackCatalogUseCase(
        app_models=app_models,
        manifest_provider=manifest_provider,
        status_provider=app_model_status_resolver,
        inject_quality_score_use_case=inject_quality_score_uc,
        # P4 通用路径元信息：让 catalog 里每个模型条目带 built-in / user layout
        # 标签 + ${APP_ROOT} 相对路径 + env-var 名字。这是**对所有 pack 100%
        # 覆盖**的信息通道（不依赖 skill.enabled 或 SKILL.md 是否存在），
        # Agent 写 app.yaml / 回答用户"模型在哪" / 调试加载失败时都能用到。
        origin_by_id=pack_manifest_origin,
    )

    # ── Weight-download use case ────────────────────────────────────────
    # Drives the shared multi-threaded aria2c engine (identical construction
    # to ``_model_catalog_di.py``: RPC engine with the single-frame CLI
    # engine as fallback, both wired to ``container.process_runner``). Reads
    # the per-Pack ``weights.json`` by path and runs the shared extract
    # helper post-download. Only wired when ``repo_root`` + a shared dir are
    # resolvable (production / dev); lean test containers leave it ``None``.
    download_model_weights_uc: DownloadModelWeightsUseCase | None = None
    repo_root_for_weights = getattr(container, "repo_root", None)
    shared_dirs = _pack_shared_pythonpath(container)
    if isinstance(repo_root_for_weights, Path) and shared_dirs:
        download_engine = Aria2cRpcDownloadEngine(
            fallback_engine=Aria2cDownloadEngine(
                process_runner=getattr(container, "process_runner", None),
                download_root=data_paths.root,
            ),
            process_runner=getattr(container, "process_runner", None),
            download_root=data_paths.root,
        )
        shared_downloader = SharedWeightDownloader(shared_dir=shared_dirs[0])
        download_model_weights_uc = DownloadModelWeightsUseCase(
            engine=download_engine,
            config_port=FileSystemWeightDownloadConfigReader(
                repo_root=repo_root_for_weights
            ),
            extract=shared_downloader.extract_weights_archive,
            detect_device=shared_downloader.detect_device_model,
            ids=ids,
            repo_root=repo_root_for_weights,
            data_root=data_paths.root,
        )

    # Standalone fullstack app projects (data/app_builder/<app_id>/). The
    # repository scans that root for directories carrying a valid
    # ``app.yaml``; it is always wireable (only needs the data root), so the
    # list/detail use cases are constructed unconditionally.
    app_project_repository = FileSystemAppProjectRepository(
        apps_root=data_paths.root / "app_builder"
    )
    list_app_projects_uc = ListAppProjectsUseCase(
        repository=app_project_repository
    )
    get_app_project_uc = GetAppProjectUseCase(
        repository=app_project_repository
    )

    # Phase 3 — managed run lifecycle. The interpreter resolver supplies
    # the venv python + QAIRT SDK env/PATH (empty when no SDK). Model root
    # is ``<repo_root>/models`` (the manifest installPath anchor, matching
    # ``APP_BUILDER_MODEL_ROOT`` in plan §4.4 — NOT data/models). The
    # background-process manager is resolved LAZILY via a provider closure
    # because DI wires ``app_builder`` before ``background_process``
    # (di.py:293 vs :354); a build-time ``container.background_process``
    # read would be ``None`` here. Lean containers without ``repo_root``
    # leave the run use cases ``None`` (routes 503 only if exercised).
    run_app_project_uc: RunAppProjectUseCase | None = None
    stop_app_project_uc: StopAppProjectUseCase | None = None
    status_app_project_uc: GetAppProjectStatusUseCase | None = None
    logs_app_project_uc: GetAppProjectLogsUseCase | None = None
    delete_app_project_uc: DeleteAppProjectUseCase | None = None
    _repo_root_for_apps = getattr(container, "repo_root", None)
    if isinstance(_repo_root_for_apps, Path):
        _app_interp = select_runner_interpreter(
            qairt_env_file=getattr(container, "qairt_env_file", None),
            repo_root=_repo_root_for_apps,
        )

        def _resolve_bg_manager():
            bg = getattr(container, "background_process", None)
            mgr = getattr(bg, "manager", None)
            if mgr is None:
                raise AppProjectStartFailedError(
                    message=(
                        "background-process manager is not available; "
                        "cannot run app projects"
                    )
                )
            return mgr

        app_project_process = AppProjectProcessManager(
            manager=_resolve_bg_manager,
            python_exe=_app_interp.resolve(),
            repo_root=_repo_root_for_apps,
            model_root=_repo_root_for_apps / "models",
            pack_root=(
                _resolve_app_builder_pack_root(container)
                or _repo_root_for_apps.joinpath(*_DEFAULT_PACK_ROOT_REL)
            ),
            # P4: expose the user-imported anchors to spawned apps so an app
            # that loads a user-imported model resolves under writable data
            # storage. ``None`` (no DataPaths) omits the env vars entirely.
            user_pack_root=user_pack_root_for_scan,
            user_model_root=user_weights_root_for_install,
            shared_dirs=_pack_shared_pythonpath(container),
            qairt_extra_env=_app_interp.extra_env(),
            qairt_path_segments=tuple(_app_interp.path_segments()),
        )
        run_app_project_uc = RunAppProjectUseCase(
            repository=app_project_repository,
            process=app_project_process,
        )
        stop_app_project_uc = StopAppProjectUseCase(
            process=app_project_process
        )
        status_app_project_uc = GetAppProjectStatusUseCase(
            process=app_project_process
        )
        logs_app_project_uc = GetAppProjectLogsUseCase(
            process=app_project_process
        )
        delete_app_project_uc = DeleteAppProjectUseCase(
            repository=app_project_repository,
            process=app_project_process,
        )

    # Phase 5 — packaging. Only needs repo_root + the user workspace + the
    # apps root, so it is wireable whenever repo_root resolves (independent
    # of the background-process manager). Output goes to
    # ``<workspace>/app_builder_packages/``.
    package_app_project_uc: PackageAppProjectUseCase | None = None
    if isinstance(_repo_root_for_apps, Path):
        from apps.api._workspace_resolver import resolve_workspace_root

        app_project_packager = FileSystemAppProjectPackager(
            repo_root=_repo_root_for_apps,
            workspace_root=Path(resolve_workspace_root(container)),
            apps_root=data_paths.root / "app_builder",
            # P4 双根：packager 需感知用户导入的 pack / weights 根，否则
            # ``_collect_model_weights`` / ``_collect_model_pack`` 会把
            # 用户 pack 的路径判为"outside root"并跳过，产出的 zip 缺 .bin。
            # 见 ``app_project_packager.py`` 顶部 dual-anchor docstring。
            user_pack_root=user_pack_root_for_scan,
            user_weights_root=user_weights_root_for_install,
        )
        package_app_project_uc = PackageAppProjectUseCase(
            repository=app_project_repository,
            packager=app_project_packager,
        )

    return AppBuilderServices(
        list_app_models_use_case=ListAppModelsUseCase(
            app_models=app_models,
            # State-Truth-First: drop rows whose on-disk pack is gone so an
            # externally/manually deleted pack never lingers as a phantom
            # model (V1 listed by disk scan). Reuses the pack-root probe.
            # P4: pass both built-in + user roots so a Pack living under
            # either anchor counts as "present".
            pack_presence=_FileSystemWeightsPresence(
                pack_root=pack_root_for_scan,
                repo_root=repo_root_for_install,
                user_pack_root=user_pack_root_for_scan,
                user_weights_root=user_weights_root_for_install,
            ),
        ),
        get_app_model_use_case=GetAppModelUseCase(app_models=app_models),
        delete_app_model_use_case=DeleteAppModelUseCase(
            app_models=app_models,
            pack_files=pack_file_cleanup,
            on_pack_removed=_on_pack_removed,
            # P3 (Sub-B) — cancel any in-flight runs for this model_id
            # BEFORE the pack files are removed, so the resident worker
            # releases the NPU cleanly rather than crashing mid-``op:run``
            # (§🔴 State-Truth-First 铁律 1/2). ``lazy_sticky`` already
            # satisfies ``RunCancellationPort.cancel_run(run_id)`` (see
            # ``_LazyStickyHost.cancel_run`` above) — it is the same
            # canceller wired into ``CancelRunUseCase`` a few lines down,
            # so a manual cancel + a model-delete take the same path.
            runs=runs,
            run_cancellation=lazy_sticky,
        ),
        run_app_use_case=run_app_uc,
        cancel_run_use_case=CancelRunUseCase(
            runs=runs, events=events, clock=clock, canceller=lazy_sticky
        ),
        upload_audio_use_case=UploadAudioUseCase(uploads=audio_uploads),
        get_voice_pref_use_case=GetVoicePreferenceUseCase(prefs=voice_prefs),
        set_voice_pref_use_case=SetVoicePreferenceUseCase(prefs=voice_prefs),
        import_dry_run_use_case=ImportDryRunUseCase(importer=importer),
        import_commit_use_case=ImportCommitUseCase(
            importer=importer, events=events, clock=clock
        ),
        import_rollback_use_case=ImportRollbackUseCase(
            importer=importer, events=events, clock=clock
        ),
        run_repository=runs,
        runner=runner,
        artifact_blob_reader=artifact_blob_reader,
        # NEW (PR-045 / issue d)
        share_repository=shares,
        worker_status=worker_status,
        get_run_use_case=GetRunUseCase(runs=runs),
        list_run_artifacts_use_case=ListRunArtifactsUseCase(runs=runs),
        get_worker_status_use_case=GetWorkerStatusUseCase(
            worker_status=worker_status
        ),
        create_share_use_case=CreateShareUseCase(
            runs=runs, shares=shares, clock=clock
        ),
        get_share_by_token_use_case=GetShareByTokenUseCase(
            runs=runs, shares=shares, clock=clock
        ),
        sticky_worker_host=sticky_host,
        runner_command_registry=runner_registry,
        # PR-304 deferred-route use cases
        get_taxonomy_use_case=GetTaxonomyUseCase(app_models=app_models),
        get_taxonomy_tree_use_case=GetTaxonomyTreeUseCase(app_models=app_models),
        get_deps_status_use_case=deps_status_uc,
        preload_voice_input_use_case=PreloadVoiceInputUseCase(
            prefs=voice_prefs,
            sticky_worker_host=lazy_sticky,
        ),
        get_metrics_for_run_use_case=GetMetricsForRunUseCase(runs=runs),
        get_aggregated_metrics_use_case=GetAggregatedMetricsForModelUseCase(
            runs=runs,
            feedback=feedbacks,
        ),
        get_cache_status_use_case=GetCacheStatusUseCase(blob_dir=blob_dir),
        clear_cache_use_case=ClearCacheUseCase(blob_dir=blob_dir),
        list_runs_use_case=ListRunsUseCase(
            app_models=app_models, runs=runs
        ),
        delete_run_history_use_case=DeleteRunHistoryUseCase(runs=runs),
        get_pack_manifest_use_case=GetPackManifestUseCase(
            app_models=app_models,
            manifest_provider=manifest_provider,
        ),
        import_scan_bins_use_case=(
            ImportScanBinsUseCase(scan_root=pack_root_for_scan)
            if pack_root_for_scan is not None
            else ImportScanBinsUseCase(scan_root=blob_dir)
        ),
        run_batch_use_case=RunBatchUseCase(
            run_app_use_case=run_app_uc,
        ),
        # PR-305 SKILL.md aggregation + Schema + appbuilder_run descriptor
        get_model_schema_use_case=GetModelSchemaUseCase(
            app_models=app_models,
            manifest_provider=manifest_provider,
        ),
        # PR-094 §17.5 #14 — Markdown run-report export. The renderer
        # adapter is injected so the use case satisfies the
        # ``layered-app_builder`` import-linter contract
        # (application -> infrastructure is forbidden).
        export_run_markdown_use_case=ExportRunMarkdownUseCase(
            runs=runs,
            renderer=MarkdownRunExporter(),
        ),
        # PR-094 §17.5 #11 / #12 / #15 — wired collaborators.
        result_cache=result_cache,
        dep_checker=dep_checker,
        # V1 deps-status 逐 pack 进度 parity — descriptor provider backing
        # ``GET /deps-status/packs`` proactive probe scheduling.
        pack_dep_descriptors=pack_dep_descriptors_provider,
        inject_quality_score_use_case=inject_quality_score_uc,
        # S9 close — feedback + benchmark wiring (migration 011).
        feedback_repository=feedbacks,
        submit_feedback_use_case=SubmitFeedbackUseCase(
            runs=runs,
            feedbacks=feedbacks,
            clock=clock,
            ids=ids,
        ),
        benchmark_repository=benchmarks,
        run_benchmark_use_case=RunBenchmarkUseCase(
            run_app_use_case=run_app_uc,
            benchmarks=benchmarks,
            clock=clock,
            ids=ids,
        ),
        get_benchmark_use_case=GetBenchmarkUseCase(benchmarks=benchmarks),
        app_model_status_resolver=app_model_status_resolver,
        # R17 — single process-wide run-stream broadcaster.
        run_stream_broadcaster=RunStreamBroadcaster(),
        # R-3 — single process-wide background-task registry.
        background_tasks=TaskRegistry(),
        # V1 app-builder chat-prompt parity — SKILL path resolver + catalog.
        resolve_skill_files_use_case=resolve_skill_files_uc,
        generate_pack_catalog_use_case=generate_pack_catalog_uc,
        resolve_model_inference_code_use_case=resolve_model_inference_code_uc,
        download_model_weights_use_case=download_model_weights_uc,
        list_app_projects_use_case=list_app_projects_uc,
        get_app_project_use_case=get_app_project_uc,
        run_app_project_use_case=run_app_project_uc,
        stop_app_project_use_case=stop_app_project_uc,
        get_app_project_status_use_case=status_app_project_uc,
        get_app_project_logs_use_case=logs_app_project_uc,
        delete_app_project_use_case=delete_app_project_uc,
        package_app_project_use_case=package_app_project_uc,
        # WS dual-transport — single process-wide progress broadcaster.
        progress_broadcaster=ProgressBroadcaster(),
    )


# ---------------------------------------------------------------------------
# Pack root / manifest bootstrap helpers
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# Bundled App Builder Pack root relative to the repository root. V1 read
# the built-in models from ``features/app-builder/models/``; the v2.7
# install layout ships the identical assets under this path.
_DEFAULT_PACK_ROOT_REL = ("factory", "app_builder", "models")
_DEFAULT_SHARED_REL = ("factory", "app_builder", "shared")


def _resolve_app_builder_pack_root(container: "Container") -> Path | None:
    """Resolve the App Builder Pack root directory.

    Resolution order:

    1. ``container.app_builder_pack_root`` when explicitly injected
       (lifespan hook / test override) and it points at a real dir.
    2. ``<repo_root>/factory/app_builder/models`` — the bundled
       built-in Pack assets shipped with the v2.7 install layout.

    Returns ``None`` only when neither candidate exists, in which case
    callers keep the inert null fallbacks (empty registry / 503 manifest
    provider / null SKILL loader).
    """
    injected = getattr(container, "app_builder_pack_root", None)
    if isinstance(injected, Path) and injected.is_dir():
        return injected
    repo_root = getattr(container, "repo_root", None)
    if isinstance(repo_root, Path):
        candidate = repo_root.joinpath(*_DEFAULT_PACK_ROOT_REL)
        if candidate.is_dir():
            return candidate
    return None


def _pack_shared_pythonpath(container: "Container") -> tuple[Path, ...]:
    """Return the Pack ``shared/`` helper dir(s) to prepend to PYTHONPATH.

    V1 spawned runners with ``features/app-builder/shared/`` on the path
    so packs can ``import`` the shared helpers. The v2.7 install layout
    ships them under ``factory/app_builder/shared/``. Returns an
    empty tuple when no shared dir is available.
    """
    injected = getattr(container, "app_builder_shared_dir", None)
    if isinstance(injected, Path) and injected.is_dir():
        return (injected,)
    repo_root = getattr(container, "repo_root", None)
    if isinstance(repo_root, Path):
        candidate = repo_root.joinpath(*_DEFAULT_SHARED_REL)
        if candidate.is_dir():
            return (candidate,)
    return ()


#: Built-in Pack ``modelId`` values that are internal-only (edition-dual-form
#: -design.md §4.1). The internal edition bundles the model + assets; the
#: external edition neither bundles it nor has any download source, so it must
#: never appear in the external gallery / runner registry. ``ppocrv4`` (OCR) is
#: the only such Pack today. Gated at runtime in
#: :func:`build_app_builder_services` (layer 1 of the four-layer defence) and
#: physically removed from external artifacts via
#: ``scripts/release/manifest.toml [exclude]`` (layer 2).
_INTERNAL_ONLY_PACK_IDS: frozenset[str] = frozenset({"ppocrv4"})


def _filter_internal_only_packs(manifests, *, is_internal: bool):
    """Drop internal-only built-in Packs when running the external edition.

    Pure function (no I/O) so it is trivially unit-testable without building a
    full container. When ``is_internal`` is ``True`` the manifests pass through
    unchanged (the internal/dev edition ships every built-in Pack). When
    ``False`` (external edition), any manifest whose ``model_id`` is in
    :data:`_INTERNAL_ONLY_PACK_IDS` is removed so the gallery, manifest
    provider, runner registry and dep descriptors all uniformly omit it.

    Robust by design (hard constraint ②): filtering an already-absent Pack is a
    harmless no-op, so an external build that has physically stripped the Pack
    dir behaves identically — the OCR capability is simply, gracefully absent;
    nothing crashes.
    """
    if is_internal:
        return manifests
    return tuple(
        m
        for m in manifests
        if getattr(m, "model_id", None) not in _INTERNAL_ONLY_PACK_IDS
    )


def _read_pack_manifests(pack_root: Path | None):
    """Read every ``<pack_root>/<id>/manifest.json`` into PackManifests.

    Returns an empty tuple when ``pack_root`` is ``None`` / missing. Bad
    manifests are logged and skipped (non-strict) so one malformed pack
    cannot blank the whole gallery.
    """
    if pack_root is None:
        return ()
    try:
        return FileSystemManifestReader(strict=False).read_all(pack_root)
    except Exception:  # noqa: BLE001 — never let bootstrap abort startup
        _logger.warning(
            "app_builder.manifest_scan_failed", exc_info=True,
        )
        return ()


def _read_pack_manifests_union(
    *,
    builtin_root: Path | None,
    user_root: Path | None,
):
    """Merged manifest scan across the built-in + user Pack roots.

    Returns a pair ``(manifests, origin_by_id)`` where:

    * ``manifests`` — deterministic tuple of :class:`PackManifest` scanned
      from both roots, with built-in Packs taking precedence on
      ``model_id`` collision (release-contracted names win over user
      imports; the shadowed user manifest is logged as a warning and
      dropped from the tuple);
    * ``origin_by_id`` — sidecar ``{model_id: "built-in" | "user"}`` map
      the DI layer uses to pick the paired ``pack_root`` when driving
      per-origin adapters (e.g. the runner-registry bridge is called
      once per root; ``_on_pack_installed`` re-scans the user root only).

    Both ``builtin_root`` / ``user_root`` may be ``None`` (lean test
    container / no data_dir); the union degrades to whichever side is
    present, or ``((), {})`` when both are ``None`` — matching the
    legacy inert fallback.

    State-Truth-First §5 铁律 1: origin is derived from **which
    directory holds the manifest.json**, not from an in-process flag —
    if a Pack is physically moved between roots, the next scan sees the
    change.
    """
    manifests_by_id: dict[str, object] = {}
    origin_by_id: dict[str, str] = {}
    # Order matters: user-root FIRST, built-in SECOND so the built-in
    # entry (assigned second) overwrites the user entry on collision.
    # We log the shadowed user entry so operators can rename their import.
    if user_root is not None:
        for m in _read_pack_manifests(user_root):
            manifests_by_id[m.model_id] = m
            origin_by_id[m.model_id] = "user"
    if builtin_root is not None:
        for m in _read_pack_manifests(builtin_root):
            if m.model_id in origin_by_id:
                _logger.warning(
                    "app_builder.manifest_scan.id_collision: id=%s "
                    "(user Pack shadowed by built-in Pack — rename the "
                    "user import to keep it visible)",
                    m.model_id,
                )
            manifests_by_id[m.model_id] = m
            origin_by_id[m.model_id] = "built-in"
    # Deterministic order for downstream consumers (test snapshots etc.).
    ordered = tuple(
        manifests_by_id[k] for k in sorted(manifests_by_id.keys())
    )
    return ordered, origin_by_id


def _build_manifest_provider(pack_manifests, *, origin_by_id=None):
    """Build a refreshable ``AppModelId -> PackManifest | None`` lookup.

    The use cases that consume the provider pass an ``AppModelId`` value
    object; manifests carry the model id as a plain string. We index by
    string and accept either an ``AppModelId`` or a raw string at call
    time so the provider is robust to both call styles.

    Returns a :class:`_RefreshableManifestProvider`: it is callable (so it
    drops straight into every existing call site unchanged) AND exposes
    ``add(manifest)`` so the import-commit refresh callback can register a
    freshly-imported Pack's manifest at runtime (State-Truth-First: the
    runtime reflects what is really on disk now, not a startup-only snapshot).

    ``origin_by_id`` (P4): optional ``{model_id: "built-in" | "user"}`` map
    from :func:`_read_pack_manifests_union`. Consumers that need to pick the
    paired ``pack_root`` for a given model (e.g. runner-registry bridge)
    read ``provider.origin_of(model_id)``; when omitted the provider
    reports every entry as ``"built-in"`` (legacy behaviour).
    """
    return _RefreshableManifestProvider(
        pack_manifests, origin_by_id=origin_by_id
    )


class _RefreshableManifestProvider:
    """Callable manifest provider whose backing dict can be updated at runtime.

    Built once at startup from the scanned built-in manifests; the
    import-commit refresh callback calls :meth:`add` after a Pack is
    physically installed so ``GET /models/{id}/manifest`` + the gallery
    status probe + SKILL aggregation see the new model immediately.

    P4 origin tracking: each entry carries an ``origin`` label
    (``"built-in"`` for factory Packs, ``"user"`` for imported Packs)
    consumed by DI helpers that need to pick the paired ``pack_root`` /
    ``weights_root`` (e.g. per-origin runner-registry populate calls).
    """

    __slots__ = ("_by_id", "_origin_by_id")

    def __init__(self, pack_manifests, *, origin_by_id=None) -> None:  # noqa: ANN001
        self._by_id = {m.model_id: m for m in pack_manifests}
        # Default every entry to "built-in" when no origin map is provided
        # (legacy behaviour) — matches the pre-P4 layout where every scanned
        # Pack lived under the factory pack_root.
        base = dict(origin_by_id) if origin_by_id else {}
        self._origin_by_id: dict[str, str] = {
            mid: base.get(mid, "built-in") for mid in self._by_id
        }

    def __call__(self, model_id):  # noqa: ANN001
        key = getattr(model_id, "value", model_id)
        return self._by_id.get(key)

    def add(self, manifest, *, origin: str = "user") -> None:  # noqa: ANN001
        """Register / replace a manifest keyed by its ``model_id``.

        Fresh imports default to ``origin="user"`` (P4 default target
        anchor). Callers scanning the built-in factory tree should pass
        ``origin="built-in"`` explicitly.
        """
        self._by_id[manifest.model_id] = manifest
        self._origin_by_id[manifest.model_id] = origin

    def remove(self, model_id) -> None:  # noqa: ANN001
        """Drop the manifest entry for ``model_id`` (idempotent).

        Symmetric counterpart to :meth:`add` (P2 / Sub-A runtime cache
        invalidation). Satisfies :class:`AppModelManifestProviderPort.remove`.
        Accepts either an :class:`AppModelId` value object or a raw string
        so a caller that already holds the string id (post-delete) does
        not have to reconstruct the VO. Missing keys are silent no-ops.
        """
        key = getattr(model_id, "value", model_id)
        if self._by_id.pop(key, None) is not None:
            self._origin_by_id.pop(key, None)
            _logger.info(
                "app_builder.manifest_provider.remove: id=%s (runtime cache cleared)",
                key,
            )

    def origin_of(self, model_id) -> str | None:  # noqa: ANN001
        """Return ``"built-in"`` / ``"user"`` / ``None`` for a model id.

        ``None`` when the id is unknown. Consumers use this to pick the
        paired ``pack_root`` / ``weights_root`` (e.g. the runner-registry
        bridge, the status resolver).
        """
        key = getattr(model_id, "value", model_id)
        return self._origin_by_id.get(key)


# Reverse of V1 ``LEGACY_CATEGORY_MAP`` (backend/app_builder/taxonomy.py):
# map a ``group/task`` taxonomy pair to the short category code V1 showed on
# the gallery card badge. Mirrors the frontend ``CATEGORY_CODE_BY_TAXONOMY``
# so the wire ``category`` and the client-derived fallback agree.
_CATEGORY_CODE_BY_TAXONOMY: dict[str, str] = {
    "audio/speech-recognition": "ASR",
    "audio/audio-generation": "TTS",
    "computer-vision/ocr": "OCR",
    "computer-vision/super-resolution": "SR",
    "generative-ai/text-generation": "LLM",
}


def _derive_category(segments: tuple[str, ...]) -> str | None:
    """V1-parity category badge derived from a taxonomy path.

    Prefers the short legacy code for a known ``group/task`` pair; falls back
    to the last (most specific) segment so the badge is never empty.
    """
    if not segments:
        return None
    if len(segments) >= 2:
        code = _CATEGORY_CODE_BY_TAXONOMY.get(f"{segments[0]}/{segments[1]}")
        if code is not None:
            return code
    return segments[-1] or None


def _deps_status_token(dep_checker, model_id: str) -> str | None:  # noqa: ANN001
    """Map the dependency checker's cached status onto the V1 wire token.

    Returns ``installing`` / ``missing`` / ``ready`` (V1 ``depsStatus``
    semantics) or ``None`` when the pack has not been probed yet (frontend
    treats ``None`` as "checking / unknown" — same as V1 omitting the field).
    """
    if dep_checker is None:
        return None
    getter = getattr(dep_checker, "get_status", None)
    if getter is None:
        return None
    status = getter(model_id)
    if status is None:
        return None
    if getattr(status, "installing", False):
        return "installing"
    if not getattr(status, "satisfied", True):
        return "missing"
    return "ready"


def _build_status_resolver(
    *,
    manifest_provider,  # noqa: ANN001 — Callable[[AppModelId], PackManifest|None]
    pack_root: "Path | None",
    repo_root: "Path | None",
    user_pack_root: "Path | None" = None,
    user_weights_root: "Path | None" = None,
    dep_checker,  # noqa: ANN001 — DynamicPackDepChecker | None
) -> "Callable[[AppModelDefinition], AppModelStatusInfo]":
    """Build the ``AppModelDefinition -> AppModelStatusInfo`` resolver.

    Combines the V1 weight-presence probe
    (:func:`qai.app_builder.domain.model_status.detect_status`) with the
    dependency checker's cached status so the gallery badge matches V1's
    ``GET /api/appbuilder/models`` augmented rows. Best-effort: any failure
    to read the manifest / probe disk degrades to the default ``Ready``
    status rather than failing the whole listing.

    P4 dual-root: ``user_pack_root`` / ``user_weights_root`` extend the
    weights probe so a Pack living under EITHER anchor counts as
    installed; ``auto_download`` still reads ``runner.py`` under whichever
    of the two anchors physically holds the pack.
    """

    def _auto_downloads(model: AppModelDefinition) -> bool:
        """Does this built-in pack fetch its own weights on the first Run?

        State-Truth-First (AGENTS §5): rather than a hardcoded id allow-list,
        we observe the ACTUAL pack — a built-in whose ``runner.py`` calls the
        shared ``ensure_weights_downloaded`` helper will auto-download on the
        first Run (URLs are hardcoded in the runner, not the manifest, and the
        manifest ``weightsUrl`` is unreliable for this). User imports never
        auto-download; packs that omit the helper (e.g. ppocrv4, which needs a
        manual conversion/import) are correctly excluded. Read-only text probe,
        no import of the runner (which runs as an isolated subprocess).

        P4: probe the built-in anchor only (user-imported packs already
        return early on the ``model.user_imported`` guard). Auto-download
        is a built-in-only capability.
        """
        if model.user_imported or pack_root is None:
            return False
        runner_path = pack_root / str(model.id) / "runner.py"
        try:
            text = runner_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return "ensure_weights_downloaded" in text

    def _resolve(model: AppModelDefinition) -> AppModelStatusInfo:
        segments = tuple(model.taxonomy.segments)
        category = _derive_category(segments)
        icon = (
            _taxonomy_tree.group_icon(segments[0]) if segments else None
        )
        deps_status = _deps_status_token(dep_checker, str(model.id))
        auto_download = _auto_downloads(model)

        manifest = None
        try:
            manifest = manifest_provider(model.id)
        except Exception:  # noqa: BLE001 — manifest read must never break listing
            manifest = None

        if manifest is None or pack_root is None:
            # No manifest / pack root → fall back to the DB ``enabled`` flag
            # as a coarse "runnable?" proxy (V2 sets enabled=False when the
            # weights are known-missing at import time).
            status = "Ready" if model.enabled else "NotInstalled"
            return AppModelStatusInfo(
                status=status,
                deps_status=deps_status,
                category=category,
                icon=icon,
                auto_download=auto_download,
            )

        probe = _FileSystemWeightsPresence(
            pack_root=pack_root,
            repo_root=repo_root,
            user_pack_root=user_pack_root,
            user_weights_root=user_weights_root,
        )
        status = _model_status.detect_status(manifest, probe=probe)
        variant_rows = _model_status.detect_variant_status(
            manifest, probe=probe
        )
        variant_status = (
            tuple(
                VariantStatusView(id=v.id, status=v.status)
                for v in variant_rows
            )
            if variant_rows is not None
            else ()
        )
        return AppModelStatusInfo(
            status=status,
            deps_status=deps_status,
            variant_status=variant_status,
            category=category,
            icon=icon,
            auto_download=auto_download,
        )

    return _resolve
