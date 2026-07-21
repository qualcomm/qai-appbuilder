# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — Path grants + audit endpoints. (split from security.py).

Pure-move extraction (zero behaviour change): the route handlers are
byte-identical to the originals; they were nested closures inside
``build_router`` and are now nested inside this registrar instead,
still capturing the ``container`` passed in.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from fastapi import Query, Response, status

from qai.security.application.use_cases.create_path_grant import CreatePathGrantUseCase
from qai.security.application.use_cases.revoke_path_grant import RevokePathGrantUseCase
from qai.security.domain.value_objects import GrantSource, Subject

from ._dto import (
    AuditRecentResponse,
    CreateGrantRequest,
    GrantResponse,
    GrantsListResponse,
    _audit_to_dto,
    _grant_to_dto,
    _mask_from_dto,
    _parse_iso_or_validation_error,
    _subject_from_dto,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


def _register_grants_routes(router: "APIRouter", *, container: "Container") -> None:
    # ── path grants ────────────────────────────────────────────────────

    @router.get("/path-grants", response_model=GrantsListResponse)
    async def list_grants(
        subject_kind: Literal["user", "preset", "system"] = Query(
            ...,
            description="Subject kind to scope the listing",
        ),
        subject_identifier: str = Query(
            ...,
            min_length=1,
            max_length=512,
            description="Subject identifier",
        ),
    ) -> GrantsListResponse:
        repo = container.security.path_grant_repository
        subj = Subject(kind=subject_kind, identifier=subject_identifier)
        grants = await repo.list_for_subject(subj)
        return GrantsListResponse(
            grants=[_grant_to_dto(g) for g in grants],
        )

    @router.post(
        "/path-grants",
        response_model=GrantResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_grant(body: CreateGrantRequest) -> GrantResponse:
        use_case: CreatePathGrantUseCase = (
            container.security.create_path_grant_use_case
        )
        expires_at: datetime | None = None
        if body.expires_at is not None:
            expires_at = _parse_iso_or_validation_error(
                body.expires_at, field_name="expires_at"
            )
        grant = await use_case.execute(
            subject=_subject_from_dto(body.subject),
            path=body.path,
            mask=_mask_from_dto(body.mask),
            source=GrantSource(body.source),
            expires_at=expires_at,
        )
        return GrantResponse(grant=_grant_to_dto(grant))

    @router.delete(
        "/path-grants/{grant_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def revoke_grant(grant_id: str) -> Response:
        use_case: RevokePathGrantUseCase = (
            container.security.revoke_path_grant_use_case
        )
        await use_case.execute(grant_id=grant_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── audit ──────────────────────────────────────────────────────────

    @router.get("/audit/recent", response_model=AuditRecentResponse)
    async def list_recent_audit(
        limit: int = Query(
            50,
            ge=1,
            le=500,
            description="Maximum number of audit entries to return",
        ),
    ) -> AuditRecentResponse:
        # PR-040 (issue a, decision A): the read path now lives on the
        # dedicated :class:`AuditQueryPort` (``container.security.audit_query``)
        # so the writer (``audit_sink``) can stay append-only by contract.
        query_port = container.security.audit_query
        entries = await query_port.recent(limit=limit)
        return AuditRecentResponse(
            entries=[_audit_to_dto(e) for e in entries],
        )
