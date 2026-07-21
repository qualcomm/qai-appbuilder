# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: decide a pending permission request.

PR-095 (S9 audit §2.2 H-13) extends :class:`DecidePermissionCommand`
with two optional fields — ``updated_input`` and
``updated_permissions`` — so the upstream Anthropic permission-resolve
API (or the OpenCode equivalent) can carry the operator's edits to
the tool input arguments and the per-tool permission overrides at
decide-time.  Both fields are appended at the END of the dataclass
and default to ``None`` so all historical callers keep working
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    PermissionDecision,
    PermissionRequest,
    PermissionRequestId,
)
from qai.platform.events import EventBus
from qai.platform.time import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class DecidePermissionCommand:
    """Input for :class:`DecidePermissionUseCase`."""

    session_id: CodingSessionId
    request_id: PermissionRequestId
    decision: PermissionDecision
    # PR-095 / S9 H-13: operator-edited tool input.  When supplied,
    # the provider's ``can_use_tool`` callback forwards this dict to
    # the upstream so the LLM sees the corrected arguments rather
    # than its original (possibly hallucinated) ones.
    updated_input: dict[str, Any] | None = None
    # PR-095 / S9 H-13: per-tool permission overrides applied with
    # this decision (e.g. {"Read": "always_allow", "Bash": "deny"}).
    # Provider forwards into the upstream permission-resolve payload.
    updated_permissions: list[dict[str, Any]] | None = None


class DecidePermissionUseCase:
    """Application service for resolving a permission request."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        clock: Clock,
        event_bus: EventBus,
        provider_port: CodingProviderPort | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._event_bus = event_bus
        # Optional — when wired the use case forwards the decision to
        # the upstream so its in-flight stream sees the override.
        # Tests / offline tooling can omit this argument.
        self._provider_port = provider_port

    async def execute(self, command: DecidePermissionCommand) -> PermissionRequest:
        session = await self._repository.get(command.session_id)
        request = session.decide_permission(
            request_id=command.request_id,
            decision=command.decision,
            now=self._clock.now(),
        )
        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)

        # PR-095 / S9 H-13: when the provider exposes a duck-typed
        # ``forward_permission_decision`` hook, ferry the operator's
        # edits to the upstream ``can_use_tool`` callback so the
        # in-flight stream sees the corrected input + overrides.
        if self._provider_port is not None:
            forward = getattr(
                self._provider_port, "forward_permission_decision", None
            )
            if callable(forward):
                await forward(
                    session_id=command.session_id,
                    request_id=command.request_id,
                    decision=command.decision,
                    updated_input=command.updated_input,
                    updated_permissions=command.updated_permissions,
                )
        return request


__all__ = ["DecidePermissionCommand", "DecidePermissionUseCase"]
