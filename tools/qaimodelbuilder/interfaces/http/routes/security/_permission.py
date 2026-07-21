# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — Permission check / request / approve / reject / cancel endpoints. (split from security.py).

Pure-move extraction (zero behaviour change): the route handlers are
byte-identical to the originals; they were nested closures inside
``build_router`` and are now nested inside this registrar instead,
still capturing the ``container`` passed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import status
from fastapi.exceptions import HTTPException

from qai.security.application.use_cases.approve_permission import ApprovePermissionUseCase
from qai.security.application.use_cases.cancel_permission_request import (
    CancelPermissionRequestUseCase,
)
from qai.security.application.use_cases.check_permission import CheckPermissionUseCase
from qai.security.application.use_cases.reject_permission import RejectPermissionUseCase
from qai.security.application.use_cases.request_permission import RequestPermissionUseCase
from qai.security.domain.value_objects import AceMask

from ._dto import (
    ApprovePermissionRequest,
    CancelPendingPermissionBody,
    CancelPendingPermissionResponse,
    CancelPermissionRequestBody,
    CheckPermissionRequest,
    CheckPermissionResponse,
    PendingPermissionRequestsResponse,
    PermissionRequestResponse,
    RejectPermissionRequest,
    RequestPermissionRequest,
    _make_request_id,
    _mask_from_dto,
    _request_to_dto,
    _resource_from_dto,
    _subject_from_dto,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


def _op_from_mask(mask: AceMask) -> str:
    """Derive the audit ``op`` string from an :class:`AceMask` (P-08).

    Mirrors the native-hook bridge's event→op mapping
    (apps/api/_native_hook_bridge.py ``_event_to_op``) so audit rows are
    consistent across the native OS-hook path and the two HTTP callers:
    ``delete`` > ``write`` > ``exec`` > ``read`` precedence. ``delete`` is a
    write-class op (represented via the mask's own ``delete`` bit) reported
    distinctly so the audit trail can tell a delete from an in-place write;
    execute is reported as ``"exec"`` to match the native path and the
    frontend audit filter option value.
    Returns ``""`` for an empty mask (the audit column is nullable / defaults
    to empty; the use case rejects an empty mask before this matters).
    """
    if mask.delete:
        return "delete"
    if mask.write:
        return "write"
    if mask.execute:
        # NOTE: the canonical audit op string for execute is "exec" — it must
        # match the native-hook bridge (_native_hook_bridge._event_to_op → "exec")
        # AND the frontend audit filter option value (AuditLogPanel.vue "exec"),
        # otherwise the same operation would appear under two different op
        # strings and split audit grouping/filtering.
        return "exec"
    if mask.read:
        return "read"
    return ""


def _register_permission_routes(router: "APIRouter", *, container: "Container") -> None:
    # ── permission ─────────────────────────────────────────────────────

    @router.post("/permission/check", response_model=CheckPermissionResponse)
    async def check_permission(
        body: CheckPermissionRequest,
    ) -> CheckPermissionResponse:
        use_case: CheckPermissionUseCase = (
            container.security.check_permission_use_case
        )
        requested_mask = _mask_from_dto(body.requested_mask)
        result = await use_case.execute(
            subject=_subject_from_dto(body.subject),
            resource=_resource_from_dto(body.resource),
            requested_mask=requested_mask,
            correlation_id=body.correlation_id,
            # P-08 — thread the audit ``op`` (derived from the requested
            # mask) so ``/permission/check`` audit rows carry the same
            # op granularity as the native-hook path (which passes ``op``).
            op=_op_from_mask(requested_mask),
        )
        return CheckPermissionResponse(
            decision=result.decision.value,  # type: ignore[arg-type]
            matched_rule_id=result.matched_rule_id,
            matched_grant_id=result.matched_grant_id,
            audit_id=result.audit_id,
        )

    @router.post(
        "/permission/request",
        response_model=PermissionRequestResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def request_permission(
        body: RequestPermissionRequest,
    ) -> PermissionRequestResponse:
        use_case: RequestPermissionUseCase = (
            container.security.request_permission_use_case
        )
        req = await use_case.execute(
            subject=_subject_from_dto(body.subject),
            resource=_resource_from_dto(body.resource),
            requested_mask=_mask_from_dto(body.requested_mask),
        )
        return PermissionRequestResponse(request=_request_to_dto(req))

    @router.get(
        "/permission/pending",
        response_model=PendingPermissionRequestsResponse,
    )
    async def list_pending() -> PendingPermissionRequestsResponse:
        repo = container.security.permission_request_repository
        pending = await repo.list_pending()
        return PendingPermissionRequestsResponse(
            requests=[_request_to_dto(r) for r in pending],
        )

    @router.post(
        "/permission/{request_id}/approve",
        response_model=PermissionRequestResponse,
    )
    async def approve_permission(
        request_id: str,
        body: ApprovePermissionRequest,
    ) -> PermissionRequestResponse:
        use_case: ApprovePermissionUseCase = (
            container.security.approve_permission_use_case
        )
        rid = _make_request_id(request_id)
        decided_by = (
            _subject_from_dto(body.decided_by) if body.decided_by else None
        )
        # SEC true-scoping (PART D) — thread the scope context so a
        # session/process-scoped approval creates a grant keyed correctly:
        #   * scope_conversation_id — the conversation that TRIGGERED this ASK.
        #     The security PermissionRequest entity has no slot for it (and is
        #     field-locked), so the FileGuard bridge stashed it in the
        #     apps-layer ``ASK_CONVERSATION_REGISTRY`` keyed by request_id at
        #     ask time; we pop it back here (single-use). Empty when the ASK
        #     had no conversation (native-hook ASK) or predates this wiring —
        #     ApprovePermissionUseCase then keys the session grant to "".
        #   * scope_boot_id — this backend process's boot id (minted once in
        #     lifespan as ``container.boot_id``); the scope_key for a
        #     process-scoped grant.
        try:
            from apps.api._file_guard_bridge import ASK_CONVERSATION_REGISTRY

            scope_conversation_id = ASK_CONVERSATION_REGISTRY.take(request_id)
        except Exception:  # noqa: BLE001 — registry read is best-effort
            scope_conversation_id = ""
        scope_boot_id = str(getattr(container, "boot_id", "") or "")
        approved = await use_case.execute(
            request_id=rid,
            decided_by=decided_by,
            reason=body.reason,
            scope=body.grant,
            scope_conversation_id=scope_conversation_id,
            scope_boot_id=scope_boot_id,
            grant_range=body.grant_range,
        )
        return PermissionRequestResponse(request=_request_to_dto(approved))

    @router.post(
        "/permission/{request_id}/reject",
        response_model=PermissionRequestResponse,
    )
    async def reject_permission(
        request_id: str,
        body: RejectPermissionRequest,
    ) -> PermissionRequestResponse:
        use_case: RejectPermissionUseCase = (
            container.security.reject_permission_use_case
        )
        rid = _make_request_id(request_id)
        decided_by = (
            _subject_from_dto(body.decided_by) if body.decided_by else None
        )
        rejected = await use_case.execute(
            request_id=rid,
            decided_by=decided_by,
            reason=body.reason,
        )
        return PermissionRequestResponse(request=_request_to_dto(rejected))

    @router.delete(
        "/permission/{request_id}",
        response_model=PermissionRequestResponse,
    )
    async def cancel_permission(
        request_id: str,
        body: CancelPermissionRequestBody | None = None,
    ) -> PermissionRequestResponse:
        """Subject-initiated withdrawal of a pending permission request.

        Returns the cancelled :class:`PermissionRequest` (state =
        ``cancelled``). Raises :class:`PermissionRequestNotFoundError`
        (404) when no request exists, or
        :class:`PermissionRequestAlreadyResolvedError` (412) when the
        request is no longer PENDING. Issue d decision B (PR-040).
        """
        use_case: CancelPermissionRequestUseCase = (
            container.security.cancel_permission_request_use_case
        )
        rid = _make_request_id(request_id)
        cancelled_by = (
            _subject_from_dto(body.cancelled_by)
            if body and body.cancelled_by
            else None
        )
        cancelled = await use_case.execute(
            request_id=rid,
            cancelled_by=cancelled_by,
        )
        return PermissionRequestResponse(request=_request_to_dto(cancelled))

    @router.post(
        "/permission/cancel",
        response_model=CancelPendingPermissionResponse,
    )
    async def cancel_pending_permission(
        body: CancelPendingPermissionBody,
    ) -> CancelPendingPermissionResponse:
        """Phase 2 — bulk-cancel in-flight ASK popups (Phase 2 §2.3 P5).

        Body accepts EXACTLY ONE of:

        * ``{"request_id": "..."}`` — cancel one specific pending ASK
        * ``{"pid": N}`` — cancel every unresolved ASK for pid N
        * ``{"cancel_all": true}`` — cancel every pending ASK in the
          in-memory registry (the "nuke the queue" panic button)

        All variants wake the future(s) as DENY and mark the durable
        rows (if persistence is on) as ``user_cancelled``. Returns
        ``{"cancelled": [<request_ids actually woken>]}`` — ids that
        were already resolved / unknown are silently skipped, so the
        caller can compare its input against this list to detect no-ops.

        Route stays thin: enumeration + wake are done inline against the
        in-memory registry + the durable store port — no dedicated use
        case is needed because the operation is a pure operational sweep
        (no domain state machine transition; the domain PermissionRequest
        aggregate is left untouched — the operator wanted to silence the
        POPUP, not withdraw the underlying REQUEST).
        """
        # Validate: exactly one field must be truthy.
        set_fields = sum(
            [
                bool(body.request_id),
                bool(body.pid and body.pid > 0),
                bool(body.cancel_all),
            ]
        )
        if set_fields != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "exactly one of request_id / pid / cancel_all must be set"
                ),
            )

        security = container.security
        registry = security.permission_wait_registry
        store = getattr(security, "permission_pending_store", None)
        clock = container.clock

        # Enumerate the ids to cancel.
        target_ids: list[str] = []
        if body.request_id:
            target_ids = [body.request_id]
        elif body.cancel_all:
            target_ids = list(registry.list_pending())
        elif body.pid and body.pid > 0:
            # The durable store is the source of truth for pid → request_id
            # (the in-memory registry does not carry pid). Empty when the
            # store is a null / in-memory-only adapter — that's fine, the
            # caller can fall back to cancel_all.
            if store is not None:
                target_ids = list(await store.list_by_pid(int(body.pid)))

        cancelled: list[str] = []
        now = clock.now()
        for rid in target_ids:
            woke = registry.resolve(rid, allow=False, scope="deny")
            if woke:
                cancelled.append(rid)
            # Mark the durable row regardless of whether the in-memory
            # waiter was still live — a rehydrate scenario may have a row
            # from a previous boot with no in-memory waiter.
            if store is not None:
                try:
                    await store.mark_resolved(
                        request_id=rid,
                        resolved_at=now,
                        resolution="user_cancelled",
                    )
                except Exception:  # noqa: BLE001 — best-effort mark
                    pass

        return CancelPendingPermissionResponse(cancelled=cancelled)

