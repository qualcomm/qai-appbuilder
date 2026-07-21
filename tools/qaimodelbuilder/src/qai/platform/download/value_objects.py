# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Download value objects for the platform shared kernel.

These frozen value objects were lifted verbatim from
:mod:`qai.model_catalog.domain.value_objects` so that the multi-threaded
aria2c download engine can live under ``qai.platform.*`` (a shared kernel
importable by every bounded context) without any context violating the
``context-isolation`` import-linter contract.

``qai.model_catalog.domain.value_objects`` re-exports these names, so
existing callers importing ``DownloadProgress`` / ``SourceUrl`` /
``StorageKey`` from the old model_catalog path keep working unchanged
(mirroring the :class:`qai.platform.crypto.hashes.Hash256` precedent).

Design rules (preserved from the model_catalog origin)
------------------------------------------------------
* ``@dataclass(frozen=True, slots=True, kw_only=True)`` everywhere.
* Construction-time validation in ``__post_init__``.
* No reference to mutable application objects, no I/O, no logging.
* These import ONLY existing ``qai.platform.*`` helpers, so they are
  pure/movable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)


__all__ = [
    "DownloadProgress",
    "StorageKey",
    "SourceUrl",
]


# ---------------------------------------------------------------------------
# DownloadProgress -- pure value, no identity.  Streams of these are
# produced by DownloadEnginePort.stream_progress and consumed by use cases.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadProgress:
    """Snapshot of a download's progress at a point in time.

    Invariants
    ----------
    * ``bytes_downloaded >= 0``
    * ``total_bytes`` is either ``None`` (unknown content-length) or
      ``>= bytes_downloaded``
    * ``speed_bps >= 0``
    * ``eta_seconds`` is either ``None`` or ``>= 0``
    """

    bytes_downloaded: int
    total_bytes: int | None
    speed_bps: float
    eta_seconds: float | None

    def __post_init__(self) -> None:
        if self.bytes_downloaded < 0:
            raise ValueError(
                f"bytes_downloaded must be >= 0, got {self.bytes_downloaded!r}"
            )
        if self.total_bytes is not None:
            if self.total_bytes < 0:
                raise ValueError(
                    f"total_bytes must be >= 0, got {self.total_bytes!r}"
                )
            if self.bytes_downloaded > self.total_bytes:
                raise ValueError(
                    "bytes_downloaded cannot exceed total_bytes "
                    f"({self.bytes_downloaded} > {self.total_bytes})"
                )
        if self.speed_bps < 0:
            raise ValueError(
                f"speed_bps must be >= 0, got {self.speed_bps!r}"
            )
        if self.eta_seconds is not None and self.eta_seconds < 0:
            raise ValueError(
                f"eta_seconds must be >= 0 or None, got {self.eta_seconds!r}"
            )

    @property
    def percent(self) -> float | None:
        """Completion percentage in ``[0.0, 100.0]``.

        Returns ``None`` if ``total_bytes`` is unknown so the caller has
        to choose between an indeterminate spinner and a percentage UI.
        """
        if self.total_bytes is None or self.total_bytes == 0:
            return None
        return 100.0 * float(self.bytes_downloaded) / float(self.total_bytes)

    @property
    def is_complete(self) -> bool:
        """True iff we know the total and have downloaded it all."""
        return (
            self.total_bytes is not None
            and self.bytes_downloaded >= self.total_bytes
        )


# ---------------------------------------------------------------------------
# StorageKey -- logical pointer used by BlobStorePort.  Never holds a
# physical path; the adapter resolves it through DataPaths.
# ---------------------------------------------------------------------------


_CATEGORY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True, slots=True, kw_only=True)
class StorageKey:
    """Logical (category, name) blob identifier.

    Examples (resolution is the adapter's job):

    * ``StorageKey(category="models", name="qwen-7b-q4_0.gguf")``
    * ``StorageKey(category="manifests", name="release-2026-05-29.json")``

    The category is restricted to ``[a-z][a-z0-9_]*`` so adapters can
    safely use it as a directory name without ever needing to escape.
    """

    category: str
    name: str

    def __post_init__(self) -> None:
        if not _CATEGORY_RE.match(self.category):
            raise ValueError(
                "StorageKey.category must match [a-z][a-z0-9_]{0,31}; "
                f"got {self.category!r}"
            )
        assert_non_empty(self.name, name="StorageKey.name")
        assert_max_length(self.name, max_length=255, name="StorageKey.name")
        if "/" in self.name or "\\" in self.name or ".." in self.name:
            raise ValueError(
                "StorageKey.name must not contain path separators or '..'; "
                f"got {self.name!r}"
            )


# ---------------------------------------------------------------------------
# SourceUrl -- string VO that refuses obvious shenanigans.  Adapters do the
# real network work; the domain only validates surface shape.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceUrl:
    """A http(s) URL the adapter is allowed to fetch from.

    We deliberately keep the validation extremely conservative: the
    domain layer is not in the business of parsing URLs, only of
    refusing the most obvious mistakes (empty / wrong scheme).  Adapters
    do the real job with ``urllib.parse`` / ``httpx``.
    """

    value: str

    def __post_init__(self) -> None:
        assert_non_empty(self.value, name="SourceUrl.value")
        assert_max_length(self.value, max_length=2048, name="SourceUrl.value")
        if not (
            self.value.startswith("https://")
            or self.value.startswith("http://")
        ):
            raise ValueError(
                "SourceUrl.value must start with http:// or https://; "
                f"got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value
