# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Taxonomy value object for App Builder.

The legacy backend exposes a hierarchical taxonomy via
``GET /api/appbuilder/taxonomy`` (see ``02-routes.md`` §3.3 line 154).
Each :class:`AppModelDefinition` is tagged with a path through this
tree (e.g. ``("audio", "speech_recognition", "stream")``).

The domain layer only models the *shape* of a taxonomy node:

* a list of segments, each non-empty and from a safe alphabet;
* the depth of the path;
* equality / ordering by tuple of segments (so collections are stable).

Adapters / repositories load the actual taxonomy tree from
configuration (``config/app_builder_models.json`` ``taxonomy`` block) and
hand :class:`Taxonomy` instances to use cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

__all__ = ["LEGACY_CATEGORY_MAP", "Taxonomy", "manifest_taxonomy_segments"]


_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
"""Segments are slug-like: lower-case, ``_-`` allowed, no leading dash."""


# Legacy ``category`` → ``(group, task)`` segments. Mirrors V1's
# ``backend/app_builder/taxonomy.py`` LEGACY_CATEGORY_MAP so manifests that
# still ship the deprecated ``category`` field classify identically.
LEGACY_CATEGORY_MAP: dict[str, tuple[str, str | None]] = {
    "SR": ("computer-vision", "super-resolution"),
    "OCR": ("computer-vision", "ocr"),
    "ASR": ("audio", "speech-recognition"),
    "TTS": ("audio", "audio-generation"),
    "CV": ("computer-vision", None),
    "LLM": ("generative-ai", "text-generation"),
    "NLP": ("generative-ai", "text-generation"),
    "Audio": ("audio", None),
    "Multimodal": ("multimodal", None),
}


def manifest_taxonomy_segments(obj: dict[str, Any]) -> tuple[str, ...]:
    """Extract ``(group, task)`` taxonomy segments from a manifest dict.

    Single source of truth shared by the built-in model **seed**
    (``apps.api.lifespan``) and the **import-commit** materialiser
    (``qai.app_builder.infrastructure.app_import_adapter``) so the two
    paths can never drift in how they classify a Pack.

    Three manifest shapes are accepted (in priority order):

    * **object form** — ``taxonomy: {"group": ..., "task": ..., "tags": [...]}``
      (the v2 source of truth emitted by ModelBuilder export). Returns the
      non-empty ``group`` + ``task`` segments; ``tags`` is presentation-only
      and is NOT a taxonomy path segment.
    * **list form** — ``taxonomy: ["computer-vision", "image-classification"]``
      (legacy / hand-authored fixtures). Returns the non-empty string segments
      verbatim.
    * **legacy ``category``** — when no usable ``taxonomy`` block is present,
      falls back to :data:`LEGACY_CATEGORY_MAP` (parity with V1's
      ``taxonomy.normalize``) so old Pack manifests still classify.

    Returns an empty tuple (root / "uncategorised") when nothing usable is
    found. Never raises — malformed inputs degrade to ``()``.
    """
    taxonomy = obj.get("taxonomy")
    if isinstance(taxonomy, dict):
        group = taxonomy.get("group")
        task = taxonomy.get("task")
        segments = [
            s for s in (group, task) if isinstance(s, str) and s.strip()
        ]
        if segments:
            return tuple(segments)
    elif isinstance(taxonomy, list):
        segments = [s for s in taxonomy if isinstance(s, str) and s.strip()]
        if segments:
            return tuple(segments)
    # Legacy ``category`` fallback (parity with V1 LEGACY_CATEGORY_MAP).
    legacy = obj.get("category")
    mapped = LEGACY_CATEGORY_MAP.get(legacy) if isinstance(legacy, str) else None
    if mapped is not None:
        return tuple(s for s in mapped if s)
    return ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Taxonomy:
    """An ordered path through the App Builder taxonomy tree.

    Segments are stored as a ``tuple`` (immutable, hashable). The empty
    path is allowed and represents the root / "uncategorised" bucket.

    Maximum depth is enforced by :attr:`MAX_DEPTH` (currently 5) — we do
    not expect deeper hierarchies and want to make pathological imports
    fail fast.
    """

    MAX_DEPTH: ClassVar[int] = 5

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segments, tuple):
            raise ValueError(
                "Taxonomy.segments must be a tuple, "
                f"got {type(self.segments).__name__}"
            )
        if len(self.segments) > self.MAX_DEPTH:
            raise ValueError(
                f"Taxonomy depth must be <= {self.MAX_DEPTH}, "
                f"got {len(self.segments)}"
            )
        for seg in self.segments:
            if not isinstance(seg, str):
                raise ValueError(
                    "Taxonomy segments must be str, "
                    f"got {type(seg).__name__}"
                )
            if not _SEGMENT_RE.match(seg):
                raise ValueError(
                    "Taxonomy segment must match [a-z0-9][a-z0-9_-]{0,63}, "
                    f"got {seg!r}"
                )

    @property
    def depth(self) -> int:
        return len(self.segments)

    @property
    def is_root(self) -> bool:
        return self.depth == 0

    def child(self, segment: str) -> Taxonomy:
        """Return a new ``Taxonomy`` with ``segment`` appended.

        Raises :class:`ValueError` if the resulting path exceeds
        :attr:`MAX_DEPTH` or if ``segment`` is malformed.
        """
        return Taxonomy(segments=(*self.segments, segment))

    def is_ancestor_of(self, other: Taxonomy) -> bool:
        """Return True iff ``self`` is a strict ancestor of ``other``."""
        return (
            self.depth < other.depth
            and other.segments[: self.depth] == self.segments
        )

    def __str__(self) -> str:
        return "/".join(self.segments) if self.segments else ""
