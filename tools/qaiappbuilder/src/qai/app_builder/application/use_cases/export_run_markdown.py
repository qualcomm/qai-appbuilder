# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Export run as Markdown — application use case (PR-094 §17.5 #14).

Loads a :class:`qai.app_builder.domain.run.Run` via
:class:`RunRepositoryPort` and forwards it to a
:class:`RunMarkdownRendererPort` adapter for actual rendering. The use
case stays clean of any HTTP / framework dependency — the route layer
only calls :meth:`execute` and writes the resulting string to a
``Response(media_type='text/markdown')``.

Restores the legacy ``GET /api/appbuilder/runs/{run_id}/export`` flow
(``backend/app_builder/api_routes.py:1695-1720``) inside the new
clean-architecture stack. The renderer is wired by
``apps/api/_app_builder_di.py`` so the application layer does not
import the concrete Markdown formatter from
:mod:`qai.app_builder.infrastructure.run_exporter` directly — the
``layered-app_builder`` import-linter contract forbids
``application -> infrastructure``.
"""

from __future__ import annotations

from qai.app_builder.application.ports import (
    RunMarkdownRendererPort,
    RunRepositoryPort,
)
from qai.app_builder.domain.value_objects import RunId

__all__ = ["ExportRunMarkdownUseCase"]


class ExportRunMarkdownUseCase:
    """Export one run aggregate as a Markdown report string.

    Raises :class:`qai.app_builder.domain.errors.RunNotFoundError` when
    the requested run id is unknown — surfaced by the route as 404.
    """

    __slots__ = ("_runs", "_renderer")

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        renderer: RunMarkdownRendererPort,
    ) -> None:
        self._runs = runs
        self._renderer = renderer

    async def execute(self, *, run_id: RunId) -> str:
        run = await self._runs.get(run_id)
        return self._renderer.render(run)
