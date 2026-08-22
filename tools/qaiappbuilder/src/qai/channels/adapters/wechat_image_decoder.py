# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Inbound WeChat image decoder (S9 PR-093 §2.2 H-15).

Restores multi-modal image handling for inbound WeChat messages —
the legacy ``backend/channels/wechat/channel.py:2174-2229`` had three
helpers (``_detect_image_mime`` / ``_build_image_content`` /
``_download_image_base64``) that the new architecture omitted, so
WeChat users sending images saw their attachments silently dropped.
This adapter restores parity for §2.2 H-15.

The adapter is a **stateless toolbox**: every function is pure or
takes an injected ``download_fn`` callable (so the channels context
never imports a specific HTTP client — the dispatch bridge supplies
one bound to the wechatbot SDK or an httpx fallback).  No module-
level globals, no logging side-effects on the happy path.

Design notes
------------

* **MIME sniffing via magic bytes**, not the Content-Type header,
  because the legacy WeChat SDK delivers raw bytes only.  Falls back
  to ``image/jpeg`` when no signature matches — matches the legacy
  behaviour and is the most common inbound type.
* **Returns OpenAI-Vision shape** (``[{"type": "image_url",
  "image_url": {"url": "data:..."}}, {"type": "text", "text": ...}]``)
  by default; an opt-in ``style="anthropic"`` argument flips to the
  Anthropic block shape used by the Claude-Code path
  (``{"type": "image", "source": {"type": "base64",
  "media_type": ..., "data": ...}}``).
* **Failure is downgrade-not-error**: when the bytes can't be fetched
  the helper returns a text-only content list with a Chinese
  fallback prompt so the user still gets a reply (matches legacy
  ``"收到一张图片（下载失败，无法显示）"`` UX).
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = [
    "build_image_content",
    "detect_image_mime",
    "download_image_base64",
]


# ---------------------------------------------------------------------------
# Magic-byte MIME detection
# ---------------------------------------------------------------------------
def detect_image_mime(data: bytes) -> str:
    """Return the MIME type for ``data`` based on its magic bytes.

    Recognises JPEG / PNG / GIF / WebP; returns ``image/jpeg`` for
    anything else (matches the legacy fallback exactly).
    """
    if not data or len(data) < 8:
        return "image/jpeg"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Download → base64
# ---------------------------------------------------------------------------
async def download_image_base64(
    download_fn: Callable[[], Awaitable[bytes | None]],
) -> tuple[str, str] | None:
    """Run ``download_fn`` and return ``(b64_data, mime_type)`` or ``None``.

    Mirrors the legacy ``_download_image_base64`` shape used by the
    Claude Code path which expects raw base64 + MIME (the Anthropic
    SDK wraps both into the message itself).  The dispatch bridge
    supplies ``download_fn`` bound to the active wechatbot SDK
    media-download call, so the channels adapter stays free of any
    SDK import.
    """
    try:
        data = await download_fn()
    except Exception:  # noqa: BLE001 — defensive; downgrade-not-error
        return None
    if not data:
        return None
    mime = detect_image_mime(data)
    encoded = base64.b64encode(data).decode("ascii")
    return encoded, mime


# ---------------------------------------------------------------------------
# Multi-modal content assembly
# ---------------------------------------------------------------------------
def build_image_content(
    *,
    image_b64: str | None,
    mime_type: str | None,
    text: str = "",
    style: str = "openai",
) -> list[dict[str, Any]]:
    """Assemble a multi-modal content list for the LLM bridge.

    Args:
        image_b64: Base64-encoded image bytes from
            :func:`download_image_base64`.  ``None`` means "download
            failed" — we degrade to text-only with a fallback prompt.
        mime_type: Detected MIME type (e.g. ``"image/jpeg"``).
        text: Optional accompanying text; when empty we synthesise
            ``"请描述这张图片"`` so the model has something to do.
        style: ``"openai"`` (default) returns the OpenAI-Vision
            ``image_url`` block shape; ``"anthropic"`` returns the
            Anthropic Claude image block shape used by the
            ``qai.ai_coding`` Claude-Code path.

    Returns:
        A list of content blocks suitable for inclusion in a single
        LLM message's ``content`` field.
    """
    if not image_b64 or not mime_type:
        # Download failed — degrade to a friendly text-only payload.
        fallback = text or "收到一张图片（下载失败，无法显示）"
        return [{"type": "text", "text": fallback}]

    if style == "anthropic":
        block: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": image_b64,
            },
        }
        if text:
            return [block, {"type": "text", "text": text}]
        return [block, {"type": "text", "text": "请描述这张图片"}]

    # Default: OpenAI-Vision shape
    data_url = f"data:{mime_type};base64,{image_b64}"
    blocks: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    if text:
        blocks.append({"type": "text", "text": text})
    else:
        blocks.append({"type": "text", "text": "请描述这张图片"})
    return blocks
