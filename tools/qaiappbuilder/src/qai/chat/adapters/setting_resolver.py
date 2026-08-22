# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Setting-source resolver for the chat agentic loop.

Migrated into the chat bounded context from the (removed) ``ai_coding``
agent harness ``setting_resolver.py``.  Merges named setting overlays
into one :class:`qai.chat.application.ports.ResolvedSettings` view.

Convention (V1 parity): earlier names in ``order`` win, so
``order=("user", "global")`` means user values override global.  This
mirrors the V1 ``forge_config.json`` deep-merge (user prefs over global
defaults); V1 has no per-project rule file so only ``global`` + ``user``
layers exist.

The source *content* is supplied by the apps layer at construction time
(it may read ``qai.user_prefs`` through a bridge — chat never imports
that context directly).  This adapter only knows how to combine the
mappings it is given.

Cross-context isolation
-----------------------
Imports only ``qai.chat.application`` + stdlib.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from qai.chat.application.ports import ResolvedSettings, SettingResolverPort

__all__ = ["DictSettingResolver"]


class DictSettingResolver(SettingResolverPort):
    """Combine pre-loaded named mappings into a flat, merged view.

    Args:
        sources: ``{source_name: mapping}`` — the content of each named
            overlay (e.g. ``{"global": {...}, "user": {...}}``).  The
            apps layer assembles this from forge_config + user_prefs.
    """

    __slots__ = ("_sources",)

    def __init__(self, *, sources: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._sources: dict[str, Mapping[str, Any]] = dict(sources or {})

    def resolve(self, *, order: tuple[str, ...]) -> ResolvedSettings:
        merged: dict[str, Any] = {}
        applied: list[str] = []
        missing: list[str] = [name for name in order if name not in self._sources]

        # Walk reverse so earlier names overwrite later updates.
        for name in reversed(order):
            src = self._sources.get(name)
            if src is None:
                continue
            for key, value in src.items():
                merged[key] = value
            applied.append(name)

        applied.reverse()  # mirror ``order`` (highest precedence first)
        return ResolvedSettings(
            values=merged,
            order=tuple(applied),
            missing=tuple(missing),
        )
