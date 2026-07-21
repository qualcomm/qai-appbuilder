# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Revoke a persistent ACL entry."""

from __future__ import annotations

from qai.platform.errors import NotFoundError
from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.security.domain.events import PathGrantRevokedEvent
from qai.security.domain.value_objects import Subject

from ..ports import AclTrackingRecorderPort, PathGrantRepositoryPort

__all__ = ["RevokePathGrantUseCase"]


class RevokePathGrantUseCase:
    """Remove a :class:`PathGrant` and publish the revocation event."""

    def __init__(
        self,
        *,
        grant_repository: PathGrantRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        tracking_recorder: AclTrackingRecorderPort | None = None,
    ) -> None:
        self._grants = grant_repository
        self._events = event_bus
        self._clock = clock
        self._tracking = tracking_recorder

    async def execute(
        self,
        *,
        grant_id: str,
        revoked_by: Subject | None = None,
    ) -> None:
        existing = await self._grants.get(grant_id)
        if existing is None:
            raise NotFoundError(
                "security.path_grant.not_found",
                "path_grant",
                grant_id,
            )
        revoked_at = self._clock.now()
        # U-013 / 6-H2: record the ``revoke`` lifecycle event *before* the
        # delete. The ``security_acl_tracking`` FK is ``ON DELETE
        # CASCADE``, so the grant deletion clears its tracking rows — the
        # V2 equivalent of V1 ``_remove_tracking_entry`` dropping the
        # path's line on un-apply (``backend/security/persistent_acl.py``).
        if self._tracking is not None:
            await self._tracking.record(
                grant_id=grant_id,
                event_type="revoke",
                occurred_at=revoked_at,
                note=existing.path,
            )
        await self._grants.delete(grant_id)
        await self._events.publish(
            PathGrantRevokedEvent(
                grant_id=grant_id,
                subject=existing.subject,
                path=existing.path,
                revoked_by=revoked_by,
                occurred_at=revoked_at,
            )
        )
