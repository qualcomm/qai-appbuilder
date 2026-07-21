# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Tool-result storage for oversized tool outputs (V1 parity).

Restores the legacy ``backend/tool_result_storage.py`` behaviour that
``backend/tools/_exec.py`` relied on: when a tool produces a body above
a byte threshold, persist the *full* body to a retrievable file and
return a ``head + omit-marker + tail`` preview to the model plus a hint
that it can ``read(path=...)`` the saved file to recover the elided
middle.

The V2 production path lost this: ``tool_exec`` hard-truncated large
outputs at 200 KB and discarded the middle with no retrieval path.
This module re-implements the capability inside the ``ai_coding``
context (where the tool handlers actually execute), backing the
:class:`qai.ai_coding.application.ports.ToolResultStorePort`.

Design notes
------------
* The algorithm is self-contained: byte-boundary-aware head/tail
  slicing, path-escape guard on fetch, and disk-failure degradation to
  preview-only.  It is the sole production implementation of the
  oversized-tool-output persistence behaviour and depends on no other
  module for the slicing/persistence logic.
* Persistence target mirrors the legacy ``data/tool_results/`` layout
  so a model that quotes ``read(path=<root>/<file>)`` recovers the body
  through the existing ``read`` tool — no special-case retrieval logic
  is needed, the file is a real on-disk file the ``read`` tool can open
  (provided the directory is within the ``read`` tool's allowed roots).

Cross-context isolation
-----------------------
Imports stdlib only (plus the application-layer port/value object).  No
imports of ``qai.security.*`` / ``qai.tools.*`` / any other bounded
context.  Lives under ``infrastructure/`` per the ``layered-ai_coding``
contract.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from qai.ai_coding.application.ports import ToolResultPreview, ToolResultStorePort

logger = logging.getLogger("qai.ai_coding.tools.tool_result_store")


# ---------------------------------------------------------------------------
# Constants — sized to match the legacy backend defaults so model-side
# expectations (head ~ 8 KB, tail ~ 4 KB, store above 16 KB) survive the
# cutover.
# ---------------------------------------------------------------------------

PREVIEW_THRESHOLD_BYTES: int = 16 * 1024
"""Default byte threshold above which an output is persisted + previewed."""

PREVIEW_HEAD_BYTES: int = 8 * 1024
"""Default number of leading bytes kept in the model-facing preview."""

PREVIEW_TAIL_BYTES: int = 4 * 1024
"""Default number of trailing bytes kept in the model-facing preview."""

DEFAULT_TRUNCATION_ADVICE: str = (
    "[truncation_advice] If you need the omitted middle, "
    "call read(path=...) on the saved file path above; "
    "otherwise rely on head+tail and continue."
)


__all__ = [
    "DEFAULT_TRUNCATION_ADVICE",
    "PREVIEW_HEAD_BYTES",
    "PREVIEW_TAIL_BYTES",
    "PREVIEW_THRESHOLD_BYTES",
    "FileSystemToolResultStore",
    "build_preview",
    "should_store",
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def should_store(
    output: str,
    *,
    threshold_bytes: int = PREVIEW_THRESHOLD_BYTES,
) -> bool:
    """Return ``True`` iff ``output`` UTF-8 byte length exceeds the threshold."""
    if not output:
        return False
    return len(output.encode("utf-8")) > threshold_bytes


def _sanitize_hint(hint: str, max_len: int = 40) -> str:
    """Reduce ``hint`` to ``[A-Za-z0-9_-]`` — used for filename generation."""
    if not hint:
        return ""
    safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in hint)
    return safe[:max_len].strip("_")


def build_preview(
    output: str,
    *,
    head_bytes: int = PREVIEW_HEAD_BYTES,
    tail_bytes: int = PREVIEW_TAIL_BYTES,
    stored_path: str | None = None,
    truncation_advice: str = DEFAULT_TRUNCATION_ADVICE,
) -> tuple[str, int, int]:
    """Render the ``head + omit + tail`` preview text for ``output``.

    Returns ``(preview_text, total_bytes, omitted_bytes)``.  Decoding is
    deliberately byte-aware: we slice on UTF-8 byte boundaries and decode
    with ``errors="ignore"`` so we never produce broken multi-byte
    sequences in the middle of a CJK character.
    """
    encoded = output.encode("utf-8")
    total_bytes = len(encoded)

    if total_bytes <= head_bytes + tail_bytes:
        # Already short enough — no omission marker needed.  Keeps the
        # "small output ⇒ no mutation" contract even if a buggy caller
        # forgets to gate on :func:`should_store`.
        return output, total_bytes, 0

    head_text = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail_text = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    omitted_bytes = total_bytes - head_bytes - tail_bytes

    omit_marker = (
        f"\n\n... [omitted {omitted_bytes:,} bytes / total "
        f"{total_bytes:,} bytes] ...\n\n"
    )

    if stored_path:
        footer = (
            "\n---\n"
            f"[full_output_saved] path={stored_path}\n"
            f"{truncation_advice}\n"
        )
    else:
        footer = (
            "\n---\n"
            f"[truncated_in_memory] full body not persisted "
            f"(omitted {omitted_bytes:,} bytes)\n"
            f"{truncation_advice}\n"
        )

    preview = f"{head_text}{omit_marker}{tail_text}{footer}"
    return preview, total_bytes, omitted_bytes


# ---------------------------------------------------------------------------
# File system implementation (mirrors legacy data/tool_results/ layout)
# ---------------------------------------------------------------------------


class FileSystemToolResultStore(ToolResultStorePort):
    """Persists oversized tool outputs under a configurable root dir.

    Mirrors the legacy ``backend/tool_result_storage.py`` on-disk layout
    so a model that quotes ``read(path=<root>/<filename>)`` recovers the
    full body through the existing ``read`` tool.

    Disk failures (out-of-space, permission denied) are caught + logged;
    in that case the preview is still rendered but with the
    ``truncated_in_memory`` footer — an I/O hiccup never tanks an
    otherwise-successful tool call (legacy "降级为只预览不持久化" rule).
    """

    __slots__ = ("_head_bytes", "_root", "_tail_bytes", "_threshold_bytes")

    def __init__(
        self,
        root: Path,
        *,
        threshold_bytes: int = PREVIEW_THRESHOLD_BYTES,
        head_bytes: int = PREVIEW_HEAD_BYTES,
        tail_bytes: int = PREVIEW_TAIL_BYTES,
    ) -> None:
        self._root: Path = Path(root)
        self._threshold_bytes: int = threshold_bytes
        self._head_bytes: int = head_bytes
        self._tail_bytes: int = tail_bytes

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_root(self) -> bool:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "tool_result_store: cannot create root %s (%s); "
                "falling back to in-memory preview only",
                self._root,
                exc,
            )
            return False
        return True

    def store(
        self,
        output: str,
        *,
        tool_name: str = "",
        context_hint: str = "",
        force: bool = False,
    ) -> ToolResultPreview:
        if not output:
            # An empty body is never persisted (nothing to recover), even
            # when ``force`` is set — there is no full set to hand back.
            return ToolResultPreview(
                preview=output,
                stored=False,
                stored_path=None,
                total_bytes=0,
                omitted_bytes=0,
                truncated=False,
            )
        if not force and not should_store(
            output, threshold_bytes=self._threshold_bytes
        ):
            return ToolResultPreview(
                preview=output,
                stored=False,
                stored_path=None,
                total_bytes=len(output.encode("utf-8")),
                omitted_bytes=0,
                truncated=False,
            )

        stored_path: Path | None = None
        if self._ensure_root():
            uid = uuid.uuid4().hex[:12]
            hint_part = _sanitize_hint(context_hint)
            suffix = f"_{hint_part}" if hint_part else ""
            filename = f"{tool_name or 'tool'}{suffix}_{uid}.txt"
            candidate = self._root / filename
            try:
                candidate.write_text(output, encoding="utf-8")
                stored_path = candidate
                logger.info(
                    "tool_result_store: persisted %d bytes to %s",
                    len(output.encode("utf-8")),
                    candidate,
                )
            except OSError as exc:
                logger.warning(
                    "tool_result_store: write failed for %s (%s); "
                    "falling back to preview-only",
                    candidate,
                    exc,
                )

        preview, total_bytes, omitted_bytes = build_preview(
            output,
            head_bytes=self._head_bytes,
            tail_bytes=self._tail_bytes,
            stored_path=str(stored_path) if stored_path is not None else None,
        )
        return ToolResultPreview(
            preview=preview,
            stored=stored_path is not None,
            stored_path=str(stored_path) if stored_path is not None else None,
            total_bytes=total_bytes,
            omitted_bytes=omitted_bytes,
            truncated=True,
        )

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Delete persisted tool-result files older than ``max_age_hours``.

        V1 parity (``backend/tool_result_storage.py:171-198``
        ``cleanup_old_results``): walk the store root, ``unlink`` every
        regular file whose ``mtime`` is older than the age threshold, and
        return the number of files removed.  Persistence is best-effort:
        a per-file :class:`OSError` (e.g. a write-locked / vanished file)
        is caught + logged and the sweep continues — one bad file never
        aborts the whole GC pass.  A missing root is a no-op (``return 0``)
        so the periodic GC task started at lifespan can run safely before
        any output has ever been persisted.
        """
        if not self._root.exists():
            return 0

        now = time.time()
        max_age_seconds = max_age_hours * 3600
        cleaned = 0
        try:
            entries = list(self._root.iterdir())
        except OSError as exc:
            logger.warning(
                "tool_result_store: cannot list root %s for cleanup (%s)",
                self._root,
                exc,
            )
            return 0

        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                age = now - entry.stat().st_mtime
            except OSError:
                # Vanished / inaccessible mid-sweep — skip it (best-effort).
                continue
            if age <= max_age_seconds:
                continue
            try:
                entry.unlink()
                cleaned += 1
            except OSError as exc:
                logger.warning(
                    "tool_result_store: cleanup failed for %s (%s)",
                    entry,
                    exc,
                )

        if cleaned > 0:
            logger.info(
                "tool_result_store: cleaned %d expired result file(s) "
                "(max_age=%dh)",
                cleaned,
                max_age_hours,
            )
        return cleaned
