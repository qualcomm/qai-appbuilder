# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for the ``model_catalog`` bounded context.

Ports (``typing.Protocol``) describe *what* the use cases need from the
outside world; concrete implementations live under ``adapters/`` and
``infrastructure/`` (see S2 spec §1).

Cycle-breaking note (HANDOFF §3.5)
----------------------------------

The legacy code base had a hard cycle between two helper modules: the
download helper called the release-version helper for "latest version"
lookups, and the release-version helper called the download helper to
start a fetch.  In the new design the cycle is removed by:

* :class:`DownloadEnginePort` -- a transport-agnostic interface
  (multiple implementations: native CLI engines, ``httpx``,
  ``requests``, ``curl-impersonate``...).  No transport-specific knobs
  leak into the domain.
* :class:`ManifestFetcherPort` -- "give me the upstream release
  manifest" reduced to a single method.  The version-comparison logic
  that used to live in the legacy release helper now lives in the
  :class:`RefreshReleaseManifestUseCase` and never calls the engine.
* :class:`ChecksumVerifierPort` -- byte-checking is now an explicit
  port; previously it was a hidden side-effect inside the downloader.

With these three ports, the use-case layer composes both directions
without either adapter ever importing the other.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from qai.platform.download.ports import DownloadEnginePort

from qai.model_catalog.domain.entities import (
    DownloadJob,
    ModelEntry,
    ReleaseManifest,
    SkillDefinition,
)
from qai.model_catalog.domain.ids import (
    DownloadJobId,
    ModelEntryId,
    SkillName,
)
from qai.model_catalog.domain.value_objects import (
    Checksum,
    StorageKey,
)


# ===========================================================================
# Repositories
# ===========================================================================


@runtime_checkable
class ModelEntryRepositoryPort(Protocol):
    """Persistence port for :class:`ModelEntry` aggregates.

    The concrete implementation is the responsibility of PR-026 (single
    SQLite schema) / PR-040+ (aiosqlite adapter).  This Protocol exists
    so use cases can be written and unit-tested before any of that
    lands.
    """

    async def add(self, entry: ModelEntry) -> None:
        """Insert a brand-new entry; raises :class:`ModelEntryConflictError` on dup."""
        ...

    async def update(self, entry: ModelEntry) -> None:
        """Persist mutations on an existing aggregate."""
        ...

    async def remove(self, model_id: ModelEntryId) -> None:
        """Hard-delete the entry; raises :class:`ModelEntryNotFoundError`."""
        ...

    async def find_by_id(self, model_id: ModelEntryId) -> ModelEntry | None:
        """Lookup by id; ``None`` when missing (NotFound is a use-case concern)."""
        ...

    async def list_all(self) -> list[ModelEntry]:
        """Return every entry in the catalog, order is implementation-defined."""
        ...


@runtime_checkable
class DownloadJobRepositoryPort(Protocol):
    """Persistence port for :class:`DownloadJob` aggregates."""

    async def add(self, job: DownloadJob) -> None:
        ...

    async def update(self, job: DownloadJob) -> None:
        ...

    async def find_by_id(self, job_id: DownloadJobId) -> DownloadJob | None:
        ...

    async def list_active(self) -> list[DownloadJob]:
        """Return jobs in any non-terminal state."""
        ...


@runtime_checkable
class SkillRegistryPort(Protocol):
    """Persistence + query for :class:`SkillDefinition` records.

    Cross-PR note (manifest §5): if PR-021 / PR-024 (``ai_coding``) ends
    up owning the canonical skill registry, this port is the candidate
    to retire; either way, this PR's domain does NOT import ai_coding.
    """

    async def list_skills(self) -> list[SkillDefinition]:
        ...

    async def get(self, skill: SkillName) -> SkillDefinition | None:
        ...

    async def upsert(self, skill: SkillDefinition) -> None:
        ...


@runtime_checkable
class ProviderRegistryPort(Protocol):
    """Read / write provider configs (formerly ``cloud_models.json``).

    The path resolution is delegated to the adapter via :class:`StorageKey`
    so the domain stays free of physical-path string literals.
    """

    async def list_provider_configs(self) -> list[dict[str, Any]]:
        ...

    async def get_provider_config(
        self, provider_id: str
    ) -> dict[str, Any] | None:
        ...

    async def save_provider_config(
        self, provider_id: str, config: dict[str, Any]
    ) -> None:
        ...


# ===========================================================================
# DownloadEngine -- the abstraction that breaks the legacy cycle.
# ===========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderProbeResult:
    """Outcome of a provider connectivity probe (never carries secrets)."""

    ok: bool
    status: int | None = None
    model_ids: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None


@runtime_checkable
class ProviderProbePort(Protocol):
    """Connectivity probe for a configured cloud provider.

    ``ListCloudModelsUseCase`` only reads the local registry; it cannot tell
    whether an ``api_key`` / ``base_url`` actually works. This port issues a
    real minimal request (typically ``GET {base_url}/v1/models``) so the
    config wizard / ``qai config provider test`` can verify a provider before
    relying on it (truth-from-real-state, AGENTS.md 🔴). Adapters keep the
    transport (``httpx``) behind this port so the domain/use-case layer stays
    transport-free.
    """

    async def probe(
        self, *, base_url: str, api_key: str | None
    ) -> "ProviderProbeResult":
        """Issue a minimal request and report reachability + model ids."""
        ...


# ``DownloadEnginePort`` was lifted to :mod:`qai.platform.download.ports`
# together with the aria2c engines and download value objects (so the
# engine could move into the platform shared kernel without a
# cross-context import). It is re-exported here (see the import at the top
# of this module + ``__all__``) so every existing
# ``from qai.model_catalog.application.ports import DownloadEnginePort``
# caller keeps working. The platform port types its ``job`` argument
# against the structural :class:`~qai.platform.download.ports.DownloadJobLike`
# Protocol, which the concrete :class:`DownloadJob` entity satisfies, so
# use cases keep passing real ``DownloadJob`` instances unchanged.


# ===========================================================================
# Checksum + manifest fetcher.
# ===========================================================================


@runtime_checkable
class ChecksumVerifierPort(Protocol):
    """Compute & verify content digests for downloaded blobs.

    Implementations stream the underlying file (or in-memory buffer)
    and produce the digest the supplied :class:`Checksum` will be
    matched against.
    """

    async def compute(
        self, target: StorageKey, *, algorithm: str
    ) -> str:
        """Return the hex digest of ``target`` using ``algorithm``."""
        ...

    async def verify(
        self, target: StorageKey, expected: Checksum
    ) -> bool:
        """``True`` iff the candidate digest equals ``expected.value``."""
        ...


@runtime_checkable
class ManifestFetcherPort(Protocol):
    """Fetch the upstream release manifest.

    The raw transport (http) is *behind* this port; the domain never
    sees ``httpx`` / ``requests``.  Adapters convert payload errors to
    :class:`InfrastructureError` and content errors to
    :class:`ReleaseManifestUnavailableError` as appropriate.
    """

    async def fetch_latest(self) -> ReleaseManifest:
        ...


# ===========================================================================
# Blob store -- where downloads land.
# ===========================================================================


@runtime_checkable
class BlobStorePort(Protocol):
    """File-system-like blob storage keyed by :class:`StorageKey`.

    Adapters resolve logical keys to physical paths via
    ``DataPaths``; the domain never sees an absolute path.
    """

    async def exists(self, key: StorageKey) -> bool:
        ...

    async def remove(self, key: StorageKey) -> None:
        ...

    async def size_bytes(self, key: StorageKey) -> int:
        ...

    async def list_keys(self, category: str) -> Iterable[StorageKey]:
        ...


__all__ = [
    "ModelEntryRepositoryPort",
    "DownloadJobRepositoryPort",
    "SkillRegistryPort",
    "ProviderRegistryPort",
    "ProviderProbePort",
    "ProviderProbeResult",
    "DownloadEnginePort",
    "ChecksumVerifierPort",
    "ManifestFetcherPort",
    "BlobStorePort",
]

# Note: ``ModelVersionRepositoryPort`` is intentionally *not* defined.
# ModelVersion is a child entity of the ModelEntry aggregate (DDD), so
# all version-level persistence flows through the entry repository.
# Adding a separate version repo would tempt callers to mutate versions
# without going through their parent and break the aggregate's
# invariants.
