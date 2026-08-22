# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Inbound Feishu image decoder (PR-097 §2 R-7).

Restores multi-modal image handling for inbound Feishu messages —
the legacy ``backend/channels/feishu/channel.py:1444-1493`` had three
helpers (``_download_feishu_image`` / ``_build_image_content`` /
``_download_image_base64``) that the new architecture omitted, so
Feishu users sending images saw their attachments silently dropped.
This adapter restores parity for §2 R-7.

Wire format (Feishu open platform, 2024)
----------------------------------------
::

    GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}
        ?type=image
    Authorization: Bearer <tenant_access_token>

    Success: 200 OK with raw image bytes (Content-Type:
             ``image/jpeg`` / ``image/png`` / ...).
    Failure: 4xx/5xx with JSON error envelope.

Design notes
------------
* The adapter pulls the **tenant_access_token** via
  :class:`FeishuTenantTokenCache` so the caller can share one cache
  across the whole instance lifecycle.
* MIME sniffing reuses
  :func:`qai.channels.adapters.wechat_image_decoder.detect_image_mime`
  (same magic-byte logic, no need to fork).
* Failure is downgrade-not-error (matches legacy
  ``"收到一张图片（下载失败，无法显示）"`` UX): on any exception
  :func:`download_image_base64` returns ``None`` and
  :func:`build_image_content` returns a text-only block.
* Two output shapes, mirroring ``wechat_image_decoder``:
  ``style="openai_vision"`` (default) for the chat path,
  ``style="anthropic"`` for the Claude-Code path used by
  :mod:`qai.ai_coding`.
"""

from __future__ import annotations

import base64
from typing import Any, TYPE_CHECKING

from qai.channels.adapters.wechat_image_decoder import detect_image_mime
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.channels.adapters.feishu_tenant_token_cache import (
        FeishuTenantTokenCache,
    )

__all__ = [
    "build_image_content",
    "detect_image_mime",
    "download_image_base64",
]

logger = get_logger(__name__)

_FEISHU_IMAGE_HOST = "https://open.feishu.cn"
_HTTP_TIMEOUT_SECONDS: float = 15.0


def _resource_url(message_id: str, image_key: str) -> str:
    return (
        f"{_FEISHU_IMAGE_HOST}/open-apis/im/v1/messages/"
        f"{message_id}/resources/{image_key}?type=image"
    )


async def download_image_base64(
    *,
    message_id: str,
    image_key: str,
    tenant_token_cache: "FeishuTenantTokenCache",
    http_client_factory: Any,
) -> tuple[str, str] | None:
    """Download a Feishu inbound image and return ``(b64, mime_type)``.

    Returns ``None`` on any failure (network error, non-200 response,
    empty body).  Errors are logged at WARNING level — the caller is
    expected to degrade gracefully (show text-only content instead).

    Args:
        message_id: Feishu ``event.message.message_id`` (``om_...``).
        image_key: Feishu ``image_key`` extracted from the message
            content (per Feishu webhook docs, an opaque string).
        tenant_token_cache: Shared token cache; supplies the bearer.
        http_client_factory: Factory matching the channels-context
            convention ``factory(*, timeout: float) -> AsyncClient``.
    """
    try:
        token = await tenant_token_cache.get_token()
    except Exception as exc:  # noqa: BLE001 — degrade-not-error
        logger.warning(
            "channels.feishu.image_download_token_failed",
            message_id=message_id,
            error=str(exc),
        )
        return None

    headers = {"Authorization": f"Bearer {token}"}
    url = _resource_url(message_id, image_key)
    try:
        async with http_client_factory(
            timeout=_HTTP_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — degrade-not-error
        logger.warning(
            "channels.feishu.image_download_http_error",
            message_id=message_id,
            image_key=image_key,
            error=str(exc),
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "channels.feishu.image_download_http_status",
            message_id=message_id,
            image_key=image_key,
            status=response.status_code,
        )
        return None

    data = getattr(response, "content", b"") or b""
    if not data:
        logger.warning(
            "channels.feishu.image_download_empty_body",
            message_id=message_id,
            image_key=image_key,
        )
        return None

    mime = detect_image_mime(data)
    encoded = base64.b64encode(data).decode("ascii")
    return encoded, mime


async def build_image_content(
    *,
    message_id: str,
    image_key: str,
    caption: str = "",
    style: str = "openai_vision",
    tenant_token_cache: "FeishuTenantTokenCache",
    http_client_factory: Any,
) -> list[dict[str, Any]]:
    """Assemble a multi-modal content list for the LLM bridge.

    Args:
        message_id: Feishu ``event.message.message_id``.
        image_key: Feishu ``image_key`` from the message content.
        caption: Optional accompanying text; when empty we synthesise
            ``"请描述这张图片"`` so the model has something to do
            (matches the legacy behaviour exactly).
        style: ``"openai_vision"`` (default) returns the OpenAI-Vision
            ``image_url`` block shape; ``"anthropic"`` returns the
            Anthropic Claude image block shape used by the
            :mod:`qai.ai_coding` Claude-Code path.
        tenant_token_cache: Shared token cache.
        http_client_factory: AsyncClient factory.

    Returns:
        A list of content blocks suitable for inclusion in a single
        LLM message's ``content`` field.  On download failure the
        list contains a single text-only fallback block so the user
        still gets a reply.
    """
    decoded = await download_image_base64(
        message_id=message_id,
        image_key=image_key,
        tenant_token_cache=tenant_token_cache,
        http_client_factory=http_client_factory,
    )
    if decoded is None:
        # Download failed — degrade to a friendly text-only payload
        # (matches legacy ``"收到一张图片（下载失败，无法显示）"``).
        fallback = caption or "收到一张图片（下载失败，无法显示）"
        return [{"type": "text", "text": fallback}]

    image_b64, mime_type = decoded

    if style == "anthropic":
        block: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": image_b64,
            },
        }
        text_block = {
            "type": "text",
            "text": caption or "请描述这张图片",
        }
        return [block, text_block]

    # Default: OpenAI-Vision shape (matches legacy
    # ``_build_image_content`` exactly).
    data_url = f"data:{mime_type};base64,{image_b64}"
    blocks: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    blocks.append(
        {"type": "text", "text": caption or "请描述这张图片"}
    )
    return blocks
