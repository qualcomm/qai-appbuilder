# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ClearContextUseCase`` — ask the running daemon to clear its context."""

from __future__ import annotations

from qai.model_runtime.application.ports import InferenceServicePort


class ClearContextUseCase:
    """Call the running daemon's ``/clear`` endpoint."""

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(self) -> dict[str, str]:
        await self._service.clear_context()
        return {"status": "cleared"}


__all__ = ["ClearContextUseCase"]
