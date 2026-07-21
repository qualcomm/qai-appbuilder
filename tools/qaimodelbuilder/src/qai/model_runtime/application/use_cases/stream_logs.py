# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StreamLogsUseCase`` — stream daemon log lines for the SSE endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator

from qai.model_runtime.application.ports import InferenceServicePort


class StreamLogsUseCase:
    """Stream log lines (buffered then live) from the inference daemon.

    Mirrors V1's SSE log stream: buffered lines with sequence ``>= skip``
    are emitted first, then new lines event-driven until the daemon exits.
    """

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    def execute(self, *, skip: int = 0) -> AsyncIterator[str]:
        return self._service.stream_logs(skip=skip)


__all__ = ["StreamLogsUseCase"]
