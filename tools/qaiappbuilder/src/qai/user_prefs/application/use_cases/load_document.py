# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``LoadDocumentUseCase`` — read a single user-prefs document by key.

Thin orchestrator: validates the raw key string into a
:class:`PrefsKey`, delegates to the repository, returns the document.
The validation is here (not at the route layer) so every entry point
into the BC — HTTP routes, future CLI / Tauri bindings, in-process
tests — gets identical key invariants for free.
"""
from __future__ import annotations

from dataclasses import dataclass

from qai.user_prefs.application.ports import UserPrefsRepositoryPort
from qai.user_prefs.domain import PrefsDocument, PrefsKey

__all__ = ["LoadDocumentUseCase"]


@dataclass(slots=True, frozen=True)
class LoadDocumentUseCase:
    """Return the persisted document for ``raw_key`` or ``{}`` if absent.

    ``raw_key`` is a plain ``str`` (FastAPI query / path / body inputs
    arrive as strings); the use case constructs the validated
    :class:`PrefsKey` so the route layer can stay declarative.
    """

    repository: UserPrefsRepositoryPort

    async def execute(self, raw_key: str) -> PrefsDocument:
        key = PrefsKey.from_string(raw_key)
        return await self.repository.load(key)
