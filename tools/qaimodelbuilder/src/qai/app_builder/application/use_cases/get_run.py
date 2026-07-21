# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetRunUseCase`` — read a single :class:`Run` by id.

Thin wrapper over :class:`RunRepositoryPort.get` so the route layer
goes through the application layer (rather than reaching for the
repository port directly, as PR-034 had to do for lack of a use case).
"""

from __future__ import annotations

from qai.app_builder.application.ports import RunRepositoryPort
from qai.app_builder.domain.run import Run
from qai.app_builder.domain.value_objects import RunId

__all__ = ["GetRunUseCase"]


class GetRunUseCase:
    """Look up a :class:`Run` aggregate by its :class:`RunId`."""

    def __init__(self, *, runs: RunRepositoryPort) -> None:
        self._runs = runs

    async def execute(self, *, run_id: RunId) -> Run:
        """Return the :class:`Run` with this id.

        Raises :class:`qai.app_builder.domain.errors.RunNotFoundError`
        if no such run exists (the port contract).
        """
        return await self._runs.get(run_id)
