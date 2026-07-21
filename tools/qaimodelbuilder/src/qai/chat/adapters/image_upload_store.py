# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed image upload store (PR-403 / S7.5 lane L4).

Migrates :class:`backend.image_store.ImageStore` (155 LOC) into the
chat bounded context.  Behaviour preserved:

* Path layout: ``<root>/<YYYY-MM-DD>/<safe_conv>/<safe_msg>.<ext>``
* MIME → extension map (jpg / png / gif / webp; unknown → jpg)
* Path-traversal guard via ``Path.relative_to`` (legacy
  comments at backend/image_store.py:135-145 explain why this is
  preferred over ``startswith``).
* Idempotent save: same ``(conv, msg)`` pair re-uses the existing
  file when present without rewriting bytes.

URL prefix matches legacy ``/api/images/files/...`` so existing
front-end fetches continue to work via the static-files mount in
``apps/api/_spa_mount.py:_mount_images``. The ``root`` is injected by
``apps/api/_chat_di.py`` from ``DataPaths.blob_dir("chat")``
(``data/blobs/chat``; ARCH-2 2026-06-09 — previously the ad-hoc
``data/images``). The URL prefix is a V1-locked contract and is
unchanged; only the physical directory moved under ``blobs/``.

The base64 decode + file write is CPU-bound enough to merit running on
the asyncio default thread pool (``asyncio.to_thread``); chat use cases
``await`` the result so the event loop stays responsive when several
images are uploaded in parallel.

This adapter is intentionally chat-local: it takes a plain ``root``
``Path`` and does not import other contexts' adapters (the
``context-isolation`` import-linter contract forbids that). Resolving
that ``root`` through the platform ``DataPaths`` port happens in the
composition root (``_chat_di`` / ``_spa_mount``), not here, so the
adapter stays free of any cross-context or platform-storage coupling.
The path layout, URL prefix, and image bytes all stay inside the chat
bounded context because the legacy front-end only ever fetched these
images via ``/api/images/files/...`` issued by the chat API surface.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import date
from pathlib import Path

from qai.chat.application.ports import (
    ImageUploadRequest,
    ImageUploadResult,
    ImageUploadStorePort,
)
from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger

_log = get_logger(__name__)
_logger_legacy = logging.getLogger("qai.chat.image_upload")


URL_PREFIX: str = "/api/images/files"
"""Mount path served by ``apps/api/main.py`` (legacy parity).  Tests
read this constant rather than hard-coding the prefix."""


_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


_FILENAME_MAX_LEN: int = 64


def _safe_name(name: str) -> str:
    """Sanitize a filename component.

    Migrated from ``backend/image_store.py:_safe_name``: keep
    ``[A-Za-z0-9_-]``, replace anything else with ``_``, truncate to
    :data:`_FILENAME_MAX_LEN` characters, fall back to ``"unknown"``
    when the result is empty.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    truncated = cleaned[:_FILENAME_MAX_LEN]
    return truncated or "unknown"


class FileSystemImageUploadStore(ImageUploadStorePort):
    """Filesystem-backed :class:`ImageUploadStorePort`."""

    __slots__ = ("_root",)

    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        _log.info("chat.image_upload_store_ready", root=str(root))

    # ------------------------------------------------------------------
    # Port API
    # ------------------------------------------------------------------
    async def save_base64(
        self,
        request: ImageUploadRequest,
    ) -> ImageUploadResult:
        try:
            url, disk_path = await asyncio.to_thread(
                self._save_sync,
                request.conversation_id,
                request.message_id,
                request.base64_data,
                request.mime_type,
            )
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.image_upload_failed",
                f"failed to save image: {exc}",
                operation="image_upload.save_base64",
                cause=exc,
            ) from exc
        return ImageUploadResult(url=url, disk_path=disk_path)

    # ------------------------------------------------------------------
    # Test / introspection helpers
    # ------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    def get_path(self, url: str) -> Path | None:
        """Reverse the URL → on-disk path mapping (synchronous helper).

        Mirrors :meth:`backend.image_store.ImageStore.get_path` for
        downstream consumers (e.g. AppBuilder runners) that already
        hold the URL and need the absolute path.  Returns ``None`` for
        URLs outside :data:`URL_PREFIX` or when the file does not
        exist; logs and returns ``None`` on traversal attempts.
        """
        if not url or not url.startswith(URL_PREFIX + "/"):
            return None
        rel = url[len(URL_PREFIX) + 1:]
        try:
            root_resolved = self._root.resolve()
            abs_path = (self._root / rel).resolve()
            abs_path.relative_to(root_resolved)
        except ValueError:
            _log.warning("chat.image_upload_traversal_blocked", url=url)
            return None
        except Exception:  # pragma: no cover - defensive
            return None
        return abs_path if abs_path.exists() else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _save_sync(
        self,
        conv_id: str,
        msg_id: str,
        b64_data: str,
        mime_type: str,
    ) -> tuple[str, str | None]:
        ext = _MIME_TO_EXT.get((mime_type or "").lower(), "jpg")
        date_str = date.today().isoformat()
        safe_conv = _safe_name(conv_id)
        safe_msg = _safe_name(msg_id)

        rel_path = Path(date_str) / safe_conv / f"{safe_msg}.{ext}"
        abs_path = self._root / rel_path

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if not abs_path.exists():
            try:
                raw = base64.b64decode(b64_data, validate=False)
            except Exception as exc:  # noqa: BLE001
                raise PersistenceError(
                    "chat.image_upload_invalid_base64",
                    f"could not decode base64 payload: {exc}",
                    operation="image_upload.decode",
                    cause=exc,
                ) from exc
            abs_path.write_bytes(raw)

        url = f"{URL_PREFIX}/{rel_path.as_posix()}"
        return url, str(abs_path.resolve())


__all__ = [
    "FileSystemImageUploadStore",
    "URL_PREFIX",
]
