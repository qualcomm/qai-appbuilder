# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the skill registry."""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import SkillRegistryPort
from qai.ai_coding.domain import Skill, make_skill_registered_event
from qai.platform.events import EventBus


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterSkillCommand:
    """Input for :class:`RegisterSkillUseCase`."""

    skill: Skill


class RegisterSkillUseCase:
    """Application service for advertising a new skill."""

    def __init__(
        self,
        *,
        skill_registry: SkillRegistryPort,
        event_bus: EventBus,
    ) -> None:
        self._skill_registry = skill_registry
        self._event_bus = event_bus

    async def execute(self, command: RegisterSkillCommand) -> Skill:
        await self._skill_registry.register(command.skill)
        await self._event_bus.publish(make_skill_registered_event(command.skill))
        return command.skill


class DiscoverSkillsUseCase:
    """Application service that returns the currently advertised skills."""

    def __init__(self, *, skill_registry: SkillRegistryPort) -> None:
        self._skill_registry = skill_registry

    async def execute(self) -> list[Skill]:
        return await self._skill_registry.list_skills()


__all__ = [
    "DiscoverSkillsUseCase",
    "RegisterSkillCommand",
    "RegisterSkillUseCase",
]
