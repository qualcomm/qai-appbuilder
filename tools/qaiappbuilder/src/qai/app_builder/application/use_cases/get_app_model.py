# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetAppModelUseCase`` — fetch a single app model definition."""

from __future__ import annotations

from qai.app_builder.application.ports import AppModelRepositoryPort
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.value_objects import AppModelId

__all__ = ["GetAppModelUseCase"]


class GetAppModelUseCase:
    """Return a single :class:`AppModelDefinition` by id."""

    def __init__(self, *, app_models: AppModelRepositoryPort) -> None:
        self._app_models = app_models

    async def execute(self, *, model_id: AppModelId) -> AppModelDefinition:
        return await self._app_models.get(model_id)
