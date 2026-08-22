# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Spawn preflight — the checks that decide whether a sub-agent may exist.

This is admission policy, deliberately separated from the spawn mechanics in
:mod:`qai.chat.adapters.subagent_bridge`: the bridge answers "how is a
sub-agent started and tracked", this module answers "should it be started at
all".  The two change for entirely different reasons — new guards (blocked
agent types, per-conversation quotas, budget ceilings) land here without
touching job registration or task lifecycle.

Every rejection is a :class:`SubAgentError` of kind
:attr:`SubAgentErrorKind.PREFLIGHT`, raised BEFORE any job is registered, so a
refused spawn leaves no trace in the job registry for a caller to clean up.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from qai.chat.domain.sub_agent_error import SubAgentError, SubAgentErrorKind
from qai.chat.domain.sub_agent_spawn_request import SubAgentSpawnRequest
from qai.platform.logging import get_logger

__all__ = ["SpawnPreflight"]

_log = get_logger(__name__)

#: Recursion ceiling used when no configured value is reachable.  Deliberately
#: conservative: an unconfigured deployment should refuse deep nesting rather
#: than allow an unbounded spawn tree.
_FALLBACK_MAX_DEPTH = 2


class SpawnPreflight:
    """Decides whether one spawn request is admissible.

    Args:
        chat_settings: settings object read for the recursion ceiling.
        max_depth_reader: optional async override consulted per spawn, for a
            ceiling the user can retune without a restart.  When ``None`` the
            ceiling comes from ``chat_settings``.
    """

    __slots__ = ("_chat_settings", "_max_depth_reader")

    def __init__(
        self,
        *,
        chat_settings: Any | None = None,
        max_depth_reader: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._chat_settings = chat_settings
        self._max_depth_reader = max_depth_reader

    async def check(
        self,
        *,
        child_depth: int,
        parent_agent_type: str | None,
        spawn_request: SubAgentSpawnRequest,
    ) -> None:
        """Return silently when the spawn is allowed; raise when it is not.

        Args:
            child_depth: the depth the sub-agent WOULD occupy (parent + 1).
            parent_agent_type: the spawning agent's own type, or ``None`` for
                the main agent (which is not itself a sub-agent).
            spawn_request: the request being admitted.

        Raises:
            SubAgentError: kind ``PREFLIGHT``, carrying a recovery hint the
                parent agent can act on.
        """
        max_depth = await self.max_depth()
        if child_depth > max_depth:
            _reject(
                reason="depth",
                message=(
                    f"Cannot spawn: max recursion depth {max_depth} reached "
                    f"(child would be depth {child_depth})"
                ),
                hint=(
                    "Reduce nesting; delegate directly from a shallower agent."
                ),
            )
        # A same-type self-spawn is the shape that most often runs away: the
        # child inherits the parent's instructions and re-derives the same
        # plan, so it recurses without converging.
        if parent_agent_type and spawn_request.agent_type == parent_agent_type:
            _reject(
                reason="blocked_agent",
                message=(
                    f"Cannot spawn: agent type '{spawn_request.agent_type}' "
                    "cannot spawn itself"
                ),
                hint=(
                    "Use a different agent type (e.g., 'general') to delegate "
                    "similar work."
                ),
            )

    async def max_depth(self) -> int:
        """The recursion ceiling in force for the next spawn."""
        if self._max_depth_reader is not None:
            return int(await self._max_depth_reader())
        if self._chat_settings is None:
            return _FALLBACK_MAX_DEPTH
        return int(self._chat_settings.max_subagent_recursion_depth)


def _reject(*, reason: str, message: str, hint: str) -> None:
    """Log the rejection and raise it.  Never returns."""
    _log.info(
        "subagent.bridge.preflight_rejected",
        extra={"reason": reason},
    )
    raise SubAgentError(
        kind=SubAgentErrorKind.PREFLIGHT,
        message=message,
        recovery_hint=hint,
    )
