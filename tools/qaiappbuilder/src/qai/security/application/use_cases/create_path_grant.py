# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Create a persistent ACL entry (``PathGrant``)."""

from __future__ import annotations

from datetime import datetime

from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

from qai.security.domain.entities import PathGrant
from qai.security.domain.errors import PathGrantConflictError
from qai.security.domain.events import PathGrantCreatedEvent
from qai.security.domain.value_objects import AceMask, GrantSource, Subject

from ..ports import AclTrackingRecorderPort, PathGrantRepositoryPort

__all__ = ["CreatePathGrantUseCase"]


class CreatePathGrantUseCase:
    """Persist a new :class:`PathGrant` and publish the event.

    Conflicts are detected by ``(subject, path, scope_kind, scope_key,
    effect)`` — a single subject cannot hold two grants for the same path IN
    THE SAME SCOPE WITH THE SAME EFFECT. Different scopes may coexist for the
    same path (e.g. a ``permanent`` grant and a ``session``-scoped grant for
    the current conversation), so a session grant never clobbers a pre-existing
    permanent one and vice versa. ``effect`` is part of the key so an
    ALLOW-grant and a DENY-grant may coexist for the same (path, scope): the
    decision cascade consults deny-effect grants FIRST, so a remembered
    rejection wins over a remembered approval without needing to first revoke
    the stale allow row. Use :class:`RevokePathGrantUseCase` first if a
    same-scope, same-effect replacement is needed.
    """

    def __init__(
        self,
        *,
        grant_repository: PathGrantRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        ids: IdGenerator,
        tracking_recorder: AclTrackingRecorderPort | None = None,
        audit_sink: object | None = None,
    ) -> None:
        self._grants = grant_repository
        self._events = event_bus
        self._clock = clock
        self._ids = ids
        self._tracking = tracking_recorder
        # M-Sec-4 (2026-07-28): optional AuditSinkPort so a grant creation
        # produces a security-audit fact alongside the U-013 tracking row.
        # ``AclTrackingRecorderPort`` records the LIFECYCLE (add/remove) of
        # a grant row; the audit sink records the SECURITY DECISION ("we
        # granted subject S access to resource R"). Both survive after this
        # change so no downstream consumer is silenced.
        self._audit_sink = audit_sink

    async def execute(
        self,
        *,
        subject: Subject,
        path: str,
        mask: AceMask,
        source: GrantSource,
        expires_at: datetime | None = None,
        scope_kind: str = "permanent",
        scope_key: str = "",
        is_directory: bool = False,
        is_program: bool = False,
        effect: str = "allow",
    ) -> PathGrant:
        existing = await self._grants.list_for_subject(subject)
        for grant in existing:
            # Conflict is per (path, scope, effect) — a permanent and a session
            # grant for the same path are distinct entries (真 scoping), and an
            # allow-effect vs deny-effect grant for the same (path, scope) are
            # ALSO distinct (deny-first precedence in the cascade resolves which
            # applies), so only a SAME-scope, SAME-effect duplicate is a
            # conflict.
            if (
                grant.path == path
                and grant.scope_kind == scope_kind
                and grant.scope_key == scope_key
                and grant.effect == effect
            ):
                raise PathGrantConflictError(
                    f"subject {subject.identifier!r} already has a "
                    f"{scope_kind} {effect} grant for path {path!r}",
                    details={
                        "subject": subject.identifier,
                        "path": path,
                        "scope_kind": scope_kind,
                        "scope_key": scope_key,
                        "effect": effect,
                        "existing_grant_id": grant.grant_id,
                    },
                )
        now = self._clock.now()
        grant = PathGrant(
            grant_id=self._ids.new_id(),
            subject=subject,
            path=path,
            mask=mask,
            source=source,
            created_at=now,
            expires_at=expires_at,
            scope_kind=scope_kind,
            scope_key=scope_key,
            is_directory=is_directory,
            is_program=is_program,
            effect=effect,
        )
        await self._grants.save(grant)
        # U-013 / 6-H2: append-only lifecycle log (replaces V1
        # ``persistent_acl_tracking.txt`` ``_add_tracking_entry``). The V1
        # ``PR``/``PF``/``PM``/``MF`` prefix encoded access-type; we keep
        # that context in the note (path + access kind) — the rest is on
        # the referenced grant row.
        if self._tracking is not None:
            access_kind = (
                "modify"
                if (mask.write or mask.delete)
                else "read_exec"
            )
            await self._tracking.record(
                grant_id=grant.grant_id,
                event_type="add",
                occurred_at=now,
                note=f"{access_kind} {path}",
            )
        await self._events.publish(
            PathGrantCreatedEvent(
                grant_id=grant.grant_id,
                subject=subject,
                path=path,
                mask=mask,
                source=source,
                occurred_at=now,
                effect=effect,
            )
        )
        # M-Sec-4: definitive grant-created audit row.  Fired AFTER the
        # persist / tracking / event side-effects so a partial failure in
        # the audit sink never rolls back an already-persisted grant.
        if self._audit_sink is not None:
            try:
                from qai.security.domain.entities import AuditEntry
                from qai.security.domain.value_objects import (
                    PolicyAction,
                    Resource,
                )

                await self._audit_sink.append(  # type: ignore[attr-defined]
                    AuditEntry(
                        audit_id=f"grant-{grant.grant_id[:16]}",
                        occurred_at=now,
                        subject=subject,
                        resource=Resource(kind="path", identifier=path),
                        decision=(
                            PolicyAction.ALLOW
                            if effect == "allow"
                            else PolicyAction.DENY
                        ),
                        rule_id=None,
                        correlation_id=None,
                        note="grant_created",
                        channel=None,
                        op="",
                        process_path="",
                        command_line="",
                        actor_pid=None,
                        actor_parent_pid=None,
                    )
                )
            except Exception:  # noqa: BLE001 — audit failure must not block
                import logging

                logging.getLogger(__name__).warning(
                    "create_path_grant: failed to write grant audit row "
                    "for grant_id=%s",
                    grant.grant_id,
                    exc_info=True,
                )
        return grant
