# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: read/save the ai_coding UI config document.

Backs the legacy ``GET /api/cc/config`` + ``POST /api/cc/config``
routes.  Delegates persistence to :class:`CodingConfigRepositoryPort`
which stores the document in the shared ``kv_user_prefs`` table.

Sensitive values (API keys, auth tokens) are NEVER persisted via
this use case — they go through :class:`qai.platform.persistence.secrets.SecretStore`
in the credentials use cases (PR-104b).  The route layer validates
the inbound document against a whitelist before passing it down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.ai_coding.application.ports import CodingConfigRepositoryPort


@dataclass(frozen=True, slots=True, kw_only=True)
class GetCodingConfigQuery:
    """Input for :class:`GetCodingConfigUseCase`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SaveCodingConfigCommand:
    """Input for :class:`SaveCodingConfigUseCase`."""

    updates: dict[str, Any]


class GetCodingConfigUseCase:
    """Application service for reading the ai_coding config document."""

    def __init__(self, *, repository: CodingConfigRepositoryPort) -> None:
        self._repository = repository

    async def execute(
        self, query: GetCodingConfigQuery | None = None
    ) -> dict[str, Any]:
        # Query is currently parameter-less; the parameter exists for
        # forward compatibility (e.g. per-user / per-workspace config
        # in a multi-tenant world).
        del query
        return await self._repository.load()


class SaveCodingConfigUseCase:
    """Application service for upserting the ai_coding config document."""

    def __init__(self, *, repository: CodingConfigRepositoryPort) -> None:
        self._repository = repository

    async def execute(self, command: SaveCodingConfigCommand) -> dict[str, Any]:
        return await self._repository.save(updates=command.updates)


__all__ = [
    "GetCodingConfigQuery",
    "GetCodingConfigUseCase",
    "SaveCodingConfigCommand",
    "SaveCodingConfigUseCase",
]
