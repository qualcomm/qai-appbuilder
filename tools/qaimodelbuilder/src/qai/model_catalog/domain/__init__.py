# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain layer for the ``model_catalog`` bounded context.

Public re-exports so callers can write::

    from qai.model_catalog.domain import (
        ModelEntry, ModelVersion, DownloadJob, ProviderKind, ...
    )

without reaching into individual sub-modules.
"""

from __future__ import annotations

from .entities import (
    DownloadJob,
    ModelEntry,
    ModelVersion,
    ReleaseManifest,
    ReleaseManifestEntry,
    SkillDefinition,
)
from .errors import (
    ChecksumMismatchError,
    DownloadJobAlreadyTerminatedError,
    DownloadJobNotFoundError,
    InvalidDownloadStateTransitionError,
    ModelEntryConflictError,
    ModelEntryNotFoundError,
    ModelVersionNotFoundError,
    ProviderConfigInvalidError,
    ReleaseManifestUnavailableError,
)
from .events import (
    ChecksumMismatchEvent,
    ChecksumVerifiedEvent,
    DownloadCancelledEvent,
    DownloadCompletedEvent,
    DownloadFailedEvent,
    DownloadJobStateChangedEvent,
    DownloadProgressedEvent,
    DownloadStartedEvent,
    ModelEntryRegisteredEvent,
    ModelEntryRemovedEvent,
    ModelVersionPublishedEvent,
    ReleaseManifestRefreshedEvent,
)
from .ids import (
    DownloadJobId,
    ModelEntryId,
    ModelVersionId,
    SkillName,
)
from .value_objects import (
    Checksum,
    ChecksumAlgorithm,
    DownloadJobState,
    DownloadProgress,
    Hash256,
    ModelVersionStatus,
    ProviderKind,
    SizeBytes,
    SourceUrl,
    StorageKey,
    Taxonomy,
)

__all__ = [
    # IDs
    "DownloadJobId",
    "ModelEntryId",
    "ModelVersionId",
    "SkillName",
    # Value objects
    "Checksum",
    "ChecksumAlgorithm",
    "DownloadJobState",
    "DownloadProgress",
    "Hash256",
    "ModelVersionStatus",
    "ProviderKind",
    "SizeBytes",
    "SourceUrl",
    "StorageKey",
    "Taxonomy",
    # Entities / aggregates
    "DownloadJob",
    "ModelEntry",
    "ModelVersion",
    "ReleaseManifest",
    "ReleaseManifestEntry",
    "SkillDefinition",
    # Errors
    "ChecksumMismatchError",
    "DownloadJobAlreadyTerminatedError",
    "DownloadJobNotFoundError",
    "InvalidDownloadStateTransitionError",
    "ModelEntryConflictError",
    "ModelEntryNotFoundError",
    "ModelVersionNotFoundError",
    "ProviderConfigInvalidError",
    "ReleaseManifestUnavailableError",
    # Events
    "ChecksumMismatchEvent",
    "ChecksumVerifiedEvent",
    "DownloadCancelledEvent",
    "DownloadCompletedEvent",
    "DownloadFailedEvent",
    "DownloadJobStateChangedEvent",
    "DownloadProgressedEvent",
    "DownloadStartedEvent",
    "ModelEntryRegisteredEvent",
    "ModelEntryRemovedEvent",
    "ModelVersionPublishedEvent",
    "ReleaseManifestRefreshedEvent",
]
