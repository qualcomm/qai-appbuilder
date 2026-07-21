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

    Conflicts are detected by ``(subject, path, scope_kind, scope_key)`` — a
    single subject cannot hold two grants for the same path IN THE SAME SCOPE.
    Different scopes may coexist for the same path (e.g. a ``permanent`` grant
    and a ``session``-scoped grant for the current conversation), so a
    session grant never clobbers a pre-existing permanent one and vice versa.
    Use :class:`RevokePathGrantUseCase` first if a same-scope replacement
    is needed.
    """

    def __init__(
        self,
        *,
        grant_repository: PathGrantRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        ids: IdGenerator,
        tracking_recorder: AclTrackingRecorderPort | None = None,
    ) -> None:
        self._grants = grant_repository
        self._events = event_bus
        self._clock = clock
        self._ids = ids
        self._tracking = tracking_recorder

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
    ) -> PathGrant:
        existing = await self._grants.list_for_subject(subject)
        for grant in existing:
            # Conflict is per (path, scope) — a permanent and a session grant
            # for the same path are distinct entries (真 scoping), so only a
            # SAME-scope duplicate is a conflict.
            if (
                grant.path == path
                and grant.scope_kind == scope_kind
                and grant.scope_key == scope_key
            ):
                raise PathGrantConflictError(
                    f"subject {subject.identifier!r} already has a "
                    f"{scope_kind} grant for path {path!r}",
                    details={
                        "subject": subject.identifier,
                        "path": path,
                        "scope_kind": scope_kind,
                        "scope_key": scope_key,
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
            )
        )
        return grant
