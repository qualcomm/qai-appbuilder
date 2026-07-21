# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the chat experience library (PR-042 / issue d).

Four small CRUD use cases backing the front-end's experience routes.
Domain :class:`Experience` is a frozen value object so updates are
implemented as full replacements -- ``SaveExperienceUseCase`` covers
both create and update by rebuilding the aggregate.

Design notes:

* Use cases follow the same shape as PR-021's conversation_management
  module (kw-only ``__init__``, ``execute`` is the single public
  method, dependencies injected explicitly).
* No domain events are emitted: the chat experience library is a
  user-private knowledge store with no cross-context observers in the
  S4 timeframe (compare ``conversation_management.py`` which does
  emit events for cross-context channels notification flows).
* Errors propagate from the repository: ``ExperienceNotFoundError``
  for missing ids, ``ValueError`` from the domain layer for invalid
  category / content lengths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.chat.application.ports import ExperienceRepositoryPort
from qai.chat.domain.experience import Experience
from qai.chat.domain.ids import ExperienceId
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock


__all__ = [
    "SaveExperienceUseCase",
    "SaveExperienceInput",
    "ListExperiencesUseCase",
    "ListExperiencesInput",
    "DeleteExperienceUseCase",
    "DeleteExperienceInput",
    "ListExperienceCategoriesUseCase",
]


_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Save (create or update)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SaveExperienceInput:
    """Inputs to :class:`SaveExperienceUseCase`.

    ``experience_id=None`` mints a fresh id (create); a non-None id
    upserts an existing record (update).
    """

    category: str
    content: str
    metadata: dict[str, Any] | None = None
    experience_id: str | None = None


class SaveExperienceUseCase:
    """Persist an :class:`Experience` -- create or update."""

    def __init__(
        self,
        *,
        experiences: ExperienceRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._experiences = experiences
        self._clock = clock
        self._ids = ids

    async def execute(self, request: SaveExperienceInput) -> Experience:
        if request.experience_id is None:
            exp_id = ExperienceId.generate(self._ids)
            created_at = self._clock.now()
        else:
            exp_id = ExperienceId.of(request.experience_id)
            existing = await self._experiences.get(exp_id)
            created_at = existing.created_at
        experience = Experience(
            id=exp_id,
            category=request.category,
            content=request.content,
            metadata=dict(request.metadata or {}),
            created_at=created_at,
        )
        await self._experiences.save(experience)
        _log.info(
            "chat.experience_saved",
            experience_id=experience.id.value,
            category=experience.category,
        )
        return experience


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ListExperiencesInput:
    """Inputs to :class:`ListExperiencesUseCase`.

    ``category=None`` returns every experience (limited by ``limit``);
    a non-None category restricts the result set.
    """

    category: str | None = None
    limit: int = 50


class ListExperiencesUseCase:
    """Return experiences ordered by ``created_at`` DESC."""

    def __init__(
        self, *, experiences: ExperienceRepositoryPort
    ) -> None:
        self._experiences = experiences

    async def execute(
        self, request: ListExperiencesInput
    ) -> tuple[Experience, ...]:
        return await self._experiences.list(
            category=request.category,
            limit=request.limit,
        )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteExperienceInput:
    """Inputs to :class:`DeleteExperienceUseCase`."""

    experience_id: str


class DeleteExperienceUseCase:
    """Remove an experience; raise :class:`ExperienceNotFoundError`."""

    def __init__(
        self, *, experiences: ExperienceRepositoryPort
    ) -> None:
        self._experiences = experiences

    async def execute(self, request: DeleteExperienceInput) -> None:
        exp_id = ExperienceId.of(request.experience_id)
        await self._experiences.delete(exp_id)
        _log.info(
            "chat.experience_deleted", experience_id=exp_id.value
        )


# ---------------------------------------------------------------------------
# List categories
# ---------------------------------------------------------------------------
class ListExperienceCategoriesUseCase:
    """Return distinct categories currently in use, sorted ascending."""

    def __init__(
        self, *, experiences: ExperienceRepositoryPort
    ) -> None:
        self._experiences = experiences

    async def execute(self) -> tuple[str, ...]:
        return await self._experiences.list_categories()
