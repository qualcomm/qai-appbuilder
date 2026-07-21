# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListRunArtifactsUseCase`` — list the artifacts attached to a Run.

Thin wrapper over :class:`RunRepositoryPort.get` that returns the
``artifacts`` tuple only. Splitting it from :class:`GetRunUseCase`
keeps the route layer's ``/runs/{id}/artifacts`` endpoint coupled to a
named use case rather than to a generic ``run.artifacts`` projection.
"""

from __future__ import annotations

from qai.app_builder.application.ports import RunRepositoryPort
from qai.app_builder.domain.artifact import Artifact
from qai.app_builder.domain.value_objects import RunId

__all__ = ["ListRunArtifactsUseCase"]


class ListRunArtifactsUseCase:
    """Return the artifacts attached to a single :class:`Run`."""

    def __init__(self, *, runs: RunRepositoryPort) -> None:
        self._runs = runs

    async def execute(self, *, run_id: RunId) -> tuple[Artifact, ...]:
        """Return the artifact tuple for ``run_id``.

        Raises :class:`qai.app_builder.domain.errors.RunNotFoundError`
        if the run is unknown.
        """
        run = await self._runs.get(run_id)
        return run.artifacts
