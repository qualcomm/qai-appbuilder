# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StopServiceUseCase`` — stop the local inference daemon."""

from __future__ import annotations

from qai.model_runtime.application.ports import InferenceServicePort


class StopServiceUseCase:
    """Stop the running inference daemon."""

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(self) -> dict[str, str]:
        await self._service.stop()
        return {"status": "stopped"}


__all__ = ["StopServiceUseCase"]
