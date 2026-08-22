# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``AppModelStatusInfo`` — install/deps status surfaced on ``GET /models``.

The lean :class:`~qai.app_builder.domain.app_model.AppModelDefinition`
(DB-backed) carries no filesystem state. V1's ``GET /api/appbuilder/models``
augmented every row with weight-presence + dependency status so the gallery
could render the install state badge. This application-layer view object is
the typed contract the interfaces layer fills (via a DI-wired resolver that
combines the pack manifest + pack-root probe with the dependency checker)
and maps onto the wire DTO.

Fields (V1 parity):

* ``status``        — ``Ready`` | ``NotInstalled`` | ``Error`` (weights present?)
* ``deps_status``   — ``ready`` | ``missing`` | ``installing`` | ``None`` (not yet probed)
* ``variant_status``— per-variant ``(id, status)`` rows for multi-variant packs
* ``category``      — short legacy category badge (manifest ``category``)
* ``icon``          — taxonomy group icon token (best-effort)
* ``auto_download`` — ``True`` for a built-in pack whose runner fetches its
  weights automatically on the first Run (so the UI can say "auto-downloads
  on first run" instead of the bare ``NotInstalled`` that a needs-conversion
  import also shows). ``False`` for user imports and packs that do not
  auto-download (e.g. models requiring a manual conversion/import step).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["AppModelStatusInfo", "VariantStatusView"]


@dataclass(frozen=True, slots=True, kw_only=True)
class VariantStatusView:
    """One ``(variant_id, status)`` row for the wire DTO."""

    id: str
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AppModelStatusInfo:
    """Install + dependency status for a single app model (V1 parity)."""

    status: str = "Ready"
    deps_status: str | None = None
    variant_status: tuple[VariantStatusView, ...] = field(default_factory=tuple)
    category: str | None = None
    icon: str | None = None
    auto_download: bool = False
