# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — share routes (``/share`` family, PR-045 / issue d).

``POST /share`` mints a share token for a run; ``GET /share/{token}``
returns the combined share + run view. Handler bodies are byte-for-byte
identical to the pre-split module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, status

from ._dto import (
    ShareCreateRequest,
    ShareResponse,
    ShareViewResponse,
    _run_to_dto,
    _validate_run_id,
)

from qai.app_builder.application.use_cases.share import (
    CreateShareUseCase,
    GetShareByTokenUseCase,
)
from qai.app_builder.domain.share import ShareToken
from qai.platform.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def register(router: APIRouter, *, container: "Container") -> None:
    """Mount the share routes onto ``router``."""

    def _services() -> Any:
        return container.app_builder

    # ---- share (PR-045 / issue d) ---------------------------------------

    @router.post(
        "/share",
        response_model=ShareResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_share(body: ShareCreateRequest) -> ShareResponse:
        rid = _validate_run_id(body.run_id)
        uc: CreateShareUseCase = _services().create_share_use_case
        share = await uc.execute(
            run_id=rid, ttl_seconds=body.ttl_seconds
        )
        return ShareResponse(
            token=str(share.token),
            run_id=str(share.run_id),
            created_at=share.created_at.isoformat(),
            expires_at=(
                share.expires_at.isoformat()
                if share.expires_at is not None
                else None
            ),
            revoked=share.revoked,
        )

    @router.get("/share/{token}", response_model=ShareViewResponse)
    async def get_share_by_token(token: str) -> ShareViewResponse:
        try:
            tk = ShareToken(value=token)
        except ValueError as exc:
            raise ValidationError(
                "app_builder.share_token_invalid",
                str(exc),
                field_errors={"token": [str(exc)]},
            ) from exc
        uc: GetShareByTokenUseCase = _services().get_share_by_token_use_case
        share, run = await uc.execute(token=tk)
        return ShareViewResponse(
            share=ShareResponse(
                token=str(share.token),
                run_id=str(share.run_id),
                created_at=share.created_at.isoformat(),
                expires_at=(
                    share.expires_at.isoformat()
                    if share.expires_at is not None
                    else None
                ),
                revoked=share.revoked,
            ),
            run=_run_to_dto(run),
        )
