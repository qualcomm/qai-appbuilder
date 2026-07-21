# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the ``model_catalog`` bounded context.

Each use case is a small class with a single ``execute`` (async) method
and dependencies injected through ``__init__``.  They wire together
domain entities and application ports; they do NOT import adapters /
infrastructure (enforced by ``.importlinter`` contract
``layered-model_catalog``).

Public re-exports below let callers write::

    from qai.model_catalog.application.use_cases import StartDownloadUseCase
"""

from __future__ import annotations

from .cancel_download import CancelDownloadUseCase
from .get_model_entry import GetModelEntryUseCase
from .list_cloud_models import ListCloudModelsUseCase
from .list_download_jobs import ListDownloadJobsUseCase
from .list_model_entries import ListModelEntriesUseCase
from .list_provider_configs import ListProviderConfigsUseCase
from .probe_cloud_model_permissions import (
    InMemoryPermissionSnapshotStore,
    PermissionSnapshotStore,
    PermissionStatus,
    ProbeCloudModelPermissionsResult,
    ProbeCloudModelPermissionsUseCase,
)
from .probe_provider import ProbeProviderCommand, ProbeProviderUseCase
from .refresh_release_manifest import RefreshReleaseManifestUseCase
from .register_model_entry import RegisterModelEntryUseCase
from .remove_model_entry import RemoveModelEntryUseCase
from .remove_version import RemoveVersionCommand, RemoveVersionUseCase
from .start_download import StartDownloadUseCase
from .stream_download_progress import StreamDownloadProgressUseCase
from .update_provider_config import UpdateProviderConfigUseCase
from .verify_checksum import VerifyChecksumUseCase

__all__ = [
    "RegisterModelEntryUseCase",
    "RemoveModelEntryUseCase",
    "ListModelEntriesUseCase",
    "GetModelEntryUseCase",
    "StartDownloadUseCase",
    "CancelDownloadUseCase",
    "StreamDownloadProgressUseCase",
    "VerifyChecksumUseCase",
    "RefreshReleaseManifestUseCase",
    "ListProviderConfigsUseCase",
    "UpdateProviderConfigUseCase",
    "ProbeProviderUseCase",
    "ProbeProviderCommand",
    # Cloud-model permission scan (chat dropdown allowed/denied filter)
    "ProbeCloudModelPermissionsUseCase",
    "ProbeCloudModelPermissionsResult",
    "PermissionSnapshotStore",
    "InMemoryPermissionSnapshotStore",
    "PermissionStatus",
    # PR-044 (issue f)
    "ListDownloadJobsUseCase",
    "RemoveVersionUseCase",
    "RemoveVersionCommand",
    # Functional block 2 (cloud models)
    "ListCloudModelsUseCase",
]
