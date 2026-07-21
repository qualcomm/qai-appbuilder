# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SaveDocumentUseCase`` — shallow-merge updates into a user-prefs doc.

Wraps the repository ``save`` with:

* :class:`PrefsKey` validation;
* an optional top-level whitelist (``allowed_top_level``) so routes
  that only mean to update one section (``/api/preferences`` →
  ``ui.*`` only) cannot be tricked into persisting unrelated keys.
"""
from __future__ import annotations

from dataclasses import dataclass

from qai.user_prefs.application.ports import UserPrefsRepositoryPort
from qai.user_prefs.domain import PrefsDocument, PrefsKey

__all__ = ["SaveDocumentUseCase"]


@dataclass(slots=True, frozen=True)
class SaveDocumentUseCase:
    """Validate inputs, run a shallow merge, return the persisted doc.

    The shallow-merge is performed here (not in the adapter) because
    the *policy* of what counts as a "merge" is application-level —
    the adapter only knows how to atomically read-modify-write a row.
    Keeping merge here also makes it trivial to unit-test the
    whitelist behaviour without touching SQLite.
    """

    repository: UserPrefsRepositoryPort

    async def execute(
        self,
        raw_key: str,
        *,
        updates: PrefsDocument,
        allowed_top_level: tuple[str, ...] | None = None,
    ) -> PrefsDocument:
        key = PrefsKey.from_string(raw_key)
        # Filter at the application layer so the persisted document
        # never contains keys the route did not authorize.
        if allowed_top_level is not None:
            filtered = {
                k: v for k, v in updates.items() if k in set(allowed_top_level)
            }
        else:
            filtered = dict(updates)
        # Repository performs the read + merge + write atomically;
        # we rely on the adapter's transactional merge so the full
        # operation stays in one DB transaction.
        return await self.repository.save(key, updates=filtered)
