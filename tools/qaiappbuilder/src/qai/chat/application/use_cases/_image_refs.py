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

# ── Cloud-copy image encoding ─────────────────────────────────────────────────
# The ON-DISK original is ALWAYS kept as-is (full resolution, original format)
# — the user-facing display (WebUI + WeChat/Feishu) shows THAT. Only the copy
# SENT TO THE CLOUD MODEL is recompressed here to (a) stay under the provider's
# hard per-image cap (Anthropic rejects a base64 image > 5 MB) and (b) not blow
# the conversation context.
#
# KEY DESIGN (coordinate accuracy): resolution is NOT downscaled by default.
# The ``computer`` tool's click/drag coordinates are pixels of the screenshot
# the model saw; if we shrank that image the model's coordinates would map to
# the wrong screen pixels. Real 1080p screenshots recompress to a few hundred
# KB as WebP/JPEG (measured), so a format change ALONE clears the 5 MB cap with
# no resize — zero coordinate drift. Downscaling is a LAST-RESORT fallback only
# when even the lowest quality still exceeds the cap (e.g. a 4K multi-monitor
# composite); that case is rare and accepts a small drift over a hard rejection.
_CLOUD_MAX_BYTES = 4_500_000  # base64-expanded stays comfortably under 5 MB
_CLOUD_QUALITY_FLOOR = 30
_CLOUD_MIN_EDGE_PX = 768

#: Default cloud-copy format + quality (user-overridable via forge_config →
#: chat.image_cloud_format / chat.image_cloud_quality, surfaced in the Agent
#: settings panel). WebP q50 balances tiny size + adequate fidelity for the
#: model to read UI text / locate targets.
_DEFAULT_CLOUD_FORMAT = "webp"
_DEFAULT_CLOUD_QUALITY = 50

#: Format name (config value) → (PIL save format, MIME). PNG is lossless
#: (``quality`` ignored) and only useful when the user insists on it.
_CLOUD_FORMATS: dict[str, tuple[str, str]] = {
    "webp": ("WEBP", "image/webp"),
    "jpeg": ("JPEG", "image/jpeg"),
    "jpg": ("JPEG", "image/jpeg"),
    "png": ("PNG", "image/png"),
}


def _encode_for_cloud(
    raw_bytes: bytes,
    mime: str,
    *,
    fmt: str = _DEFAULT_CLOUD_FORMAT,
    quality: int = _DEFAULT_CLOUD_QUALITY,
) -> "tuple[bytes, str]":
    """Return a (bytes, mime) copy of ``raw_bytes`` sized for a cloud request.

    Recompresses to ``fmt`` at ``quality`` WITHOUT resizing (so screenshot
    coordinates stay valid). If still over ``_CLOUD_MAX_BYTES``, steps quality
    down, and only as a LAST resort proportionally downscales. NEVER mutates
    the on-disk file. Any failure (Pillow absent, decode error) returns the
    original bytes/mime unchanged so a turn is never broken.
    """
    save_fmt, out_mime = _CLOUD_FORMATS.get(
        (fmt or "").strip().lower(), _CLOUD_FORMATS[_DEFAULT_CLOUD_FORMAT]
    )
    try:
        q = int(quality)
    except (TypeError, ValueError):
        q = _DEFAULT_CLOUD_QUALITY
    q = max(1, min(100, q))

    # Already small AND no recompression needed (same format, under cap) → send
    # as-is. A PNG screenshot is recompressed even when small so the configured
    # (smaller) format still applies for context savings.
    if (
        len(raw_bytes) <= _CLOUD_MAX_BYTES
        and mime == out_mime
    ):
        return raw_bytes, mime
    try:
        import io

        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow unavailable → send original
        return raw_bytes, mime
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.load()
            # PNG/WebP keep alpha; JPEG cannot — flatten onto white for JPEG.
            if save_fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                base = bg
            elif img.mode == "P":
                base = img.convert("RGBA")
            else:
                base = img

            def _save(frame: "Image.Image", quality_: int) -> bytes:
                buf = io.BytesIO()
                if save_fmt == "PNG":
                    frame.save(buf, format="PNG", optimize=True)
                elif save_fmt == "WEBP":
                    frame.save(buf, format="WEBP", quality=quality_, method=6)
                else:  # JPEG
                    frame.save(
                        buf, format="JPEG", quality=quality_, optimize=True
                    )
                return buf.getvalue()

            # 1) No resize: try the configured quality, then step down.
            cur_q = q
            best = _save(base, cur_q)
            while len(best) > _CLOUD_MAX_BYTES and cur_q > _CLOUD_QUALITY_FLOOR:
                cur_q = max(_CLOUD_QUALITY_FLOOR, cur_q - 10)
                best = _save(base, cur_q)
            if len(best) <= _CLOUD_MAX_BYTES:
                return best, out_mime

            # 2) LAST-RESORT downscale (rare: 4K/multi-monitor). Accepts small
            #    coordinate drift over a hard >5 MB rejection.
            w, h = base.size
            edge = max(w, h)
            while len(best) > _CLOUD_MAX_BYTES and edge > _CLOUD_MIN_EDGE_PX:
                edge = max(_CLOUD_MIN_EDGE_PX, int(edge * 0.8))
                scale = edge / float(max(w, h))
                frame = base.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.LANCZOS,
                )
                best = _save(frame, cur_q)
            return best, out_mime
    except Exception:  # noqa: BLE001 — decode/encode failure → send original
        return raw_bytes, mime


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
    cloud_format: str = _DEFAULT_CLOUD_FORMAT,
    cloud_quality: int = _DEFAULT_CLOUD_QUALITY,
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
            # Recompress the CLOUD copy only (on-disk original is untouched) to
            # the configured format/quality so it fits the provider's per-image
            # cap and stays light in context. Applies uniformly to user uploads
            # AND computer screenshots (both reach the cloud through this path).
            cloud_bytes, cloud_mime = _encode_for_cloud(
                raw_bytes, mime, fmt=cloud_format, quality=cloud_quality
            )
            b64_str = base64.b64encode(cloud_bytes).decode("ascii")
            data_url = f"data:{cloud_mime};base64,{b64_str}"
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
