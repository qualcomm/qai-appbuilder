# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Image upload + prompt enhance + prompt snapshot routes (PR-403 / S7.5 lane L4).

Three endpoints migrated from the legacy ``backend/main.py``:

* ``POST /api/images/upload``       — chat image upload (BE-132)
* ``POST /api/prompt/enhance``      — LLM-driven prompt rewrite (BE-135)
* ``GET  /api/prompt-snapshot/{request_id}`` — debug capture lookup (BE-134)

These three paths sit OUTSIDE the ``/api/chat`` prefix because they
predate the chat-namespaced surface and have third-party consumers
(image upload is referenced by App Builder runners through the URL,
prompt-snapshot is referenced by support tooling).  Per v2.7 §3.1
route lock, the legacy paths must be preserved 1:1.

Mounted by ``interfaces/http/routes/chat/__init__.py:build_router`` as
a sibling sub-router so all chat-domain HTTP entry points stay co-
located with the ``ChatServices`` DI surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from qai.chat.application.use_cases.extras import (
    EnhancePromptInput,
    GetPromptSnapshotInput,
    UploadImageInput,
)
from qai.platform.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
class ImageUploadRequestBody(BaseModel):
    """``POST /api/images/upload`` body — legacy parity (verbatim field names)."""

    conv_id: str = Field(..., min_length=1, max_length=128)
    msg_id: str = Field(..., min_length=1, max_length=128)
    b64_data: str = Field(..., min_length=1)
    mime_type: str = Field(..., min_length=1, max_length=64)


class ImageUploadResponse(BaseModel):
    """``POST /api/images/upload`` response — legacy parity."""

    url: str
    path: str | None


class PromptEnhanceRequestBody(BaseModel):
    """``POST /api/prompt/enhance`` body — legacy parity."""

    text: str = Field(..., min_length=1)
    model_id: str | None = None
    model_provider: str | None = None


class PromptEnhanceResponse(BaseModel):
    """``POST /api/prompt/enhance`` response — legacy parity."""

    text: str
    model_id: str
    model_provider: str


class PromptSnapshotResponse(BaseModel):
    """``GET /api/prompt-snapshot/{request_id}`` response — legacy parity.

    V1 (``backend/main.py:6870``) returns ``{"request_id": id, **snapshot}``
    — i.e. the captured payload fields (``model_id`` / ``tool_mode`` /
    ``messages`` / ``timestamp`` …) are **flattened to the top level** next
    to ``request_id``, NOT nested under a ``snapshot`` key.  The V1 front-end
    (and V2 ``ChatMessageList.vue`` prompt-snapshot dialog) reads
    ``data.messages`` / ``data.model_id`` / ``data.timestamp`` from the top
    level.  We mirror that flattening so the dialog shows the captured prompt.

    ``model_config = extra="allow"`` lets the free-form captured payload
    fields pass through verbatim alongside the echoed ``request_id``.
    """

    model_config = ConfigDict(extra="allow")

    request_id: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def build_router(*, container: "Container") -> APIRouter:
    """Build the (unprefixed) router for the three legacy endpoints."""
    router = APIRouter(tags=["chat"])

    @router.post(
        "/api/images/upload",
        response_model=ImageUploadResponse,
        status_code=status.HTTP_200_OK,
    )
    async def upload_image(req: ImageUploadRequestBody) -> ImageUploadResponse:
        try:
            result = await container.chat.upload_image_use_case.execute(
                UploadImageInput(
                    conversation_id=req.conv_id,
                    message_id=req.msg_id,
                    base64_data=req.b64_data,
                    mime_type=req.mime_type,
                ),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ImageUploadResponse(url=result.url, path=result.disk_path)

    @router.post(
        "/api/prompt/enhance",
        response_model=PromptEnhanceResponse,
        status_code=status.HTTP_200_OK,
    )
    async def enhance_prompt(
        req: PromptEnhanceRequestBody,
    ) -> PromptEnhanceResponse:
        try:
            result = await container.chat.enhance_prompt_use_case.execute(
                EnhancePromptInput(
                    text=req.text,
                    model_id=req.model_id,
                    model_provider=req.model_provider,
                ),
            )
        except ValidationError as exc:
            # Distinct status codes match the legacy behaviour:
            # * empty / too-long input → 400
            # * empty upstream response → 502 (downstream provider issue)
            code = getattr(exc, "code", "") or ""
            if code == "chat.prompt_enhance_empty_response":
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return PromptEnhanceResponse(
            text=result.text,
            model_id=result.model_id,
            model_provider=result.model_provider,
        )

    @router.get(
        "/api/prompt-snapshot/{request_id}",
        response_model=PromptSnapshotResponse,
        status_code=status.HTTP_200_OK,
    )
    async def get_prompt_snapshot(request_id: str) -> PromptSnapshotResponse:
        snapshot = await container.chat.get_prompt_snapshot_use_case.execute(
            GetPromptSnapshotInput(request_id=request_id),
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        # V1 parity (backend/main.py:6870): flatten the captured payload to
        # the top level next to ``request_id`` — ``{"request_id": id, **payload}``
        # — so the front-end dialog can read ``data.messages`` / ``data.model_id``
        # / ``data.timestamp`` directly (it does NOT look under a ``snapshot`` key).
        return PromptSnapshotResponse(
            request_id=snapshot.request_id,
            **dict(snapshot.payload),
        )

    return router


__all__ = ["build_router"]
