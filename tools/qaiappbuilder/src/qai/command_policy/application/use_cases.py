# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for ``qai.command_policy`` (PR-603).

Single use case wrapping :class:`ExecBrokerPort`:
* :class:`GetExecProfilesUseCase` — returns profiles + enabled state.
"""
from __future__ import annotations

from dataclasses import dataclass

from qai.command_policy.application.ports import ExecBrokerPort
from qai.command_policy.domain import CommandProfile

__all__ = [
    "GetExecProfilesUseCase",
]


@dataclass(slots=True)
class GetExecProfilesResult:
    """Return value of :class:`GetExecProfilesUseCase`."""

    profiles: list[CommandProfile]
    enabled: bool


@dataclass(slots=True)
class GetExecProfilesUseCase:
    """Return all loaded exec profiles and the broker's enabled state."""

    broker: ExecBrokerPort

    async def execute(self) -> GetExecProfilesResult:
        profiles = await self.broker.get_profiles()
        enabled = await self.broker.is_enabled()
        return GetExecProfilesResult(profiles=profiles, enabled=enabled)
