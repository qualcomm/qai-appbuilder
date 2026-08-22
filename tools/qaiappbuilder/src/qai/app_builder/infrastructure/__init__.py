# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem / process-backed infrastructure for the App Builder context.

Adapters that reach beyond the database (filesystem blobs, subprocess
runners, importer file scanning) live here rather than in
``adapters/`` so the layered import-linter contract still distinguishes
DB-only adapters from the rest.
"""

from __future__ import annotations

from .app_import_adapter import FileSystemAppImportAdapter
from .app_manifest import (
    FileSystemManifestReader,
    ManifestParseError,
    populate_runner_registry_from_manifests,
)
from .artifact_blob_reader import FileSystemArtifactBlobReader
from .artifact_store import FileSystemArtifactStore
from .audio_upload import FileSystemAudioUpload
from .command_resolver import (
    InMemoryRunnerCommandRegistry,
    PythonInterpreterResolver,
    QairtEnvJsonResolver,
    RunnerCommandRegistryPort,
    RunnerSpec,
    SysExecutableResolver,
    build_command_resolver,
)
from .process_runner import CommandResolver, ProcessBackedAppRunner
from .pack_file_cleanup import FileSystemPackFileCleanup
from .sticky_bootstrap import (
    BOOTSTRAP_SCRIPT,
    build_persistent_bootstrap_spec,
)
from .sticky_load_resolver import (
    StickyLoadResolver,
    build_load_request_for_model_id,
    build_sticky_load_resolver,
)
from .sticky_runner import StickyBackedAppRunner
from .runner_protocol import (
    DoneEvent,
    ErrorEvent,
    LogEvent,
    LogStream,
    MetricsEvent,
    NdjsonDecoder,
    ProgressEvent,
    ResultEvent,
    RunnerEvent,
    StatusEvent,
    StdoutLogEvent,
    UnknownRunnerEvent,
    decode_event,
    is_terminal,
)
from .sticky_worker import (
    BootstrapProtocol,
    BootstrapSpec,
    LoadedModelEntry,
    LoadModelRequest,
    ProtocolError,
    ProtocolFrame,
    RunRequest,
    StickyWorkerHost,
    StickyWorkerSpawnError,
    WorkerEvent,
)

__all__ = [
    # PR-303 — manifest schema v1
    "FileSystemManifestReader",
    "ManifestParseError",
    "populate_runner_registry_from_manifests",
    # pack file cleanup (delete)
    "FileSystemPackFileCleanup",
    "DoneEvent",
    "ErrorEvent",
    "LogEvent",
    "LogStream",
    "MetricsEvent",
    "NdjsonDecoder",
    "ProgressEvent",
    "ResultEvent",
    "RunnerEvent",
    "StatusEvent",
    "StdoutLogEvent",
    "UnknownRunnerEvent",
    "decode_event",
    "is_terminal",
    # PR-302 — command resolver
    "CommandResolver",
    "InMemoryRunnerCommandRegistry",
    "PythonInterpreterResolver",
    "QairtEnvJsonResolver",
    "RunnerCommandRegistryPort",
    "RunnerSpec",
    "SysExecutableResolver",
    "build_command_resolver",
    # PR-301 — sticky worker
    "BootstrapProtocol",
    "BootstrapSpec",
    "LoadModelRequest",
    "LoadedModelEntry",
    "ProtocolError",
    "ProtocolFrame",
    "RunRequest",
    "StickyWorkerHost",
    "StickyWorkerSpawnError",
    "WorkerEvent",
    # PR-302 wiring — sticky runner + load resolver + bootstrap spec
    "BOOTSTRAP_SCRIPT",
    "StickyBackedAppRunner",
    "StickyLoadResolver",
    "build_load_request_for_model_id",
    "build_persistent_bootstrap_spec",
    "build_sticky_load_resolver",
    # PR-045 — base infrastructure
    "FileSystemAppImportAdapter",
    "FileSystemArtifactBlobReader",
    "FileSystemArtifactStore",
    "FileSystemAudioUpload",
    "ProcessBackedAppRunner",
]
