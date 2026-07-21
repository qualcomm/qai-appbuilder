# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``model_catalog`` bounded context.

PR-032 (S3) injected eight ``_Fake<Port>`` in-memory adapters here;
PR-044 (S4) replaces all eight with real adapters and adds two missing
use cases (issue f decision A):

* :class:`qai.model_catalog.adapters.SqliteModelEntryRepository`
* :class:`qai.model_catalog.adapters.SqliteDownloadJobRepository`
* :class:`qai.model_catalog.adapters.SqliteModelSkillRegistry`
* :class:`qai.model_catalog.adapters.SqliteProviderRegistry`
* :class:`qai.model_catalog.adapters.Hash256ChecksumVerifier`
* :class:`qai.model_catalog.infrastructure.Aria2cDownloadEngine`
* :class:`qai.model_catalog.infrastructure.HttpReleaseManifestFetcher`
* :class:`qai.model_catalog.infrastructure.FileSystemBlobStore`

Three new fields appear on :class:`ModelCatalogServices`:

* ``model_entry_repository`` / ``download_job_repository`` /
  ``model_skill_registry`` / ``provider_registry`` /
  ``checksum_verifier`` / ``manifest_fetcher`` / ``download_engine`` /
  ``blob_store`` — raw ports exposed for tests and adapters that need
  direct access (read-only inspection from routes is still discouraged
  per PR-032 §11; routes call use cases). PR-040 established the
  precedent for exposing raw ports on the namespace dataclass alongside
  use cases.
* ``list_download_jobs_use_case`` — wraps
  :meth:`DownloadJobRepositoryPort.list_active` (issue f).
* ``remove_version_use_case`` — wraps
  :meth:`ModelEntry.remove_version` (issue f).

Existing :class:`ModelCatalogServices` field names are part of the
public route contract (PR-032 §11) and have NOT been changed.

Cross-PR coordination
---------------------

* ``Aria2cDownloadEngine`` accepts an optional process runner; PR-041
  may eventually expose ``container.process_runner`` (a
  :class:`qai.platform.process.ProcessRunnerPort`). At the time of
  this PR's merge ``qai.platform.process`` exists but its
  ``ProcessRunnerPort`` is a Protocol stub — we therefore lazy-import
  to avoid a hard dependency, and fall back to an internal
  ``asyncio.create_subprocess_exec`` runner when the import fails or
  the container has no ``process_runner`` field.
* ``SqliteModelSkillRegistry`` is the canonical skill registry. PR-046
  (ai_coding) will compose a cross-context skill view at the apps
  layer; this adapter remains context-isolated (no ai_coding imports).

Import discipline
-----------------
Top-level adapters import is allowed because the
``interfaces-stays-thin`` contract uses ``allow_indirect_imports = True``
(set in PR-040): routes reach this module transitively via
``apps.api.di``, but only DIRECT
``interfaces.http -> qai.*.adapters`` edges are forbidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.model_catalog.adapters import (
    Hash256ChecksumVerifier,
    SqliteDownloadJobRepository,
    SqliteModelEntryRepository,
    SqliteModelSkillRegistry,
    SqliteProviderRegistry,
)
from qai.model_catalog.application.ports import (
    BlobStorePort,
    ChecksumVerifierPort,
    DownloadEnginePort,
    DownloadJobRepositoryPort,
    ManifestFetcherPort,
    ModelEntryRepositoryPort,
    ProviderRegistryPort,
    SkillRegistryPort,
)
from qai.model_catalog.application.use_cases import (
    CancelDownloadUseCase,
    GetModelEntryUseCase,
    InMemoryPermissionSnapshotStore,
    ListCloudModelsUseCase,
    ListDownloadJobsUseCase,
    ListModelEntriesUseCase,
    ListProviderConfigsUseCase,
    PermissionSnapshotStore,
    ProbeCloudModelPermissionsUseCase,
    ProbeProviderUseCase,
    RefreshReleaseManifestUseCase,
    RegisterModelEntryUseCase,
    RemoveModelEntryUseCase,
    RemoveVersionUseCase,
    StartDownloadUseCase,
    StreamDownloadProgressUseCase,
    UpdateProviderConfigUseCase,
    VerifyChecksumUseCase,
)
from qai.model_catalog.infrastructure import (
    FileSystemBlobStore,
    HttpProviderProbe,
    HttpReleaseManifestFetcher,
)
from qai.platform.download import (
    Aria2cDownloadEngine,
    Aria2cRpcDownloadEngine,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = [
    "ModelCatalogServices",
    "build_model_catalog_services",
]


# Default release manifest URL. Mirrored here as a fallback for
# hand-rolled containers that pass a stripped-down ``settings`` object
# without the ``model_catalog`` namespace; production wiring reads the
# value from ``container.settings.model_catalog.release_manifest_url``
# (S9 PR-097 promoted this constant to ``ModelCatalogSettings`` in
# :mod:`qai.platform.config.settings`).
_DEFAULT_RELEASE_MANIFEST_URL = (
    "https://qai.example.com/release-manifest.json"
)


@dataclass(slots=True)
class ModelCatalogServices:
    """Application services for the ``model_catalog`` bounded context.

    Holds the 11 PR-032 use cases (preserved verbatim) plus the two
    PR-044 additions (issue f) and the eight raw port instances behind
    them. Field-name compatibility note (PR-032 §11): every PR-032 use
    case field is retained verbatim. PR-044 only **adds** the eight
    port fields and the two new use case fields.
    """

    # repositories / engines (raw ports — additive, PR-044 / issue f
    # convenience for tests + cross-context composition by PR-046)
    model_entry_repository: ModelEntryRepositoryPort
    download_job_repository: DownloadJobRepositoryPort
    model_skill_registry: SkillRegistryPort
    provider_registry: ProviderRegistryPort
    checksum_verifier: ChecksumVerifierPort
    manifest_fetcher: ManifestFetcherPort
    download_engine: DownloadEnginePort
    blob_store: BlobStorePort
    # entries (PR-032)
    register_model_entry_use_case: RegisterModelEntryUseCase
    list_model_entries_use_case: ListModelEntriesUseCase
    get_model_entry_use_case: GetModelEntryUseCase
    remove_model_entry_use_case: RemoveModelEntryUseCase
    remove_version_use_case: RemoveVersionUseCase  # NEW (PR-044 / issue f)
    # downloads (PR-032)
    start_download_use_case: StartDownloadUseCase
    cancel_download_use_case: CancelDownloadUseCase
    stream_download_progress_use_case: StreamDownloadProgressUseCase
    list_download_jobs_use_case: ListDownloadJobsUseCase  # NEW (PR-044 / issue f)
    # checksum + manifest (PR-032)
    verify_checksum_use_case: VerifyChecksumUseCase
    refresh_release_manifest_use_case: RefreshReleaseManifestUseCase
    # provider configs (PR-032)
    list_provider_configs_use_case: ListProviderConfigsUseCase
    update_provider_config_use_case: UpdateProviderConfigUseCase
    # cloud-inference catalog (functional block 2 — additive tail field)
    list_cloud_models_use_case: ListCloudModelsUseCase
    # provider connectivity probe (additive tail field — config wizard /
    # ``qai config provider test``; HTTP+CLI shareable)
    probe_provider_use_case: ProbeProviderUseCase
    # Cloud-model permission snapshot (additive tail fields — scanned once
    # from lifespan; the chat dropdown reads the snapshot to hide models
    # the user's API key has no access to. Store is an in-memory dict;
    # ``PermissionStatus.UNKNOWN`` is the safe default so a scan that has
    # not yet run / has failed keeps every model visible.
    permission_snapshot_store: PermissionSnapshotStore
    probe_cloud_model_permissions_use_case: ProbeCloudModelPermissionsUseCase


def build_model_catalog_services(
    container: "Container",
) -> ModelCatalogServices:
    """Wire ``container.model_catalog`` with real PR-044 adapters.

    Uses ``container.{database, clock, ids, events, data_paths,
    settings}`` rather than constructing fresh primitives so tests
    injecting a ``FrozenClock`` via ``Container`` see the same clock
    everywhere.

    The download engine defaults to :class:`Aria2cRpcDownloadEngine`
    (F-13 / GAP-PR-E1), which drives a daemon-mode aria2c via JSON-RPC
    and produces real incremental ``DownloadProgress`` frames.  It
    transparently falls back to the legacy
    :class:`Aria2cDownloadEngine` (single-frame mode) when the binary
    is unavailable or the daemon fails to start, so this DI change
    introduces no new failure mode for environments without aria2c.
    """
    db = container.database
    clock = container.clock
    ids = container.ids
    events = container.events
    data_paths = container.data_paths

    # ── Repositories / registries ────────────────────────────────────
    entry_repo = SqliteModelEntryRepository(db=db, clock=clock)
    job_repo = SqliteDownloadJobRepository(db=db)
    skill_registry = SqliteModelSkillRegistry(db=db)
    provider_registry = SqliteProviderRegistry(db=db, clock=clock)

    # ── Infrastructure / non-DB adapters ─────────────────────────────
    blob_store = FileSystemBlobStore.from_data_paths(data_paths)
    verifier = Hash256ChecksumVerifier(resolver=blob_store)

    manifest_url = _resolve_manifest_url(container)
    # 缺口 7 — route the release-manifest fetch through the mechanism-B global
    # proxy (it is a "file download" class request). The provider reads the
    # live ``settings.tools.global_proxy`` (+ embedded auth) at call time so a
    # runtime-config edit hot-applies; ``None`` → direct connection.
    from apps.api._global_proxy import (
        build_global_proxy_provider,
        build_ssl_verify_provider,
    )

    manifest_fetcher = HttpReleaseManifestFetcher(
        url=manifest_url,
        clock=clock,
        proxy_provider=build_global_proxy_provider(container),
        # 缺口 fix — route the (previously hardcoded verify=False) manifest fetch
        # through the live global Settings.ssl_verify toggle (read per fetch).
        ssl_verify_provider=build_ssl_verify_provider(container),
    )

    engine = Aria2cRpcDownloadEngine(
        fallback_engine=Aria2cDownloadEngine(
            process_runner=getattr(container, "process_runner", None),
        ),
        process_runner=getattr(container, "process_runner", None),
    )

    # ── Use cases (PR-032 + 2 new from PR-044) ───────────────────────
    register_entry = RegisterModelEntryUseCase(
        repository=entry_repo,
        event_bus=events,
    )
    list_entries = ListModelEntriesUseCase(repository=entry_repo)
    get_entry = GetModelEntryUseCase(repository=entry_repo)
    remove_entry = RemoveModelEntryUseCase(
        repository=entry_repo,
        event_bus=events,
    )
    remove_version = RemoveVersionUseCase(
        repository=entry_repo,
        event_bus=events,
    )
    start_download = StartDownloadUseCase(
        entry_repository=entry_repo,
        job_repository=job_repo,
        engine=engine,
        ids=ids,
        clock=clock,
        event_bus=events,
    )
    cancel_download = CancelDownloadUseCase(
        job_repository=job_repo,
        engine=engine,
        clock=clock,
        event_bus=events,
    )
    stream_progress = StreamDownloadProgressUseCase(
        job_repository=job_repo,
        engine=engine,
        clock=clock,
        event_bus=events,
    )
    list_jobs = ListDownloadJobsUseCase(job_repository=job_repo)
    verify_checksum = VerifyChecksumUseCase(
        entry_repository=entry_repo,
        verifier=verifier,
        event_bus=events,
    )
    refresh_manifest = RefreshReleaseManifestUseCase(
        fetcher=manifest_fetcher,
        entry_repository=entry_repo,
        event_bus=events,
    )
    list_providers = ListProviderConfigsUseCase(
        registry=provider_registry,
        # Inject the SecretStore so the provider list can report a per-provider
        # ``has_api_key`` flag (presence only — never the value). The UI uses
        # it to prompt for a key when a provider ships models but has none yet
        # (internal-edition qgenie first-launch). Same namespace as the chat
        # read path in ``_model_resolver_bridge.py``.
        secret_store=getattr(container, "secret_store", None),
    )
    update_provider = UpdateProviderConfigUseCase(
        registry=provider_registry,
        # api_key is extracted into the SecretStore (V1 parity, §3.3) instead
        # of being written plaintext into ``kv_user_prefs``; the namespace
        # matches the chat read path in ``_model_resolver_bridge.py``.
        secret_store=getattr(container, "secret_store", None),
    )
    list_cloud_models = ListCloudModelsUseCase(registry=provider_registry)
    probe_provider = ProbeProviderUseCase(
        registry=provider_registry,
        # 缺口 fix — route the (previously hardcoded verify=False) connectivity
        # probe through the live global Settings.ssl_verify toggle.
        probe=HttpProviderProbe(
            ssl_verify_provider=build_ssl_verify_provider(container)
        ),
        secret_store=getattr(container, "secret_store", None),
    )

    # ── Cloud-model permission snapshot ──────────────────────────────
    # Shared in-memory store (process-lifetime). Reused across the scan use
    # case (writer, driven by lifespan) and the HTTP route (reader). Reset
    # on process restart by design — permission state is never persisted.
    permission_store = InMemoryPermissionSnapshotStore()
    probe_permissions = ProbeCloudModelPermissionsUseCase(
        registry=provider_registry,
        # Reuse the same HttpProviderProbe (one ``GET /v1/models`` per provider)
        # so the permission scan piggybacks on the connectivity probe's
        # SSL-verify wiring + timeout.
        probe=HttpProviderProbe(
            ssl_verify_provider=build_ssl_verify_provider(container)
        ),
        store=permission_store,
        secret_store=getattr(container, "secret_store", None),
    )

    return ModelCatalogServices(
        # raw ports
        model_entry_repository=entry_repo,
        download_job_repository=job_repo,
        model_skill_registry=skill_registry,
        provider_registry=provider_registry,
        checksum_verifier=verifier,
        manifest_fetcher=manifest_fetcher,
        download_engine=engine,
        blob_store=blob_store,
        # use cases (PR-032 order preserved)
        register_model_entry_use_case=register_entry,
        list_model_entries_use_case=list_entries,
        get_model_entry_use_case=get_entry,
        remove_model_entry_use_case=remove_entry,
        remove_version_use_case=remove_version,
        start_download_use_case=start_download,
        cancel_download_use_case=cancel_download,
        stream_download_progress_use_case=stream_progress,
        list_download_jobs_use_case=list_jobs,
        verify_checksum_use_case=verify_checksum,
        refresh_release_manifest_use_case=refresh_manifest,
        list_provider_configs_use_case=list_providers,
        update_provider_config_use_case=update_provider,
        list_cloud_models_use_case=list_cloud_models,
        probe_provider_use_case=probe_provider,
        permission_snapshot_store=permission_store,
        probe_cloud_model_permissions_use_case=probe_permissions,
    )


def _resolve_manifest_url(container: "Container") -> str:
    """Resolve the release manifest URL.

    Production wiring reads
    ``container.settings.model_catalog.release_manifest_url`` directly
    (the typed :class:`ModelCatalogSettings` namespace shipped in
    S9 PR-097).  The defensive ``getattr`` chain below covers
    hand-rolled test containers that construct a stripped-down
    ``settings`` object without the ``model_catalog`` namespace; in
    that case the bundled default constant is used.
    """
    settings = getattr(container, "settings", None)
    model_catalog = getattr(settings, "model_catalog", None)
    if model_catalog is not None:
        url = getattr(model_catalog, "release_manifest_url", None)
        if isinstance(url, str) and url:
            return url
    return _DEFAULT_RELEASE_MANIFEST_URL
