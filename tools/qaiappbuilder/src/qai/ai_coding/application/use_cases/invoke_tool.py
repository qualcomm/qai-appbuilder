# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: invoke a tool from a coding session.

Pre-condition: the session has an *approved* permission request for
``tool_name``.  The application root is responsible for orchestrating
the request → decide → invoke flow; this use case only handles the
invocation step so it can be unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.ai_coding.application.ports import (
    CodingSessionRepositoryPort,
    ToolBridgePort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    InvalidSessionStateError,
    ToolInvocation,
    ToolInvocationId,
    ToolName,
)
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class InvokeToolCommand:
    """Input for :class:`InvokeToolUseCase`."""

    session_id: CodingSessionId
    tool_name: ToolName
    args: dict[str, Any]


class InvokeToolUseCase:
    """Application service for dispatching a tool invocation."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        tool_bridge: ToolBridgePort,
        clock: Clock,
        ids: IdGenerator,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._tool_bridge = tool_bridge
        self._clock = clock
        self._ids = ids
        self._event_bus = event_bus

    async def execute(self, command: InvokeToolCommand) -> ToolInvocation:
        session = await self._repository.get(command.session_id)
        invocation_id = ToolInvocationId(value=self._ids.new_id())
        session.start_tool_invocation(
            invocation_id=invocation_id,
            tool_name=command.tool_name,
            args=command.args,
            now=self._clock.now(),
        )

        try:
            result = await self._tool_bridge.invoke(
                tool_name=command.tool_name,
                args=command.args,
            )
        except InvalidSessionStateError:
            raise
        except Exception as exc:
            session.fail_tool_invocation(
                invocation_id=invocation_id,
                error_code="ai_coding.tool_bridge_error",
                now=self._clock.now(),
            )
            await self._repository.save(session)
            for event in session.drain_events():
                await self._event_bus.publish(event)
            raise InvalidSessionStateError(
                message=f"tool bridge raised: {exc!r}",
            ) from exc

        if result.ok:
            invocation = session.complete_tool_invocation(
                invocation_id=invocation_id,
                result=result.result or {},
                now=self._clock.now(),
            )
        else:
            invocation = session.fail_tool_invocation(
                invocation_id=invocation_id,
                error_code=result.error_code or "ai_coding.tool_failed",
                now=self._clock.now(),
            )

        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        return invocation


__all__ = ["InvokeToolCommand", "InvokeToolUseCase"]
