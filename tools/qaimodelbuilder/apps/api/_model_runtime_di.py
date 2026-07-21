# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``model_runtime`` bounded context.

PR-604 (L6) introduces the BC with a :class:`StubInferenceService`
adapter.  The use cases cover start/stop/probe/status/load_model/
get_logs/clear_logs/list_models/open_service_dir of the local inference
daemon (GenieAPIService), plus the GenieAPIService ``service_config.json``
read/write surface and the live SSE log frame stream.

Production mode always uses :class:`ProcessBackedInferenceService`,
which spawns the real ``GenieAPIService.exe`` subprocess. Its install dir
is resolved *live* from ``forge_config.genie_service.root_path`` on each
status/start (V1 ``_build_service_exe_path`` parity), so a binary installed
after server start is discovered without a restart; when the binary is
absent the adapter reports an empty ``exe_path`` ("not installed"), exactly
like V1. :class:`StubInferenceService` is used **only** for minimal test
containers that lack ``data_paths`` — never as a production fallback.

Import discipline
-----------------
This module imports from ``qai.model_runtime.adapters``,
``qai.model_runtime.infrastructure``, ``qai.model_runtime.application`` and
the ``qai.platform`` shared kernel only — no cross-BC imports. The
``forge.config`` reads (``genie_service.root_path`` /
``service_launch.models_root_path``) are obtained through the live
``container.user_prefs`` namespace and exposed to the use cases as opaque
async callables, so no ``import qai.user_prefs`` ever leaks in.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qai.model_runtime.adapters import StubInferenceService
from qai.model_runtime.application.ports import (
    InferenceServicePort,
    ServiceConfigRepositoryPort,
)
from qai.model_runtime.application.use_cases import (
    ClearLogsUseCase,
    GetLogsUseCase,
    GetServiceConfigUseCase,
    GetStatusUseCase,
    ListModelsUseCase,
    LoadModelUseCase,
    OpenServiceDirUseCase,
    ProbeServiceUseCase,
    SaveServiceConfigUseCase,
    StartServiceUseCase,
    StopServiceUseCase,
    StreamLogFramesUseCase,
    StreamLogsUseCase,
)
from qai.model_runtime.infrastructure import (
    FileServiceConfigRepository,
    ProcessBackedInferenceService,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = logging.getLogger(__name__)

__all__ = [
    "ModelRuntimeServices",
    "build_model_runtime_services",
]


@dataclass(slots=True)
class ModelRuntimeServices:
    """Application services for the ``model_runtime`` bounded context.

    Holds the inference adapter port, the service-config repository port and
    the use cases for controlling the local inference daemon + its config.
    """

    # raw ports (exposed for tests that need direct access)
    inference_service: InferenceServicePort
    service_config_repository: ServiceConfigRepositoryPort | None

    # use cases
    start_service_use_case: StartServiceUseCase
    stop_service_use_case: StopServiceUseCase
    probe_service_use_case: ProbeServiceUseCase
    get_status_use_case: GetStatusUseCase
    load_model_use_case: LoadModelUseCase
    get_logs_use_case: GetLogsUseCase
    clear_logs_use_case: ClearLogsUseCase
    stream_logs_use_case: StreamLogsUseCase
    stream_log_frames_use_case: StreamLogFramesUseCase
    list_models_use_case: ListModelsUseCase
    open_service_dir_use_case: OpenServiceDirUseCase
    # Service-config CRUD. ``None`` when the container lacks the platform
    # ports needed to build the repository (minimal test containers that
    # only mount the daemon-control routes).
    get_service_config_use_case: GetServiceConfigUseCase | None
    save_service_config_use_case: SaveServiceConfigUseCase | None


def _read_forge_config(container: "Container") -> dict:
    """Synchronously read forge_config.json from the data root.

    The ``model_runtime`` adapter selection (Process vs Stub) happens at
    build time and is synchronous, so it cannot await the async
    ``user_prefs`` document reader used elsewhere. This DI-layer helper
    reads the same forge_config.json that ``service_release`` writes
    (``data/config/forge_config.json``) so the install dir resolved from
    ``genie_service.root_path`` (V1 ``main.py:5205-5211``) is honoured.
    Any failure yields ``{}`` — the caller then falls back to the Stub.
    """
    data_paths = getattr(container, "data_paths", None)
    root = getattr(data_paths, "root", None)
    if root is None:
        return {}
    cfg_path = Path(root) / "config" / "forge_config.json"
    if not cfg_path.is_file():
        return {}
    try:
        text = cfg_path.read_text(encoding="utf-8-sig")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _resolve_install_dir(container: "Container", configured: str) -> str:
    """Resolve the GenieAPIService install dir (V1 parity).

    Prefers an explicit ``settings.model_runtime.install_dir`` (server.toml /
    env override). When empty — the common case — falls back to
    ``forge_config.genie_service.root_path`` (V1's source of truth, kept up
    to date by the local-status auto-configure). Returns ``""`` when neither
    is set, which selects the Stub adapter.
    """
    if configured:
        return configured
    forge = _read_forge_config(container)
    gs = forge.get("genie_service", {})
    if isinstance(gs, dict):
        return str(gs.get("root_path", "") or "").strip()
    return ""


def _resolve_default_models_root(container: "Container") -> str:
    """Default models root = ``<data>/models`` (V1 parity).

    V1's ``forge_config_manager`` falls back ``models_root_path`` to
    ``<webui>/models`` when unset; V2's data-root equivalent is
    ``data/models``. Returned as the adapter's static ``models_root`` so the
    live ``service_launch.models_root_path`` provider still overrides it when
    the operator sets one.
    """
    data_paths = getattr(container, "data_paths", None)
    root = getattr(data_paths, "root", None)
    if root is None:
        return ""
    # Absolute: GenieAPIService.exe runs with cwd = its own bin dir, so a
    # relative ``-c data/models/...`` config path would resolve against the
    # bin dir and fail. V1 stored an absolute models_root for this reason.
    return str((Path(root) / "models").resolve())


def _make_install_dir_provider(
    container: "Container",
) -> Callable[[], str]:
    """Build a sync callable returning the live GenieAPIService install dir.

    Reads ``forge_config.genie_service.root_path`` (V1's source of truth,
    kept current by the local-status auto-configure) on each call so the
    binary is discoverable the instant it is installed — no API restart
    needed (V1 ``_build_service_exe_path`` parity). Empty when unset.

    The stored value may be *relative* (V2 writes ``data\\bin\\...``); V1
    always stored an **absolute** path (``WEBUI_DIR/bin/...``). A relative
    install dir makes ``GenieAPIService.exe`` spawn with a relative
    ``argv[0]`` + relative ``cwd``, so the daemon doubles its internal
    "root dir" (``cwd + data/bin/...``), fails to load
    ``service_config.json`` and falls back to defaults that leak progress
    lines into ``delta.content``. Resolve any relative path against the
    project root (``<data>/..``) so the path handed to the daemon is always
    absolute — V1 parity.
    """
    data_paths = getattr(container, "data_paths", None)
    data_root = getattr(data_paths, "root", None)
    project_root = Path(data_root).parent if data_root is not None else None
    bin_dir = Path(data_root) / "bin" if data_root is not None else None

    def _resolve_abs(raw: str) -> str:
        p = Path(raw)
        if p.is_absolute():
            return str(p)
        base = project_root if project_root is not None else Path.cwd()
        return str((base / p).resolve())

    def _exe_present(install_dir: str) -> bool:
        if not install_dir:
            return False
        return (Path(install_dir) / "GenieAPIService.exe").is_file()

    def _scan_bin_for_install() -> str:
        """Newest ``data/bin/<...>`` dir that actually contains the exe.

        Self-heal for a stale ``root_path`` (e.g. it points at a version the
        user has since deleted, while a different version is installed): the
        Service panel must reflect the REAL disk state, not a dead pointer
        (truth-from-real-state). Returns "" when nothing is installed.
        """
        if bin_dir is None or not bin_dir.is_dir():
            return ""
        candidates = [
            child
            for child in bin_dir.iterdir()
            if child.is_dir()
            and not child.name.endswith(".bak")
            and (child / "GenieAPIService.exe").is_file()
        ]
        if not candidates:
            return ""
        # Newest by mtime (good enough; avoids re-implementing version sort).
        candidates.sort(key=lambda c: c.stat().st_mtime, reverse=True)
        return str(candidates[0])

    def _provider() -> str:
        forge = _read_forge_config(container)
        gs = forge.get("genie_service", {})
        configured = ""
        if isinstance(gs, dict):
            raw = str(gs.get("root_path", "") or "").strip()
            if raw:
                configured = _resolve_abs(raw)
        # Happy path: configured root_path points at a real binary.
        if _exe_present(configured):
            return configured
        # Self-heal: configured path is empty or stale (binary missing) →
        # discover the real install by scanning data/bin. This fixes the
        # "Download Center shows installed but Service panel says not
        # installed" mismatch caused by root_path pointing at a deleted
        # version directory.
        healed = _scan_bin_for_install()
        if healed:
            return healed
        # Nothing installed anywhere: return the configured value (possibly
        # empty) so callers behave exactly as before.
        return configured

    return _provider


async def _load_service_launch(container: "Container") -> dict:
    """Return forge.config ``service_launch`` dict, or empty on failure."""
    user_prefs = getattr(container, "user_prefs", None)
    load_uc = getattr(user_prefs, "load_document_use_case", None)
    if load_uc is None:
        return {}
    try:
        doc = await load_uc.execute("forge.config")
        service_launch = doc.get("service_launch", {})
        if isinstance(service_launch, dict):
            return service_launch
    except (ValueError, TypeError, AttributeError, KeyError):
        return {}
    return {}


def _make_models_root_provider(
    container: "Container",
) -> Callable[[], Awaitable[str]]:
    """Build an async callable returning the active models-scan root.

    Design simplification (user mandate 2026-06-07): the models install/scan
    root is **fixed** to the default ``<data>/models`` and is no longer a
    user-settable path. The Service Config "models directory" input has been
    removed from the UI, so there is no longer a way to populate
    ``service_launch.models_root_path``; this provider therefore always yields
    ``""`` so the adapter / :class:`GetStatusUseCase` fall back to the static
    default (``_resolve_default_models_root`` → ``<data>/models``).

    Returning ``""`` (rather than reading the persisted value) makes the fixed
    default authoritative even if a *stale* ``models_root_path`` lingers in an
    existing ``forge_config.json`` from before this simplification — the daemon
    is always launched with the default models root. Injected into the adapter
    (``-c`` flag selection) and :class:`GetStatusUseCase` (unsafe-path warning)
    without importing ``qai.user_prefs`` directly.
    """

    async def _provider() -> str:
        # Fixed default: never honour a persisted ``models_root_path`` override.
        return ""

    return _provider


def _make_loglevel_provider(
    container: "Container",
) -> Callable[[], Awaitable[int]]:
    """Build an async callable returning ``service_launch.loglevel``.

    Injected into :class:`ProcessBackedInferenceService` so its V1-parity
    ``-d`` CLI flag honours the user's forge.config value
    (``main.py:1760`` / ``main.py:5373`` / ``main.py:5266``). Falls back
    to V1 default ``3`` when the document is missing / malformed / out of
    range. ``service_launch.loglevel`` accepts the V1 1..5 range; values
    outside that range collapse to ``3`` so a typoed config can never
    spawn the daemon with garbage.
    """

    async def _provider() -> int:
        service_launch = await _load_service_launch(container)
        raw = service_launch.get("loglevel")
        if raw is None:
            return 3
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 3
        if value < 1 or value > 5:
            return 3
        return value

    return _provider


def _make_port_provider(
    container: "Container",
) -> Callable[[], Awaitable[int | None]]:
    """Build an async callable returning ``service_launch.local_port``.

    Injected into :class:`ProcessBackedInferenceService` so its real-state
    status probe (`status()` HTTP fallback) checks the port the USER actually
    configured in the Service page (``service_launch.local_port``, e.g. 9999),
    not the built-in ``default_port`` (8000). Without this, a daemon running
    on the configured port is invisible to the probe and the UI shows
    "Stopped" while the service is alive (the exact reported bug). Returns
    ``None`` when unset so the adapter falls back to ``default_port``.
    """

    async def _provider() -> int | None:
        service_launch = await _load_service_launch(container)
        raw = service_launch.get("local_port")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    return _provider


def _make_genie_root_provider(
    container: "Container",
) -> Callable[[], Awaitable[str]]:
    """Build an async callable returning ``genie_service.root_path``.

    Injected into the service-config use cases so they can resolve the
    active config path without importing ``qai.user_prefs``. Reads through
    the live ``container.user_prefs`` namespace; any failure yields ``""``.
    """

    async def _provider() -> str:
        user_prefs = getattr(container, "user_prefs", None)
        load_uc = getattr(user_prefs, "load_document_use_case", None)
        if load_uc is None:
            return ""
        try:
            forge_doc = await load_uc.execute("forge.config")
            gs = forge_doc.get("genie_service", {})
            if isinstance(gs, dict):
                return str(gs.get("root_path", "") or "").strip()
        except Exception:  # noqa: BLE001 — convenience read; never fatal
            return ""
        return ""

    return _provider


def build_model_runtime_services(
    container: "Container",
) -> ModelRuntimeServices:
    """Wire ``container.model_runtime`` with the production adapter.

    Always uses :class:`ProcessBackedInferenceService` — the real adapter
    that spawns ``GenieAPIService.exe`` — mirroring V1, which has no
    "stub" daemon. The binary's install dir is resolved *live* from
    ``forge_config.genie_service.root_path`` on each status/start, so a
    binary installed after server start is discovered without a restart
    (V1 ``_build_service_exe_path`` parity); when the binary is absent the
    adapter simply reports an empty ``exe_path`` ("not installed"), exactly
    like V1.

    :class:`StubInferenceService` is used **only** for minimal test
    containers that lack ``data_paths`` (and therefore cannot resolve
    forge_config) — never as a production fallback.

    Uses ``container.settings`` to resolve the default port (falling back
    to sensible defaults when the settings section is absent).
    """
    settings = getattr(container, "settings", None)
    model_runtime_cfg = getattr(settings, "model_runtime", None)
    # PR-095 / S9 A-26: ProcessBackedInferenceService now honours
    # ``settings.service.log_buffer_size`` so operators can tune the
    # retained log line count without code changes.  Falls back to the
    # adapter's compiled default when the section is absent (e.g. test
    # harnesses that build a partial Settings).
    service_cfg = getattr(settings, "service", None)
    log_buffer_size: int = int(getattr(service_cfg, "log_buffer_size", 1000))

    install_dir: str = _resolve_install_dir(
        container, getattr(model_runtime_cfg, "install_dir", "")
    )
    default_port: int = getattr(model_runtime_cfg, "default_port", 8000)
    exe_name: str = getattr(model_runtime_cfg, "exe_name", "GenieAPIService.exe")

    # V1 parity: default the model-scan root to ``<data>/models`` so an
    # operator who hasn't set ``service_launch.models_root_path`` still sees
    # models installed by the download center. The live provider injected
    # below overrides this static default when a root path is configured.
    default_models_root: str = _resolve_default_models_root(container)

    data_paths = getattr(container, "data_paths", None)

    service: InferenceServicePort
    if data_paths is not None:
        logger.info(
            "Using ProcessBackedInferenceService (dir=%s, port=%d, log_buffer=%d)",
            install_dir or "<live forge_config>",
            default_port,
            log_buffer_size,
        )
        # Inject async forge.config providers so the V1 ``-c`` /``-d``
        # CLI flags honour live ``service_launch.{models_root_path,loglevel}``
        # without the adapter importing ``qai.user_prefs`` directly. The
        # adapter awaits these providers each spawn so an operator who
        # edits forge.config does not need to restart the API server for
        # the new values to take effect. ``install_dir_provider`` does the
        # same for ``genie_service.root_path`` (binary discovery).
        models_root_provider_for_adapter = _make_models_root_provider(container)
        loglevel_provider_for_adapter = _make_loglevel_provider(container)
        install_dir_provider_for_adapter = _make_install_dir_provider(container)
        port_provider_for_adapter = _make_port_provider(container)
        service = ProcessBackedInferenceService(
            install_dir=install_dir,
            default_port=default_port,
            exe_name=exe_name,
            log_buffer_size=log_buffer_size,
            models_root=default_models_root,
            models_root_provider=models_root_provider_for_adapter,
            loglevel_provider=loglevel_provider_for_adapter,
            install_dir_provider=install_dir_provider_for_adapter,
            port_provider=port_provider_for_adapter,
        )
    else:
        # Minimal test container (no data_paths → cannot resolve forge_config).
        logger.info(
            "No data_paths on container — using StubInferenceService (tests only)"
        )
        service = StubInferenceService(
            install_dir=install_dir,
            default_port=default_port,
        )

    models_root_provider = _make_models_root_provider(container)
    genie_root_provider = _make_genie_root_provider(container)

    # Service-config CRUD: only wired when the platform ports it needs
    # (DataPaths + SecretStore) are present on the container. Minimal test
    # containers that mount only the daemon-control routes omit them.
    secret_store = getattr(container, "secret_store", None)
    service_config_repository: ServiceConfigRepositoryPort | None = None
    get_service_config_use_case: GetServiceConfigUseCase | None = None
    save_service_config_use_case: SaveServiceConfigUseCase | None = None
    if data_paths is not None and secret_store is not None:
        service_config_repository = FileServiceConfigRepository(
            data_paths=data_paths
        )
        get_service_config_use_case = GetServiceConfigUseCase(
            repository=service_config_repository,
            secret_store=secret_store,
            genie_root_provider=genie_root_provider,
        )
        save_service_config_use_case = SaveServiceConfigUseCase(
            repository=service_config_repository,
            secret_store=secret_store,
            genie_root_provider=genie_root_provider,
        )

    return ModelRuntimeServices(
        inference_service=service,
        service_config_repository=service_config_repository,
        start_service_use_case=StartServiceUseCase(service=service),
        stop_service_use_case=StopServiceUseCase(service=service),
        probe_service_use_case=ProbeServiceUseCase(service=service),
        get_status_use_case=GetStatusUseCase(
            service=service, models_root_provider=models_root_provider
        ),
        load_model_use_case=LoadModelUseCase(service=service),
        get_logs_use_case=GetLogsUseCase(service=service),
        clear_logs_use_case=ClearLogsUseCase(service=service),
        stream_logs_use_case=StreamLogsUseCase(service=service),
        stream_log_frames_use_case=StreamLogFramesUseCase(service=service),
        list_models_use_case=ListModelsUseCase(service=service),
        open_service_dir_use_case=OpenServiceDirUseCase(service=service),
        get_service_config_use_case=get_service_config_use_case,
        save_service_config_use_case=save_service_config_use_case,
    )
