# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aggregates and entities for the ``model_catalog`` bounded context.

Aggregates
----------

* :class:`ModelEntry` -- the catalog row.  Holds metadata about a model
  (name, provider, source URL) plus a list of :class:`ModelVersion`
  child entities.  The aggregate enforces invariants that span entry +
  versions (e.g. only one version may be ``DOWNLOADING`` at a time).

* :class:`DownloadJob` -- a deliberately *separate* aggregate from
  :class:`ModelEntry`.  It tracks one download attempt for one version.
  Keeping it separate avoids the historical bug where the legacy
  download helper reached into the release-helper module and back; the
  new design has one-way relations: a job *references* a version id but
  does not own the version, and the catalog *references* job ids but
  does not own jobs.

* :class:`SkillDefinition` -- skill manifest entry (skill registry
  inside the catalog).  Independent aggregate.

* :class:`ReleaseManifest` -- cached snapshot of the upstream release
  manifest.  Independent aggregate, mostly read-only.

Cycle-breaking
--------------

The legacy code base had a true cycle between the download helper and
the release-version helper.  In the new design that cycle is broken
three ways:

1. ``DownloadJob`` is its own aggregate (no upward reference into
   ``ModelEntry`` / ``ModelVersion``).
2. ``ModelEntry`` / ``ModelVersion`` never call the engine; they hold
   no knowledge of how downloading happens.  Use cases compose the two
   sides through :class:`DownloadEnginePort` and
   :class:`ChecksumVerifierPort` -- see ``application/ports.py``.
3. The actual transport (native CLI engines / ``httpx`` / ``requests``)
   implements :class:`DownloadEnginePort`; the domain layer does not
   import any of them.  This eliminates the static cycle entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time.conversions import ensure_aware_utc

from .errors import (
    DownloadJobAlreadyTerminatedError,
    InvalidDownloadStateTransitionError,
    ModelEntryConflictError,
    ModelVersionNotFoundError,
)
from .ids import DownloadJobId, ModelEntryId, ModelVersionId, SkillName
from .value_objects import (
    Checksum,
    DownloadJobState,
    DownloadProgress,
    ModelVersionStatus,
    ProviderKind,
    SizeBytes,
    SourceUrl,
    Taxonomy,
)

_MAX_NAME_LENGTH: Final[int] = 255
_MAX_DESC_LENGTH: Final[int] = 4096


# ---------------------------------------------------------------------------
# ModelVersion -- inner entity of the ModelEntry aggregate.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModelVersion:
    """A single downloadable / installed version of a parent
    :class:`ModelEntry`.

    Mutable on purpose: the entry aggregate updates the version's status
    while orchestrating downloads / verification.  Mutation goes through
    :meth:`mark_*` methods only, never raw field writes.
    """

    version_id: ModelVersionId
    parent_model_id: ModelEntryId
    checksum: Checksum
    size_bytes: SizeBytes
    manifest_url: SourceUrl
    status: ModelVersionStatus = ModelVersionStatus.PUBLISHED

    # ── Status mutators (validated transitions) ────────────────────────

    def mark_downloading(self) -> None:
        if self.status not in {
            ModelVersionStatus.PUBLISHED,
            ModelVersionStatus.UNINSTALLED,
            ModelVersionStatus.CORRUPTED,
        }:
            raise InvalidDownloadStateTransitionError(
                from_state=self.status.value,
                to_state=ModelVersionStatus.DOWNLOADING.value,
            )
        self.status = ModelVersionStatus.DOWNLOADING

    def mark_installed(self) -> None:
        if self.status is not ModelVersionStatus.DOWNLOADING:
            raise InvalidDownloadStateTransitionError(
                from_state=self.status.value,
                to_state=ModelVersionStatus.INSTALLED.value,
            )
        self.status = ModelVersionStatus.INSTALLED

    def mark_corrupted(self) -> None:
        if self.status not in {
            ModelVersionStatus.DOWNLOADING,
            ModelVersionStatus.INSTALLED,
        }:
            raise InvalidDownloadStateTransitionError(
                from_state=self.status.value,
                to_state=ModelVersionStatus.CORRUPTED.value,
            )
        self.status = ModelVersionStatus.CORRUPTED

    def mark_uninstalled(self) -> None:
        if self.status not in {
            ModelVersionStatus.INSTALLED,
            ModelVersionStatus.CORRUPTED,
        }:
            raise InvalidDownloadStateTransitionError(
                from_state=self.status.value,
                to_state=ModelVersionStatus.UNINSTALLED.value,
            )
        self.status = ModelVersionStatus.UNINSTALLED


# ---------------------------------------------------------------------------
# ModelEntry -- aggregate root.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModelEntry:
    """A row in the model catalog.

    Holds zero or more :class:`ModelVersion` children.  The aggregate
    enforces:

    * version IDs are unique within the entry;
    * at most one version is ``DOWNLOADING`` at any time.

    Identity is the ``model_id`` value object, stable across renames.
    """

    model_id: ModelEntryId
    name: str
    provider: ProviderKind
    source_url: SourceUrl
    description: str = ""
    taxonomy: Taxonomy = field(default_factory=Taxonomy)
    versions: list[ModelVersion] = field(default_factory=list)
    current_version_id: ModelVersionId | None = None

    def __post_init__(self) -> None:
        assert_non_empty(self.name, name="ModelEntry.name")
        assert_max_length(
            self.name, max_length=_MAX_NAME_LENGTH, name="ModelEntry.name"
        )
        if self.description:
            assert_max_length(
                self.description,
                max_length=_MAX_DESC_LENGTH,
                name="ModelEntry.description",
            )
        # Validate version uniqueness (in case caller pre-populates).
        seen: set[str] = set()
        for v in self.versions:
            if v.version_id.value in seen:
                raise ModelEntryConflictError(
                    self.model_id.value,
                    message=(
                        f"duplicate version_id {v.version_id.value!r} in "
                        f"model_entry {self.model_id.value!r}"
                    ),
                )
            seen.add(v.version_id.value)
            if v.parent_model_id != self.model_id:
                raise ValueError(
                    "ModelVersion.parent_model_id must equal the parent "
                    "ModelEntry.model_id"
                )
        self._enforce_single_downloading_invariant()
        if self.current_version_id is not None:
            self._require_version(self.current_version_id)

    # ── Public mutators ────────────────────────────────────────────────

    def add_version(self, version: ModelVersion) -> None:
        if version.parent_model_id != self.model_id:
            raise ValueError(
                "version.parent_model_id must equal this entry's model_id"
            )
        if self.find_version(version.version_id) is not None:
            raise ModelEntryConflictError(
                self.model_id.value,
                message=(
                    f"version {version.version_id.value!r} already exists in "
                    f"model_entry {self.model_id.value!r}"
                ),
            )
        self.versions.append(version)
        self._enforce_single_downloading_invariant()

    def find_version(self, version_id: ModelVersionId) -> ModelVersion | None:
        for v in self.versions:
            if v.version_id == version_id:
                return v
        return None

    def get_version(self, version_id: ModelVersionId) -> ModelVersion:
        v = self.find_version(version_id)
        if v is None:
            raise ModelVersionNotFoundError(version_id.value)
        return v

    def remove_version(self, version_id: ModelVersionId) -> None:
        v = self.get_version(version_id)
        self.versions.remove(v)
        if self.current_version_id == version_id:
            self.current_version_id = None

    def set_current_version(self, version_id: ModelVersionId) -> None:
        # Implicitly validates existence.
        self._require_version(version_id)
        self.current_version_id = version_id

    def with_taxonomy(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy

    # ── Invariants ─────────────────────────────────────────────────────

    def _enforce_single_downloading_invariant(self) -> None:
        downloading = [
            v
            for v in self.versions
            if v.status is ModelVersionStatus.DOWNLOADING
        ]
        if len(downloading) > 1:
            raise ModelEntryConflictError(
                self.model_id.value,
                message=(
                    "at most one ModelVersion may be DOWNLOADING per "
                    "ModelEntry; got "
                    f"{[v.version_id.value for v in downloading]!r}"
                ),
            )

    def _require_version(self, version_id: ModelVersionId) -> ModelVersion:
        v = self.find_version(version_id)
        if v is None:
            raise ModelVersionNotFoundError(version_id.value)
        return v


# ---------------------------------------------------------------------------
# DownloadJob -- separate aggregate.
# ---------------------------------------------------------------------------

# Allowed FSM edges.  Sorted alphabetically by source state for ease of audit.
_DOWNLOAD_TRANSITIONS: Final[dict[DownloadJobState, frozenset[DownloadJobState]]] = {
    DownloadJobState.QUEUED: frozenset(
        {
            DownloadJobState.RUNNING,
            DownloadJobState.CANCELLED,
            DownloadJobState.FAILED,
        }
    ),
    DownloadJobState.RUNNING: frozenset(
        {
            DownloadJobState.PAUSED,
            DownloadJobState.COMPLETED,
            DownloadJobState.CANCELLED,
            DownloadJobState.FAILED,
        }
    ),
    DownloadJobState.PAUSED: frozenset(
        {
            DownloadJobState.RUNNING,
            DownloadJobState.CANCELLED,
            DownloadJobState.FAILED,
        }
    ),
    # Terminal states have no outgoing edges.
    DownloadJobState.COMPLETED: frozenset(),
    DownloadJobState.CANCELLED: frozenset(),
    DownloadJobState.FAILED: frozenset(),
}


@dataclass(slots=True)
class DownloadJob:
    """One download attempt for one :class:`ModelVersion`.

    The job is a small finite state machine; transitions go through
    :meth:`transition_to` so the legal-edges table is the single source
    of truth.

    The job *references* a :class:`ModelVersionId` but does not hold a
    reference to the parent :class:`ModelEntry` -- this is what cuts the
    historical engine ↔ release-helper cycle.
    """

    job_id: DownloadJobId
    target_model_version_id: ModelVersionId
    state: DownloadJobState = DownloadJobState.QUEUED
    progress: DownloadProgress = field(
        default_factory=lambda: DownloadProgress(
            bytes_downloaded=0,
            total_bytes=None,
            speed_bps=0.0,
            eta_seconds=None,
        )
    )
    failure_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is not None:
            self.created_at = ensure_aware_utc(self.created_at)
        if self.updated_at is not None:
            self.updated_at = ensure_aware_utc(self.updated_at)

    # ── Transitions ────────────────────────────────────────────────────

    def transition_to(
        self,
        target: DownloadJobState,
        *,
        now: datetime | None = None,
        reason: str | None = None,
    ) -> None:
        if self.state.is_terminal:
            raise DownloadJobAlreadyTerminatedError(
                self.job_id.value, self.state.value
            )
        allowed = _DOWNLOAD_TRANSITIONS[self.state]
        if target not in allowed:
            raise InvalidDownloadStateTransitionError(
                from_state=self.state.value, to_state=target.value
            )
        self.state = target
        if target is DownloadJobState.FAILED:
            self.failure_reason = reason or "unspecified"
        if now is not None:
            self.updated_at = ensure_aware_utc(now)

    def update_progress(
        self,
        progress: DownloadProgress,
        *,
        now: datetime | None = None,
    ) -> None:
        if self.state.is_terminal:
            raise DownloadJobAlreadyTerminatedError(
                self.job_id.value, self.state.value
            )
        self.progress = progress
        if now is not None:
            self.updated_at = ensure_aware_utc(now)

    # Convenience wrappers used by the use-case layer.
    def start(self, *, now: datetime | None = None) -> None:
        self.transition_to(DownloadJobState.RUNNING, now=now)

    def pause(self, *, now: datetime | None = None) -> None:
        self.transition_to(DownloadJobState.PAUSED, now=now)

    def resume(self, *, now: datetime | None = None) -> None:
        self.transition_to(DownloadJobState.RUNNING, now=now)

    def complete(self, *, now: datetime | None = None) -> None:
        self.transition_to(DownloadJobState.COMPLETED, now=now)

    def cancel(self, *, now: datetime | None = None) -> None:
        self.transition_to(DownloadJobState.CANCELLED, now=now)

    def fail(self, reason: str, *, now: datetime | None = None) -> None:
        self.transition_to(DownloadJobState.FAILED, now=now, reason=reason)


# ---------------------------------------------------------------------------
# SkillDefinition -- registry entry for an addressable skill.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SkillDefinition:
    """One skill registered in the catalog.

    Skills are conceptually independent of model entries -- a skill is a
    *role* / *prompt template*, not a runtime artefact.  But they live
    in the same catalog file historically, so they share this bounded
    context.

    Cross-PR note (manifest §5): if ``ai_coding`` ends up owning
    ``SkillRegistryPort``, this entity may be moved.  For now, both
    contexts can coexist without importing each other.
    """

    skill_name: SkillName
    version: str
    enabled: bool = True
    manifest: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert_non_empty(self.version, name="SkillDefinition.version")
        assert_max_length(
            self.version, max_length=64, name="SkillDefinition.version"
        )

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False


# ---------------------------------------------------------------------------
# ReleaseManifest -- snapshot of upstream release manifest.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseManifestEntry:
    """One row inside a :class:`ReleaseManifest`."""

    model_id: ModelEntryId
    version_id: ModelVersionId
    checksum: Checksum
    size_bytes: SizeBytes
    download_url: SourceUrl


@dataclass(slots=True)
class ReleaseManifest:
    """Cached upstream catalog manifest.

    Mostly read-only from the perspective of the application: the
    refresh use case replaces the value wholesale rather than mutating
    individual entries.
    """

    manifest_version: str
    fetched_at: datetime
    entries: tuple[ReleaseManifestEntry, ...] = ()

    def __post_init__(self) -> None:
        assert_non_empty(
            self.manifest_version, name="ReleaseManifest.manifest_version"
        )
        assert_max_length(
            self.manifest_version,
            max_length=64,
            name="ReleaseManifest.manifest_version",
        )
        self.fetched_at = ensure_aware_utc(self.fetched_at)
        seen_pairs: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.model_id.value, entry.version_id.value)
            if key in seen_pairs:
                raise ValueError(
                    "ReleaseManifest contains duplicate "
                    f"(model_id, version_id) pair: {key!r}"
                )
            seen_pairs.add(key)

    @property
    def entry_count(self) -> int:
        return len(self.entries)


__all__ = [
    "ModelVersion",
    "ModelEntry",
    "DownloadJob",
    "SkillDefinition",
    "ReleaseManifestEntry",
    "ReleaseManifest",
]
