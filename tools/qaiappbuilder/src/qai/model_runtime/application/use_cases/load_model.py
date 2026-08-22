# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``LoadModelUseCase`` — load/switch a model in the running daemon."""

from __future__ import annotations

from qai.model_runtime.application.ports import InferenceServicePort


class LoadModelUseCase:
    """Load or switch to *model_name* in the running daemon."""

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(self, model_name: str) -> dict[str, str]:
        await self._service.load_model(model_name)
        return {"status": "loading", "model": model_name}


__all__ = ["LoadModelUseCase"]
