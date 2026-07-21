# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared image-reference decoding for chat turns (single source of truth).

Both the SSE/WS route layer (a user prompt that embeds uploaded images as
``![name](/api/images/files/xxx)`` markdown) and the agentic follow-up loop
(a ``question`` tool answer that embeds the SAME markdown) need to turn those
``/api/images/files/...`` URLs into OpenAI-Vision content blocks so a
multimodal model can actually *see* the pixels.

Historically only the route layer did this (``interfaces/http/routes/chat/
_sse.py`` ``_extract_image_refs`` + ``_resolve_image_refs_to_vision_blocks``).
That decode logic is lifted here, into the chat **application** layer, so:

* the agentic loop (``streaming.py``) can reuse it WITHOUT importing the
  ``interfaces`` layer (which would break ``interfaces-stays-thin`` /
  layering), and
* the route layer can collapse onto the SAME helper (one decode口径 for both
  the user-prompt path and the question-answer path).

Layering / side-effects: the only side-effect is *reading* image bytes off
disk, which is an infrastructure concern reached through the injected
``ImageUploadStorePort`` (its ``get_path`` reverses URL → on-disk path). The
helper itself imports only ``base64`` / ``re`` and the chat application port,
so it stays free of any cross-context or ``interfaces`` import.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "IMAGE_REF_PATTERN",
    "ImagePathResolver",
    "extract_image_refs",
    "resolve_image_refs_to_vision_blocks",
]

# Markdown image syntax pointing at the chat image static mount. V1 parity
# (useChat.js:2067-2077): the WebUI prepends uploaded images as
# ``![name](/api/images/files/xxx)``. This is the SAME pattern the route
# layer used (``_sse.py:_IMAGE_REF_PATTERN``).
IMAGE_REF_PATTERN = re.compile(r"!\[[^\]]*\]\((/api/images/files/[^)]+)\)")

_URL_PREFIX = "/api/images/files"

_MIME_MAP = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


@runtime_checkable
class ImagePathResolver(Protocol):
    """Narrow read interface: reverse an image URL to its on-disk path.

    Satisfied by ``ImageUploadStorePort`` adapters
    (``FileSystemImageUploadStore.get_path``); declared here as a structural
    Protocol so this helper depends only on the one method it needs and never
    imports a concrete adapter.
    """

    def get_path(self, url: str) -> Any:  # -> pathlib.Path | None
        """Return the on-disk path for ``url`` or ``None`` if absent."""
        ...


def extract_image_refs(text: str) -> tuple[str, ...]:
    """Extract ``/api/images/files/...`` URLs from markdown image syntax.

    Returns the ordered tuple of matched URLs (possibly empty). Pure — no
    side-effects.
    """
    if not text:
        return ()
    refs = IMAGE_REF_PATTERN.findall(text)
    return tuple(refs) if refs else ()


def resolve_image_refs_to_vision_blocks(
    *,
    store: ImagePathResolver | None,
    image_refs: tuple[str, ...],
    source_text: str,
    placeholder_text: str = "请描述这张图片",
) -> list[dict[str, Any]]:
    """Resolve image URLs to an OpenAI-Vision content-block list.

    Reads each referenced image off disk via ``store.get_path`` and encodes it
    as a ``data:`` URL ``{"type":"image_url",...}`` block. The markdown image
    references are stripped from ``source_text`` to form a leading text block;
    when nothing remains, ``placeholder_text`` is used so the model still gets
    a textual cue alongside the image(s).

    Returns ``[]`` when ``store`` is ``None``, no refs resolve to an existing
    file, or any error occurs (best-effort: this never raises so a streaming
    turn is never broken by a missing/corrupt upload).
    """
    if store is None or not image_refs:
        return []

    blocks: list[dict[str, Any]] = []
    for ref in image_refs:
        if not ref.startswith(_URL_PREFIX + "/"):
            continue
        try:
            disk_path = None
            if hasattr(store, "get_path"):
                disk_path = store.get_path(ref)
            if disk_path is None or not disk_path.exists():
                continue
            ext = disk_path.suffix.lstrip(".").lower()
            mime = _MIME_MAP.get(ext, "image/jpeg")
            raw_bytes = disk_path.read_bytes()
            b64_str = base64.b64encode(raw_bytes).decode("ascii")
            data_url = f"data:{mime};base64,{b64_str}"
            blocks.append(
                {"type": "image_url", "image_url": {"url": data_url}}
            )
        except Exception:  # noqa: BLE001 — best-effort decode
            continue

    if not blocks:
        return []

    clean_text = re.sub(
        r"!\[[^\]]*\]\([^)]*\)\n?", "", source_text or ""
    ).strip()

    content_blocks: list[dict[str, Any]] = []
    content_blocks.append(
        {"type": "text", "text": clean_text if clean_text else placeholder_text}
    )
    content_blocks.extend(blocks)
    return content_blocks
