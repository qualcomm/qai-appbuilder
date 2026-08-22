# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: read / mutate per-instance :class:`ChannelBindings` (PR-202).

Bindings map ``conversation_id -> channel_user_id`` so the WebUI's
chat tab can post a reply that the channel sync layer pushes back out
to the bound channel-side user.  PR-202 only persists the mapping;
the cross-context sync that consumes it lands in PR-205.

Pattern matches :mod:`manage_settings`: load aggregate ⇒ compute new
:class:`ChannelBindings` ⇒ persist via
:meth:`ChannelInstance.with_bindings`.  Empty ``channel_user_id`` on
the bind use case is treated as an unbind (the
:meth:`ChannelBindings.with_binding` helper enforces this so the
legacy semantics — ``POST /api/wechat/bindings`` with an empty
``wechat_user_id`` clearing the row — round-trip exactly).
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.time import Clock

from qai.channels.application.ports import ChannelInstanceRepositoryPort
from qai.channels.domain import (
    ChannelBindings,
    ChannelInstance,
    ChannelInstanceId,
)


class GetChannelBindingsUseCase:
    """Return the persisted :class:`ChannelBindings` for one instance."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock  # symmetry with mutators

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> ChannelBindings:
        instance = await self._instances.get(instance_id)
        return instance.get_bindings()


@dataclass(frozen=True, slots=True, kw_only=True)
class BindChannelConversationCommand:
    """Inbound command for :class:`BindChannelConversationUseCase`.

    Empty ``channel_user_id`` removes the binding (matches the legacy
    semantics of ``POST /api/wechat/bindings``).
    """

    instance_id: ChannelInstanceId
    conversation_id: str
    channel_user_id: str


class BindChannelConversationUseCase:
    """Set / replace / clear a single conversation→channel-user binding."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock

    async def execute(
        self, command: BindChannelConversationCommand
    ) -> ChannelInstance:
        instance = await self._instances.get(command.instance_id)
        existing = instance.get_bindings()
        new_bindings = existing.with_binding(
            conversation_id=command.conversation_id,
            channel_user_id=command.channel_user_id,
        )
        updated = instance.with_bindings(
            new_bindings, now=self._clock.now()
        )
        await self._instances.save(updated)
        return updated


@dataclass(frozen=True, slots=True, kw_only=True)
class UnbindChannelConversationCommand:
    """Inbound command for :class:`UnbindChannelConversationUseCase`."""

    instance_id: ChannelInstanceId
    conversation_id: str


class UnbindChannelConversationUseCase:
    """Remove the binding for ``conversation_id`` (no-op if absent)."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock

    async def execute(
        self, command: UnbindChannelConversationCommand
    ) -> ChannelInstance:
        instance = await self._instances.get(command.instance_id)
        existing = instance.get_bindings()
        new_bindings = existing.without_binding(
            conversation_id=command.conversation_id
        )
        updated = instance.with_bindings(
            new_bindings, now=self._clock.now()
        )
        await self._instances.save(updated)
        return updated


__all__ = [
    "GetChannelBindingsUseCase",
    "BindChannelConversationCommand",
    "BindChannelConversationUseCase",
    "UnbindChannelConversationCommand",
    "UnbindChannelConversationUseCase",
]
