# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StartServiceUseCase`` — start the local inference daemon."""

from __future__ import annotations

from qai.model_runtime.application.ports import InferenceServicePort


class StartServiceUseCase:
    """Start the inference daemon, optionally loading a model."""

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(
        self,
        *,
        model_name: str | None = None,
        port: int | None = None,
        loglevel: int | None = None,
    ) -> dict[str, str]:
        await self._service.start(
            model_name=model_name, port=port, loglevel=loglevel
        )
        return {"status": "starting"}


__all__ = ["StartServiceUseCase"]
