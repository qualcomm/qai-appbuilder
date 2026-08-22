# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Reject a pending permission request."""

from __future__ import annotations

import logging
from datetime import timedelta

from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.security.application.permission_wait import PermissionWaitRegistry
from qai.security.domain.entities import PermissionRequest
from qai.security.domain.errors import PermissionRequestNotFoundError
from qai.security.domain.events import PermissionRejectedEvent
from qai.security.domain.value_objects import GrantSource, RequestId, Subject

from ..ports import PermissionRequestRepositoryPort
from .approve_permission import (
    _PERSISTING_SCOPES,
    _SCOPE_TTL_SECONDS,
    _dir_grant_path,
    _exec_binary_token,
)

__all__ = ["RejectPermissionUseCase"]

_log = logging.getLogger(__name__)


class RejectPermissionUseCase:
    """Transition a PermissionRequest from PENDING to REJECTED.

    P0 ASK restore — when wired with a :class:`PermissionWaitRegistry` the
    rejection also wakes the FileGuard ASK waiter blocked on this
    ``request_id`` with a DENY resolution (V1 ``pending.event.set()`` with
    ``Decision.DENY``). The registry is optional so existing callers keep
    working.

    Constraint G (reject side) — when wired with a ``create_grant``
    collaborator and the user chose a persisting scope
    (``session`` / ``process`` / ``permanent``), the rejection ALSO persists a
    ``effect="deny"`` :class:`PathGrant`. This mirrors approve's grant
    persistence so a remembered rejection re-denies the same
    (subject, path, mask) WITHOUT re-prompting on the next identical access.
    ``once`` persists nothing (this-call-only deny). Both collaborators are
    optional so existing callers / tests keep working.
    """

    def __init__(
        self,
        *,
        request_repository: PermissionRequestRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        wait_registry: PermissionWaitRegistry | None = None,
        create_grant: object | None = None,
        audit_sink: object | None = None,
        ttl_resolver: object | None = None,
    ) -> None:
        self._requests = request_repository
        self._events = event_bus
        self._clock = clock
        self._wait_registry = wait_registry
        self._create_grant = create_grant
        # P-17 (2026-07-09): write the definitive DENY audit row here.
        self._audit_sink = audit_sink
        # L-Sec-3 (2026-07-28): optional callable returning the same shape as
        # :data:`_SCOPE_TTL_SECONDS`.  ``None`` (existing callers) keeps
        # historical behaviour byte-for-byte.
        self._ttl_resolver = ttl_resolver

    async def execute(
        self,
        *,
        request_id: RequestId,
        decided_by: Subject | None = None,
        reason: str = "",
        scope: str = "once",
        scope_conversation_id: str = "",
        scope_boot_id: str = "",
        grant_range: str = "file",
    ) -> PermissionRequest:
        existing = await self._requests.get(request_id)
        if existing is None:
            raise PermissionRequestNotFoundError(request_id.value)
        now = self._clock.now()
        rejected = existing.reject(now=now, reason=reason)
        await self._requests.save(rejected)

        scope_n = (scope or "once").strip().lower()
        # Constraint G (reject side): persist a deny-effect grant for
        # session/process/permanent so the next identical access is re-denied
        # without re-prompting. Best-effort — a grant write failure must not
        # block the rejection / wake (mirrors approve's grant persistence).
        if scope_n in _PERSISTING_SCOPES and self._create_grant is not None:
            await self._persist_deny_grant(
                existing=existing,
                now=now,
                scope=scope_n,
                scope_conversation_id=scope_conversation_id,
                scope_boot_id=scope_boot_id,
                grant_range=(grant_range or "file").strip().lower(),
            )

        await self._events.publish(
            PermissionRejectedEvent(
                request_id=request_id,
                subject=existing.subject,
                resource=existing.resource,
                decided_by=decided_by,
                reason=reason,
                occurred_at=now,
            )
        )
        if self._wait_registry is not None:
            self._wait_registry.resolve(
                request_id.value, allow=False, scope="deny"
            )

        # P-17 (2026-07-09): write the definitive DENY audit row now that the
        # user has explicitly rejected the request. Best-effort.
        if self._audit_sink is not None:
            try:
                from qai.security.domain.entities import AuditEntry
                from qai.security.domain.value_objects import PolicyAction

                audit_id = f"reject-{request_id.value[:16]}"
                await self._audit_sink.append(
                    AuditEntry(
                        audit_id=audit_id,
                        occurred_at=now,
                        subject=existing.subject,
                        resource=existing.resource,
                        decision=PolicyAction.DENY,
                        rule_id=None,
                        correlation_id=None,
                        note="user_rejected",
                        channel=None,
                        op=existing.resource.kind,
                        process_path="",
                        command_line="",
                        actor_pid=None,
                        actor_parent_pid=None,
                    )
                )
            except Exception:  # noqa: BLE001 — audit failure must not block
                _log.warning(
                    "reject_permission: failed to write deny audit row "
                    "for request_id=%s — check AuditEntry construction / "
                    "audit_sink availability",
                    request_id.value,
                    exc_info=True,
                )

        return rejected

    def _resolve_ttl(self, scope: str) -> int | None:
        """Return TTL seconds for ``scope`` — resolver override → default.

        Mirror of :meth:`ApprovePermissionUseCase._resolve_ttl` so a single
        settings override steers both terminal paths (approve + reject)
        identically.  L-Sec-3 (2026-07-28).
        """
        if self._ttl_resolver is None:
            return _SCOPE_TTL_SECONDS.get(scope)
        try:
            table = self._ttl_resolver()  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — provider fault → default
            return _SCOPE_TTL_SECONDS.get(scope)
        if not isinstance(table, dict) or scope not in table:
            return _SCOPE_TTL_SECONDS.get(scope)
        value = table[scope]
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return _SCOPE_TTL_SECONDS.get(scope)

    async def _persist_deny_grant(
        self,
        *,
        existing: PermissionRequest,
        now: object,
        scope: str,
        scope_conversation_id: str = "",
        scope_boot_id: str = "",
        grant_range: str = "file",
    ) -> None:
        """Create an ``effect="deny"`` PathGrant covering the rejected access.

        Constraint G (reject side) mirror of
        :meth:`ApprovePermissionUseCase._persist_grant`: same scope→scope_key
        mapping (session → conversation id, process → boot id, permanent → no
        key), same :data:`_SCOPE_TTL_SECONDS` TTL safety net, and the same
        directory / program range widening — so a "reject + whole directory"
        or "reject + whole program" is remembered exactly as broadly as the
        approve side would remember an allow. The ONLY difference is
        ``effect="deny"``: the grant cascade in ``check_permission`` consults
        deny-effect grants FIRST and returns DENY on a match, so the next
        identical access is re-denied without a prompt.

        Best-effort: a conflict (a SAME-scope, SAME-effect deny grant for this
        subject+path already exists) or any write error is swallowed — the
        path is already denied in that case, and the wake/DENY must still
        proceed.
        """
        scope_key = ""
        if scope == "session":
            scope_key = scope_conversation_id or ""
        elif scope == "process":
            scope_key = scope_boot_id or ""
        ttl_seconds = self._resolve_ttl(scope)
        expires_at = None
        if ttl_seconds is not None:
            expires_at = now + timedelta(seconds=ttl_seconds)  # type: ignore[operator]
        grant_path = existing.resource.identifier
        is_directory = False
        is_program = False
        if grant_range == "directory" and existing.resource.kind != "exec":
            dir_path = _dir_grant_path(grant_path)
            if dir_path is not None:
                grant_path = dir_path
                is_directory = True
        elif grant_range == "program" and existing.resource.kind == "exec":
            token = _exec_binary_token(grant_path)
            if token:
                grant_path = token
                is_program = True
        try:
            await self._create_grant.execute(  # type: ignore[attr-defined]
                subject=existing.subject,
                path=grant_path,
                mask=existing.requested_mask,
                source=GrantSource.USER,
                expires_at=expires_at,
                scope_kind=scope,
                scope_key=scope_key,
                is_directory=is_directory,
                is_program=is_program,
                effect="deny",
            )
        except Exception as exc:  # noqa: BLE001 — never block the rejection
            _log.debug(
                "security.reject_permission.deny_grant_persist_skipped "
                "request_id=%s error=%s",
                existing.request_id.value,
                str(exc),
            )
