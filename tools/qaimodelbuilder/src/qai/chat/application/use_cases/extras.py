# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Image upload, prompt enhance, prompt snapshot use cases (PR-403 / S7.5 lane L4).

Three thin orchestrating use cases that wrap the corresponding ports.
Each one performs validation, calls the port, and returns a stable
shape — no domain side effects beyond what the port itself does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.chat.application.ports import (
    ImageUploadRequest,
    ImageUploadResult,
    ImageUploadStorePort,
    PromptEnhanceRequest,
    PromptEnhancerPort,
    PromptSnapshot,
    PromptSnapshotStorePort,
)
from qai.platform.errors import ValidationError
from qai.platform.logging import get_logger

_log = get_logger(__name__)


# Legacy guard: max input size for prompt enhance (`backend/main.py:7277`).
PROMPT_ENHANCE_MAX_CHARS: int = 8000


# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class UploadImageInput:
    conversation_id: str
    message_id: str
    base64_data: str
    mime_type: str


class UploadImageUseCase:
    """Validate inputs and persist a base64-encoded chat image."""

    def __init__(self, *, store: ImageUploadStorePort) -> None:
        self._store = store

    async def execute(self, input: UploadImageInput) -> ImageUploadResult:
        if not input.base64_data:
            raise ValidationError(
                "chat.image_upload_invalid",
                "base64_data must be non-empty",
            )
        if not input.mime_type:
            raise ValidationError(
                "chat.image_upload_invalid",
                "mime_type must be non-empty",
            )
        result = await self._store.save_base64(
            ImageUploadRequest(
                conversation_id=input.conversation_id,
                message_id=input.message_id,
                base64_data=input.base64_data,
                mime_type=input.mime_type,
            ),
        )
        _log.info(
            "chat.image_uploaded",
            conversation_id=input.conversation_id,
            message_id=input.message_id,
            mime_type=input.mime_type,
            url=result.url,
        )
        return result


# ---------------------------------------------------------------------------
# Prompt enhance
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class EnhancePromptInput:
    text: str
    model_id: str | None = None
    model_provider: str | None = None
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True, kw_only=True)
class EnhancePromptResult:
    text: str
    model_id: str
    model_provider: str


class EnhancePromptUseCase:
    """Rewrite a raw user prompt into a higher-quality version via LLM.

    Returns an :class:`EnhancePromptResult` on success.  Raises:

    * :class:`qai.platform.errors.ValidationError` (``empty_input`` /
      ``input_too_long``) on invalid inputs;
    * :class:`qai.platform.errors.ValidationError` (``empty_response``)
      when the upstream returned ``None`` (port contract);
    * never raises adapter-internal exceptions — those are absorbed by
      the port and surface as ``None``.
    """

    def __init__(self, *, enhancer: PromptEnhancerPort) -> None:
        self._enhancer = enhancer

    async def execute(self, input: EnhancePromptInput) -> EnhancePromptResult:
        raw = (input.text or "").strip()
        if not raw:
            raise ValidationError(
                "chat.prompt_enhance_empty",
                "input text must be non-empty",
            )
        if len(raw) > PROMPT_ENHANCE_MAX_CHARS:
            raise ValidationError(
                "chat.prompt_enhance_too_long",
                f"input text exceeds {PROMPT_ENHANCE_MAX_CHARS} character limit",
            )
        enhanced = await self._enhancer.enhance(
            PromptEnhanceRequest(
                text=raw,
                model_id=input.model_id,
                model_provider=input.model_provider,
                timeout_seconds=input.timeout_seconds,
            ),
        )
        if not enhanced or not enhanced.strip():
            raise ValidationError(
                "chat.prompt_enhance_empty_response",
                "upstream returned an empty enhancement",
            )
        return EnhancePromptResult(
            text=enhanced.strip(),
            model_id=input.model_id or "",
            model_provider=input.model_provider or "",
        )


# ---------------------------------------------------------------------------
# Prompt snapshot
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class GetPromptSnapshotInput:
    request_id: str


class GetPromptSnapshotUseCase:
    """Look up a previously captured prompt snapshot by request id."""

    def __init__(self, *, store: PromptSnapshotStorePort) -> None:
        self._store = store

    async def execute(
        self,
        input: GetPromptSnapshotInput,
    ) -> PromptSnapshot | None:
        if not input.request_id:
            return None
        return await self._store.get(input.request_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class SavePromptSnapshotInput:
    request_id: str
    payload: dict[str, Any]


class SavePromptSnapshotUseCase:
    """Persist a prompt snapshot (debug capture path).

    Used by the streaming use case (or instrumentation hook) to record
    the exact ``messages`` list sent upstream for later inspection.
    The store is FIFO-bounded; over-cap inserts evict the oldest.
    """

    def __init__(self, *, store: PromptSnapshotStorePort) -> None:
        self._store = store

    async def execute(self, input: SavePromptSnapshotInput) -> None:
        if not input.request_id:
            return
        await self._store.save(
            PromptSnapshot(
                request_id=input.request_id,
                payload=dict(input.payload),
            ),
        )


__all__ = [
    "PROMPT_ENHANCE_MAX_CHARS",
    "UploadImageUseCase",
    "UploadImageInput",
    "EnhancePromptUseCase",
    "EnhancePromptInput",
    "EnhancePromptResult",
    "GetPromptSnapshotUseCase",
    "GetPromptSnapshotInput",
    "SavePromptSnapshotUseCase",
    "SavePromptSnapshotInput",
]
