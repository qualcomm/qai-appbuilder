# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for ``qai.dependency_approval`` (PR-603).

Two thin use cases that wrap the :class:`DepBrokerPort`:
* :class:`GetPendingRequestsUseCase` — returns pending requests.
* :class:`ResolveRequestUseCase` — approves or rejects a request by id.
"""
from __future__ import annotations

from dataclasses import dataclass

from qai.dependency_approval.application.ports import DepBrokerPort
from qai.dependency_approval.domain import PendingRequest

__all__ = [
    "GetPendingRequestsUseCase",
    "ResolveRequestUseCase",
]


@dataclass(slots=True)
class GetPendingRequestsUseCase:
    """Return all pending dependency-install requests."""

    broker: DepBrokerPort

    async def execute(self) -> list[PendingRequest]:
        return await self.broker.get_pending()


@dataclass(slots=True)
class ResolveRequestUseCase:
    """Approve or reject a pending dependency-install request.

    Returns ``True`` if the request was resolved, ``False`` if not found.
    """

    broker: DepBrokerPort

    async def execute(self, request_id: str, decision: str) -> bool:
        """Resolve the request.

        Args:
            request_id: Unique identifier of the pending request.
            decision: Either ``"approve"`` or ``"reject"``.
        """
        return await self.broker.resolve(request_id, decision)
