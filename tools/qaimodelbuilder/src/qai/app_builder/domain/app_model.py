# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``AppModelDefinition`` entity.

Represents a single registered App Builder model — the pure-domain
analogue of an entry in the legacy ``config/app_builder_models.json``
file (see ``05-data-config.md`` line 29).

We deliberately call this **AppModelDefinition** instead of plain
``ModelEntry`` to avoid colliding with ``model_catalog.ModelEntry``
(PR-025). The two contexts are semantically different: a *catalog*
entry is a downloadable artifact, while an *app model* is a runnable
function exposed by App Builder (it may reference one or more catalog
entries via ``required_catalog_ids`` but the relation lives outside the
domain layer).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qai.app_builder.domain.taxonomy import Taxonomy
from qai.app_builder.domain.value_objects import AppModelId, InputPreset

__all__ = ["AppModelDefinition"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AppModelDefinition:
    """A single registered App Builder model.

    Fields:

    * :attr:`id` — stable :class:`AppModelId` (registry key).
    * :attr:`title` — human-readable display title (1..200 chars).
    * :attr:`taxonomy` — classification path through the taxonomy tree.
    * :attr:`enabled` — registry-level enable flag (legacy
      ``hidden`` / disabled state). Disabled definitions remain
      addressable but cannot start a new :class:`Run`.
    * :attr:`pinned` — UI sort hint (top of list when ``True``).
    * :attr:`input_presets` — tuple of preset bundles offered to users.
    * :attr:`required_catalog_ids` — opaque catalog ids the runner
      adapter will need to materialise the model. The domain treats
      these as opaque strings; ``model_catalog`` owns their semantics.
    * :attr:`user_imported` — provenance flag. ``False`` for built-in
      models seeded from bundled Packs; ``True`` for models the user
      imported themselves. Built-in models are protected from deletion
      (V1 parity: only user-imported models may be removed).
    * :attr:`version` — semantic version string (``"major.minor.patch"``).
      V1 carried the version inside the Pack ``manifest.json`` and bumped
      the patch on re-import under ``conflict_policy="bump"``
      (``backend/app_builder/importer.py:_bump_patch``). Defaults to
      ``"1.0.0"`` so built-in seeds and pre-existing rows are well-formed.
    """

    id: AppModelId
    title: str
    taxonomy: Taxonomy
    enabled: bool = True
    pinned: bool = False
    input_presets: tuple[InputPreset, ...] = field(default_factory=tuple)
    required_catalog_ids: tuple[str, ...] = field(default_factory=tuple)
    user_imported: bool = False
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not isinstance(self.title, str):
            raise ValueError(
                f"title must be str, got {type(self.title).__name__}"
            )
        title = self.title.strip()
        if not title:
            raise ValueError("title must be a non-empty string")
        if len(self.title) > 200:
            raise ValueError(
                f"title must be <= 200 chars, got {len(self.title)}"
            )
        if not isinstance(self.input_presets, tuple):
            raise ValueError("input_presets must be a tuple")
        if not isinstance(self.required_catalog_ids, tuple):
            raise ValueError("required_catalog_ids must be a tuple")
        seen: set[str] = set()
        for preset in self.input_presets:
            if preset.name in seen:
                raise ValueError(
                    f"duplicate input preset name: {preset.name!r}"
                )
            seen.add(preset.name)
        for cid in self.required_catalog_ids:
            if not isinstance(cid, str) or not cid.strip():
                raise ValueError(
                    "required_catalog_ids entries must be non-empty strings"
                )
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")

    @property
    def is_runnable(self) -> bool:
        """Return True iff a new :class:`Run` may be started from this."""
        return self.enabled
