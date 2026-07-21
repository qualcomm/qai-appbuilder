# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListModelsUseCase`` — list available models on disk."""

from __future__ import annotations

from qai.model_runtime.application.ports import InferenceServicePort
from qai.model_runtime.domain.entities import ModelInfo


class ListModelsUseCase:
    """List locally-available model files.

    *models_root* (V1 ``service_launch.models_root_path``) is optional; when
    omitted the adapter scans its configured install dir.
    """

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(
        self, *, models_root: str | None = None
    ) -> list[ModelInfo]:
        return await self._service.list_models(models_root=models_root)


__all__ = ["ListModelsUseCase"]
