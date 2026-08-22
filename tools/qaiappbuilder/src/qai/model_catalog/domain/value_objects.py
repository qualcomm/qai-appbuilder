# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for the ``model_catalog`` bounded context.

These are immutable, equality-by-value objects with no identity of their
own.  They live alongside the entities (``entries.py`` /
``download_job.py``) and are imported wherever a typed *piece of data*
is needed instead of a primitive ``str`` / ``int``.

Design rules
------------

* ``@dataclass(frozen=True, slots=True, kw_only=True)`` everywhere.
* Construction-time validation in ``__post_init__``.
* No reference to mutable application objects, no I/O, no logging.
* Path-like VOs hold *logical* keys only -- they MUST NOT contain
  any physical filesystem-prefix literals.  Adapters resolve the
  actual filesystem path through ``DataPaths``.

Cross-PR note (PR-026 schema doc §10.1): :class:`Hash256` was lifted
to :mod:`qai.platform.crypto.hashes`. This module re-exports it so
existing callers are unaffected. :class:`Checksum` stays here because
it is algorithm-agnostic (SHA-256 *and* BLAKE3) and therefore not a
fit for a SHA-256-only platform primitive.

Download shared-kernel lift: :class:`DownloadProgress`, :class:`SourceUrl`
and :class:`StorageKey` were lifted to
:mod:`qai.platform.download.value_objects` so the aria2c download engine
could move into the platform shared kernel without a cross-context
import. This module re-exports them (mirroring the :class:`Hash256`
precedent) so every existing
``from qai.model_catalog.domain.value_objects import DownloadProgress``
caller keeps working unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from qai.platform.crypto.hashes import Hash256
from qai.platform.download.value_objects import (
    DownloadProgress,
    SourceUrl,
    StorageKey,
)
from qai.platform.io_validator import (
    assert_in_range,
    assert_max_length,
    assert_non_empty,
)

# ---------------------------------------------------------------------------
# Provider kinds -- enumerates the runtime backends a catalog entry can map
# to.  Used by entries.py to refuse mixing local + cloud semantics in the
# same model.
# ---------------------------------------------------------------------------


class ProviderKind(str, Enum):
    """Where a model is served from.

    ``LOCAL`` covers any on-device runtime (GGUF / ONNX / safetensors)
    that we download into the local blob store.  Everything else is
    served by a remote API.

    The values are deliberately stable strings (not ints) so the same
    enum can round-trip through JSON / SQLite without an extra mapping.
    """

    LOCAL = "local"
    OLLAMA = "ollama"
    OPENAI_COMPAT = "openai_compat"
    ANTHROPIC = "anthropic"
    GENERIC_CLOUD = "generic_cloud"

    @property
    def is_local(self) -> bool:
        return self is ProviderKind.LOCAL

    @property
    def is_cloud(self) -> bool:
        return not self.is_local


# ---------------------------------------------------------------------------
# Hash256 lives in qai.platform.crypto.hashes; re-exported via __all__ for
# backward compatibility with PR-022 / PR-025 callers.
# ---------------------------------------------------------------------------


_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
"""Local copy used by :class:`Checksum` for SHA-256 surface validation."""


# ---------------------------------------------------------------------------
# Checksum -- algorithm + value pair, so future migrations to BLAKE3 / SHA-512
# do not break the schema.  ``Hash256`` remains the most common case.
# ---------------------------------------------------------------------------


class ChecksumAlgorithm(str, Enum):
    SHA256 = "sha256"
    BLAKE3 = "blake3"


@dataclass(frozen=True, slots=True, kw_only=True)
class Checksum:
    """A single content-integrity check.

    The verification policy is "value must equal candidate digest of the
    same algorithm" -- nothing more, nothing less.  Adapters compute the
    candidate; the domain layer just compares strings via :meth:`matches`.
    """

    algorithm: ChecksumAlgorithm
    value: str

    def __post_init__(self) -> None:
        assert_non_empty(self.value, name="Checksum.value")
        assert_max_length(self.value, max_length=256, name="Checksum.value")
        if self.algorithm is ChecksumAlgorithm.SHA256:
            if not _HEX64_RE.match(self.value):
                raise ValueError(
                    "sha256 Checksum.value must be 64 lower-case hex chars; "
                    f"got {self.value!r}"
                )

    def matches(self, candidate: str) -> bool:
        """Return ``True`` iff ``candidate`` equals our stored digest.

        Comparison is case-insensitive on the assumption hex digests are
        the only realistic algorithm right now; we still normalise to
        lower case to avoid surprises.
        """
        return self.value.lower() == candidate.lower()


# ---------------------------------------------------------------------------
# DownloadProgress -- lifted to qai.platform.download.value_objects; re-exported
# here (see __all__) for backward compatibility with existing callers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# StorageKey -- lifted to qai.platform.download.value_objects; re-exported
# here (see __all__) for backward compatibility with existing callers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Taxonomy -- per-context tag bag.  See manifest §5: this is intentionally
# scoped to model_catalog and does NOT import app_builder.Taxonomy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Taxonomy:
    """Curated tag bag attached to a :class:`ModelEntry`.

    ``tags`` is a tuple (immutable) so the whole VO can be hashed and
    safely stored in sets or dict keys.  Tags are normalised to lower
    case at construction.
    """

    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for tag in self.tags:
            if not isinstance(tag, str):
                raise TypeError(
                    f"Taxonomy.tags items must be str, got {type(tag).__name__}"
                )
            assert_non_empty(tag, name="Taxonomy.tag")
            assert_max_length(tag, max_length=64, name="Taxonomy.tag")
            if tag.lower() in seen:
                raise ValueError(
                    f"Taxonomy.tags contains duplicate {tag!r}"
                )
            seen.add(tag.lower())
        # Normalise to lower-case + sorted tuple for stable equality.
        normalised = tuple(sorted(t.lower() for t in self.tags))
        # ``frozen=True`` forbids assignment via ``self.x = ...``, so we
        # use object.__setattr__ -- the canonical pattern for frozen
        # dataclasses needing post-init normalisation.
        object.__setattr__(self, "tags", normalised)

    def with_tag(self, tag: str) -> Taxonomy:
        return Taxonomy(tags=tuple(set(self.tags) | {tag.lower()}))

    def has(self, tag: str) -> bool:
        return tag.lower() in self.tags


# ---------------------------------------------------------------------------
# Status enums for entities.
# ---------------------------------------------------------------------------


class ModelVersionStatus(str, Enum):
    """Lifecycle of a ``ModelVersion`` from publication to local install.

    * ``PUBLISHED`` -- visible in the catalog, never downloaded locally.
    * ``DOWNLOADING`` -- a ``DownloadJob`` is currently fetching it.
    * ``INSTALLED`` -- bytes are on disk and verified.
    * ``CORRUPTED`` -- bytes are on disk but checksum mismatch detected.
    * ``UNINSTALLED`` -- previously installed, now removed by the user;
      catalog metadata still kept for historical reference.
    """

    PUBLISHED = "published"
    DOWNLOADING = "downloading"
    INSTALLED = "installed"
    CORRUPTED = "corrupted"
    UNINSTALLED = "uninstalled"


class DownloadJobState(str, Enum):
    """Finite state machine for ``DownloadJob`` (see entity for transitions)."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_DOWNLOAD_STATES


_TERMINAL_DOWNLOAD_STATES: Final[frozenset[DownloadJobState]] = frozenset(
    {
        DownloadJobState.COMPLETED,
        DownloadJobState.CANCELLED,
        DownloadJobState.FAILED,
    }
)


# ---------------------------------------------------------------------------
# SourceUrl -- lifted to qai.platform.download.value_objects; re-exported
# here (see __all__) for backward compatibility with existing callers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SizeBytes -- enforces non-negative file size; small but typeful guard.
# ---------------------------------------------------------------------------


_SIZE_BYTES_MAX: Final[int] = 1 << 60  # 1 EiB sanity cap.


@dataclass(frozen=True, slots=True, kw_only=True)
class SizeBytes:
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError(
                f"SizeBytes.value must be int, got {type(self.value).__name__}"
            )
        assert_in_range(
            self.value, min=0, max=_SIZE_BYTES_MAX, name="SizeBytes.value"
        )

    def __int__(self) -> int:
        return self.value


__all__ = [
    "ProviderKind",
    "Hash256",
    "ChecksumAlgorithm",
    "Checksum",
    "DownloadProgress",
    "StorageKey",
    "Taxonomy",
    "ModelVersionStatus",
    "DownloadJobState",
    "SourceUrl",
    "SizeBytes",
]
