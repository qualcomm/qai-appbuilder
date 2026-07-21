# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ProbeServiceUseCase`` — quick health probe of the inference daemon."""

from __future__ import annotations

from typing import Any

from qai.model_runtime.application.ports import InferenceServicePort


class ProbeServiceUseCase:
    """Quick health probe returning reachability and loaded model.

    When *host*/*port* are supplied, probes that arbitrary address (the
    V1 Connection-panel "Test" button); otherwise probes the
    locally-managed daemon.
    """

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(
        self, *, host: str | None = None, port: int | None = None
    ) -> dict[str, Any]:
        return await self._service.probe(host=host, port=port)


__all__ = ["ProbeServiceUseCase"]
