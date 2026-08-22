# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListAppModelsUseCase`` — return all registered app model definitions.

Wraps :class:`AppModelRepositoryPort.list_all` and enforces the V1
invariant that the gallery only shows models whose on-disk Pack still
exists. V1 listed models by scanning the pack directory
(``registry._scan_packs``), so a deleted pack vanished automatically; V2
persists rows in ``app_builder_model_definition`` and must therefore
filter out *orphan* rows whose pack directory is gone (otherwise a
manually/externally deleted pack lingers in the list as a phantom
"Ready" model that cannot run — §🔴 State-Truth-First: disk is the
truth, the DB row is only a cache).
"""

from __future__ import annotations

from qai.app_builder.application.ports import (
    AppModelRepositoryPort,
    PackPresencePort,
)
from qai.app_builder.domain.app_model import AppModelDefinition

__all__ = ["ListAppModelsUseCase"]


class ListAppModelsUseCase:
    """Return all registered :class:`AppModelDefinition` objects.

    When a :class:`PackPresencePort` is wired, rows whose on-disk pack no
    longer exists are dropped from the result (V1 "disk is the source of
    truth" parity). A missing presence probe (lean test container) lists
    every row unchanged.
    """

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        pack_presence: PackPresencePort | None = None,
    ) -> None:
        self._app_models = app_models
        self._pack_presence = pack_presence

    async def execute(
        self,
        *,
        include_disabled: bool = True,
    ) -> tuple[AppModelDefinition, ...]:
        models = await self._app_models.list_all()
        # State-Truth-First: hide rows whose on-disk pack is gone so an
        # externally/manually deleted pack never lingers as a phantom model
        # (V1 listed by disk scan, so this happened for free).
        if self._pack_presence is not None:
            models = tuple(
                m
                for m in models
                if self._pack_presence.pack_dir_present(m.id.value)
            )
        if include_disabled:
            return models
        return tuple(m for m in models if m.enabled)
