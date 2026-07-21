# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aggregates tool execution progress events for batched channel push.

PR-097 R-10 deliverable.  Restores the legacy
``backend/channels/wechat/cc_handler.py:451-580,873-940`` 3-layer
batching logic (``last_progress_send_time``, ``_TOOL_BATCH_SIZE``,
``_TOOL_PROGRESS_MIN_INTERVAL``) that surfaced CC/OC tool progress
to the channel user as ``📖 读取 / ✏️ 写入 / 🔧 编辑`` lines —
addressed by parity-audit row §3.1 F-10.

Two complementary record / format paths
---------------------------------------

1. **Legacy summary path** — :class:`ToolProgressEvent` carries a
   simple ``(tool_name, success, summary)`` triple; :meth:`flush`
   emits a generic ``✅ tool_name — summary`` line per event.  Kept
   verbatim because the existing ai_coding plain-text adapter feeds
   these.
2. **Rich path (PR-097 R-10)** — :meth:`add_rich` records a
   ``(tool_name, args, status)`` triple matching
   :class:`~qai.channels.adapters.channel_tool_formatter.ChannelToolFormatter.format_progress`.
   :meth:`flush_rich` produces a single multi-line message via
   :meth:`ChannelToolFormatter.format_batch` so the channel user
   sees the same icon-prefixed lines the legacy CC handler emitted.

Flush triggers
--------------

A flush is due when *either* condition holds:

* The batch has reached :attr:`batch_size` events (default 5).
* :attr:`min_interval_seconds` (default 2.5s) has passed since the
  last flush.

Both numbers are tunable via the dataclass init args; the dispatch
bridge in :mod:`apps.api._channel_dispatch_bridge` constructs one
per inbound message so the timing windows do not leak across users.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class ToolProgressEvent:
    """A single tool execution result (legacy summary form)."""
    tool_name: str
    success: bool
    summary: str = ""


@dataclass
class RichToolEvent:
    """Rich tool event for :class:`ChannelToolFormatter` consumption."""
    tool_name: str
    args: Mapping[str, Any]
    status: Any  # ToolStatus — kept untyped to avoid hard import here


@dataclass
class ToolProgressAggregator:
    """Batches tool progress events for periodic user push.

    Usage (legacy summary path)::

        agg = ToolProgressAggregator(batch_size=5, min_interval_seconds=2.5)
        agg.add(ToolProgressEvent(tool_name="read_file", success=True))
        if agg.should_flush():
            message = agg.flush()
            # send message to channel user

    Usage (rich path, PR-097 R-10)::

        agg = ToolProgressAggregator(batch_size=5, min_interval_seconds=2.5)
        agg.add_rich(RichToolEvent("Read", {"file_path": "..."}, ToolStatus.SUCCESS))
        if agg.should_flush():
            message = agg.flush_rich(formatter)
    """
    batch_size: int = 5
    min_interval_seconds: float = 2.5
    _events: list[ToolProgressEvent] = field(default_factory=list, init=False)
    _rich_events: list[RichToolEvent] = field(
        default_factory=list, init=False
    )
    _last_flush_time: float = field(default_factory=time.monotonic, init=False)
    _batch_index: int = field(default=0, init=False)

    def add(self, event: ToolProgressEvent) -> None:
        """Add a legacy summary tool progress event."""
        self._events.append(event)

    def add_rich(self, event: RichToolEvent) -> None:
        """Add a rich tool event for :class:`ChannelToolFormatter` formatting."""
        self._rich_events.append(event)

    def should_flush(self) -> bool:
        """Check if we have enough events or enough time has passed."""
        total = len(self._events) + len(self._rich_events)
        if total == 0:
            return False
        if total >= self.batch_size:
            return True
        if time.monotonic() - self._last_flush_time >= self.min_interval_seconds:
            return True
        return False

    def flush(self) -> str:
        """Format and return all pending legacy events as text, then clear."""
        if not self._events:
            return ""
        lines = [f"\U0001f504 工具调用进度（{len(self._events)} 项）："]
        for ev in self._events:
            icon = "\u2705" if ev.success else "\u274c"
            line = f"  {icon} {ev.tool_name}"
            if ev.summary:
                line += f" — {ev.summary}"
            lines.append(line)
        self._events.clear()
        self._last_flush_time = time.monotonic()
        return "\n".join(lines)

    def flush_rich(self, formatter: Any) -> str:
        """Format pending rich events via :class:`ChannelToolFormatter`.

        ``formatter`` is duck-typed so this module does not import the
        adapter (keeps the application layer free of adapter imports).
        Returns ``""`` when there are no pending rich events.
        """
        if not self._rich_events:
            return ""
        self._batch_index += 1
        triple_list = [
            (ev.tool_name, ev.args, ev.status)
            for ev in self._rich_events
        ]
        text = formatter.format_batch(
            triple_list, batch_index=self._batch_index
        )
        self._rich_events.clear()
        self._last_flush_time = time.monotonic()
        return text

    @property
    def pending_count(self) -> int:
        return len(self._events) + len(self._rich_events)
