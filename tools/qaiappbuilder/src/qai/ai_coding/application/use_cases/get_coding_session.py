# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: read a single :class:`CodingSession` by id.

Wraps :meth:`CodingSessionRepositoryPort.get` so the route layer
(``GET /api/cc/sessions/{id}``) goes through the application layer
instead of touching the repository directly.  Keeps the route layer
free of repository imports and lets a future audit/permission
decoration sit at this level.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSession, CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class GetCodingSessionQuery:
    """Input for :class:`GetCodingSessionUseCase`."""

    session_id: CodingSessionId


class GetCodingSessionUseCase:
    """Application service for reading a single coding session.

    Raises :class:`CodingSessionNotFoundError` (propagated from the
    repository) if the id is unknown; the route layer surfaces this
    as HTTP 404 via the unified error handler.
    """

    def __init__(self, *, repository: CodingSessionRepositoryPort) -> None:
        self._repository = repository

    async def execute(self, query: GetCodingSessionQuery) -> CodingSession:
        return await self._repository.get(query.session_id)


__all__ = ["GetCodingSessionQuery", "GetCodingSessionUseCase"]
