# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: list :class:`CodingSession` aggregates.

Two scopes are supported, mirroring the legacy routes:

* ``active`` — backs ``GET /api/cc/sessions`` and ``GET /api/oc/sessions``.
* ``all``    — backs ``GET /api/cc/sessions/history/all`` and the
  OpenCode equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSession


@dataclass(frozen=True, slots=True, kw_only=True)
class ListCodingSessionsQuery:
    """Input for :class:`ListCodingSessionsUseCase`."""

    scope: Literal["active", "all"] = "active"


class ListCodingSessionsUseCase:
    """Application service for listing coding sessions."""

    def __init__(self, *, repository: CodingSessionRepositoryPort) -> None:
        self._repository = repository

    async def execute(self, query: ListCodingSessionsQuery) -> list[CodingSession]:
        if query.scope == "active":
            return await self._repository.list_active()
        return await self._repository.list_all()


__all__ = ["ListCodingSessionsQuery", "ListCodingSessionsUseCase"]
