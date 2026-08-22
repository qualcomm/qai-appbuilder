# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events emitted by the ``model_catalog`` bounded context.

Events are immutable snapshots: they carry IDs + small value objects,
NOT references to live aggregates.  Subscribers in other contexts (or
the same context's adapters) receive them via
:class:`qai.platform.events.EventBus`.

Naming follows S2 §9: ``<Subject><Verb>Event``, with ``event_type``
prefixed by ``"model_catalog."`` so topic-based subscribers can
``"model_catalog.*"`` glob the whole context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from qai.platform.events import DomainEvent

from .value_objects import (
    DownloadJobState,
    DownloadProgress,
    ModelVersionStatus,
    ProviderKind,
)


# ---------------------------------------------------------------------------
# Catalog lifecycle events.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelEntryRegisteredEvent(DomainEvent):
    """A new :class:`ModelEntry` has been added to the catalog."""

    event_type: ClassVar[str] = "model_catalog.entry_registered"
    model_id: str
    name: str
    provider: ProviderKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelEntryRemovedEvent(DomainEvent):
    """A :class:`ModelEntry` has been deleted from the catalog."""

    event_type: ClassVar[str] = "model_catalog.entry_removed"
    model_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVersionPublishedEvent(DomainEvent):
    """A new :class:`ModelVersion` is now visible under its parent entry."""

    event_type: ClassVar[str] = "model_catalog.version_published"
    model_id: str
    version_id: str
    status: ModelVersionStatus


# ---------------------------------------------------------------------------
# Download lifecycle events.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadStartedEvent(DomainEvent):
    """A :class:`DownloadJob` transitioned to ``RUNNING``."""

    event_type: ClassVar[str] = "model_catalog.download_started"
    job_id: str
    version_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadProgressedEvent(DomainEvent):
    """A periodic update from :meth:`DownloadEnginePort.stream_progress`."""

    event_type: ClassVar[str] = "model_catalog.download_progressed"
    job_id: str
    progress: DownloadProgress


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadCompletedEvent(DomainEvent):
    """A :class:`DownloadJob` reached ``COMPLETED`` after byte transfer."""

    event_type: ClassVar[str] = "model_catalog.download_completed"
    job_id: str
    version_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadFailedEvent(DomainEvent):
    """A :class:`DownloadJob` reached ``FAILED``."""

    event_type: ClassVar[str] = "model_catalog.download_failed"
    job_id: str
    version_id: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadCancelledEvent(DomainEvent):
    """A :class:`DownloadJob` was cancelled by the caller."""

    event_type: ClassVar[str] = "model_catalog.download_cancelled"
    job_id: str
    version_id: str


# ---------------------------------------------------------------------------
# Verification events.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ChecksumVerifiedEvent(DomainEvent):
    """A :class:`ModelVersion` blob passed checksum verification."""

    event_type: ClassVar[str] = "model_catalog.checksum_verified"
    version_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ChecksumMismatchEvent(DomainEvent):
    """A :class:`ModelVersion` blob failed checksum verification.

    Note: this is the *event*, distinct from
    :class:`ChecksumMismatchError` which is the *exception* the use case
    raises to the caller.  Subscribers (e.g. an audit adapter) want the
    event; the calling HTTP handler wants the exception.
    """

    event_type: ClassVar[str] = "model_catalog.checksum_mismatch"
    version_id: str
    expected: str
    actual: str


# ---------------------------------------------------------------------------
# Manifest events.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseManifestRefreshedEvent(DomainEvent):
    """The cached release manifest has been refreshed from upstream."""

    event_type: ClassVar[str] = "model_catalog.release_manifest_refreshed"
    manifest_version: str
    entry_count: int


# ---------------------------------------------------------------------------
# Generic state-changed event for download jobs (helps audit / UI).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadJobStateChangedEvent(DomainEvent):
    """Fine-grained state-machine notification (audit-friendly)."""

    event_type: ClassVar[str] = "model_catalog.download_job_state_changed"
    job_id: str
    from_state: DownloadJobState
    to_state: DownloadJobState


__all__ = [
    "ModelEntryRegisteredEvent",
    "ModelEntryRemovedEvent",
    "ModelVersionPublishedEvent",
    "DownloadStartedEvent",
    "DownloadProgressedEvent",
    "DownloadCompletedEvent",
    "DownloadFailedEvent",
    "DownloadCancelledEvent",
    "ChecksumVerifiedEvent",
    "ChecksumMismatchEvent",
    "ReleaseManifestRefreshedEvent",
    "DownloadJobStateChangedEvent",
]
