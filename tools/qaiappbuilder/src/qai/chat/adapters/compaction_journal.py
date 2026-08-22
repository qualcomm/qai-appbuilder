# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Append-only JSONL journal for compaction events (CONTEXT-COMPRESSION-NEXT §5.1).

Best-effort, non-blocking sink for one line per compaction. Every
:meth:`CompactionJournal.append` is fire-and-forget: exceptions are swallowed
to ``logger.debug`` so the caller's turn never fails because of the journal.

- Serialised writes: a module-level :class:`asyncio.Lock` serialises writes
  across all instances in the process. File I/O runs on a worker thread via
  :func:`asyncio.to_thread` so the event loop is never blocked.
- Daily rotation on size: when the active file grows past ``_MAX_BYTES``
  (8 MiB), it is renamed to ``compaction-events.YYYY-MM-DD.jsonl`` and a
  fresh file is opened on the next write.
- UTF-8 with ``ensure_ascii=False`` so non-ASCII payloads (Chinese, etc.)
  round-trip verbatim; explicit ``newline="\\n"`` keeps line boundaries LF
  on every platform (AGENTS.md §9 cross-platform rule).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from qai.platform.logging import get_logger

__all__ = ["CompactionJournal"]

_log = get_logger(__name__)
_LOCK = asyncio.Lock()  # single-process serialisation across all instances
_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB before daily rotation


class CompactionJournal:
    """Best-effort append-only JSONL sink for compaction events."""

    def __init__(self, base_dir: Path) -> None:
        base_dir.mkdir(parents=True, exist_ok=True)
        self._path = base_dir / "compaction-events.jsonl"

    async def append(self, event: dict[str, Any]) -> None:
        """Fire-and-forget append. NEVER raises."""
        try:
            event.setdefault("ts", time.time())
            line = json.dumps(event, ensure_ascii=False) + "\n"
            async with _LOCK:
                await asyncio.to_thread(self._append_sync, line)
        except Exception as exc:  # noqa: BLE001 -- best-effort sink
            _log.debug("compaction_journal.append_failed", error=str(exc))

    def _append_sync(self, line: str) -> None:
        # Rotate the active file once it crosses the size cap. ``exists()``
        # guards ``stat()`` so a missing file (first ever write) is fine.
        if self._path.exists() and self._path.stat().st_size >= _MAX_BYTES:
            stamp = time.strftime("%Y-%m-%d")
            rotated = self._path.with_name(f"compaction-events.{stamp}.jsonl")
            self._path.rename(rotated)
        with open(self._path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
