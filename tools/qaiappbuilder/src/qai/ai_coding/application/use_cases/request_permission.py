# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: request authorisation for a tool invocation.

The provider's stream surfaces a permission_request frame; the use
case captures it on the aggregate and consults
:class:`PermissionDecisionPort` to see if the policy can short-circuit
to APPROVED / REJECTED.  When the policy returns ``PENDING`` the
session stays in ``PERMISSION_REQUESTED`` until
:class:`DecidePermissionUseCase` is invoked by the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.ai_coding.application.ports import (
    CodingSessionRepositoryPort,
    PermissionDecisionPort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    PermissionDecision,
    PermissionRequest,
    PermissionRequestId,
    ToolName,
)
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestPermissionCommand:
    """Input for :class:`RequestPermissionUseCase`."""

    session_id: CodingSessionId
    tool_name: ToolName
    args: dict[str, Any]


class RequestPermissionUseCase:
    """Application service for issuing a permission request."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        decision_policy: PermissionDecisionPort,
        clock: Clock,
        ids: IdGenerator,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._decision_policy = decision_policy
        self._clock = clock
        self._ids = ids
        self._event_bus = event_bus

    async def execute(self, command: RequestPermissionCommand) -> PermissionRequest:
        session = await self._repository.get(command.session_id)
        request_id = PermissionRequestId(value=self._ids.new_id())
        request = session.request_permission(
            request_id=request_id,
            tool_name=command.tool_name,
            args=command.args,
            now=self._clock.now(),
        )

        # Consult the policy: if it returns APPROVED / REJECTED we
        # decide on behalf of the user immediately so the agent does
        # not stall on a no-op prompt.
        policy_decision = await self._decision_policy.evaluate(
            request=request,
            workspace=session.workspace,
        )
        if policy_decision is not PermissionDecision.PENDING:
            session.decide_permission(
                request_id=request_id,
                decision=policy_decision,
                now=self._clock.now(),
            )

        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        return request


__all__ = ["RequestPermissionCommand", "RequestPermissionUseCase"]
