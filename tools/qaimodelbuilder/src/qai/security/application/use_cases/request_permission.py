# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Request a fresh permission approval workflow item."""

from __future__ import annotations

from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

from qai.security.application.permission_wait import PermissionWaitRegistry
from qai.security.domain.entities import PermissionRequest
from qai.security.domain.events import (
    PermissionApprovedEvent,
    PermissionRejectedEvent,
    PermissionRequestedEvent,
)
from qai.security.domain.value_objects import (
    AceMask,
    RequestId,
    Resource,
    Subject,
)

from ..ports import (
    PermissionRequestRepositoryPort,
    SmartApprovalDecision,
    SmartApprovalPort,
)

__all__ = ["RequestPermissionUseCase"]

logger = get_logger(__name__)


class RequestPermissionUseCase:
    """Create a PENDING :class:`PermissionRequest` and publish the event.

    F-7: when a :class:`SmartApprovalPort` is wired, the freshly created
    request is immediately evaluated by the smart-approval heuristic:

    * ``APPROVE``    → auto-approve the request (publish
      :class:`PermissionApprovedEvent`); the returned request is APPROVED.
    * ``REJECT``     → auto-reject (publish :class:`PermissionRejectedEvent`);
      the returned request is REJECTED.
    * ``UNDECIDED``  → leave the request PENDING for a human reviewer
      (the historical behaviour).

    The port is optional — when ``None`` the use case behaves exactly as
    before (always returns a PENDING request), keeping every existing
    caller / test green.
    """

    def __init__(
        self,
        *,
        request_repository: PermissionRequestRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        ids: IdGenerator,
        smart_approval: SmartApprovalPort | None = None,
        wait_registry: PermissionWaitRegistry | None = None,
    ) -> None:
        self._requests = request_repository
        self._events = event_bus
        self._clock = clock
        self._ids = ids
        self._smart_approval = smart_approval
        self._wait_registry = wait_registry

    async def execute(
        self,
        *,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
        reason: str = "",
        reason_code: str = "",
        reason_args: dict[str, str] | None = None,
    ) -> PermissionRequest:
        """Create a PENDING permission request and publish the event.

        ``reason`` (TAIL-appended, optional, default ``""``) carries a
        human-readable explanation of *why* this request needs manual
        confirmation — e.g. the exec-broker "dangerous but possibly
        legitimate command" rationale. It is forwarded verbatim onto the
        :class:`PermissionRequestedEvent` so the SSE frame / front-end
        authorization dialog can surface it. Existing callers that omit
        ``reason`` keep the historical behaviour (empty reason).
        """
        if requested_mask.is_empty():
            raise ValueError("requested_mask must have at least one bit set")
        now = self._clock.now()
        request_id = RequestId(value=self._ids.new_id())
        request = PermissionRequest.create(
            request_id=request_id,
            subject=subject,
            resource=resource,
            requested_mask=requested_mask,
            now=now,
        )
        await self._requests.save(request)
        await self._events.publish(
            PermissionRequestedEvent(
                request_id=request_id,
                subject=subject,
                resource=resource,
                requested_mask=requested_mask,
                occurred_at=now,
                reason=reason,
                reason_code=reason_code,
                reason_args=dict(reason_args or {}),
            )
        )

        # F-7: consult the smart-approval heuristic (if wired) to
        # auto-resolve the brand-new request.
        if self._smart_approval is not None:
            resolved = await self._apply_smart_approval(
                request=request,
                subject=subject,
                resource=resource,
                requested_mask=requested_mask,
            )
            if resolved is not None:
                return resolved

        return request

    async def _apply_smart_approval(
        self,
        *,
        request: PermissionRequest,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
    ) -> PermissionRequest | None:
        """Evaluate + auto-resolve via the smart-approval port.

        Returns the resolved (APPROVED/REJECTED) request, or ``None`` when
        the heuristic abstains (UNDECIDED) or evaluation fails — in both
        cases the request stays PENDING (fail-open to human review, never
        fail-open to auto-approval).
        """
        assert self._smart_approval is not None  # guarded by caller
        try:
            decision = await self._smart_approval.evaluate(
                subject=subject,
                resource=resource,
                requested_mask=requested_mask,
            )
        except Exception as exc:  # noqa: BLE001 — never let a heuristic 500 the request
            logger.warning(
                "security.request_permission.smart_approval_failed",
                request_id=request.request_id.value,
                error=str(exc),
            )
            return None

        now = self._clock.now()
        reason = "smart-approval auto-decision"
        if decision is SmartApprovalDecision.APPROVE:
            approved = request.approve(now=now, reason=reason)
            await self._requests.save(approved)
            await self._events.publish(
                PermissionApprovedEvent(
                    request_id=request.request_id,
                    subject=subject,
                    resource=resource,
                    granted_mask=requested_mask,
                    decided_by=None,
                    occurred_at=now,
                )
            )
            logger.info(
                "security.request_permission.smart_approved",
                request_id=request.request_id.value,
            )
            # P0 ASK restore — wake a FileGuard waiter (if any) so the
            # smart auto-approval immediately unblocks the synchronous tool
            # call instead of waiting for the 60s timeout.
            if self._wait_registry is not None:
                self._wait_registry.resolve(
                    request.request_id.value, allow=True, scope="once"
                )
            return approved
        if decision is SmartApprovalDecision.REJECT:
            rejected = request.reject(now=now, reason=reason)
            await self._requests.save(rejected)
            await self._events.publish(
                PermissionRejectedEvent(
                    request_id=request.request_id,
                    subject=subject,
                    resource=resource,
                    decided_by=None,
                    reason=reason,
                    occurred_at=now,
                )
            )
            logger.info(
                "security.request_permission.smart_rejected",
                request_id=request.request_id.value,
            )
            if self._wait_registry is not None:
                self._wait_registry.resolve(
                    request.request_id.value, allow=False, scope="deny"
                )
            return rejected
        # UNDECIDED → stay PENDING.
        return None
