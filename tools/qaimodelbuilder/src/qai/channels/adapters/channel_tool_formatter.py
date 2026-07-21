# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Rich tool-progress formatter for channel push notifications (S9 PR-093).

Restores the per-tool icon + path-extraction formatting that the legacy
``backend/channels/wechat/cc_handler.py:_format_tool_line`` (lines
873-940) provided to WeChat / Feishu users.  Without this adapter the
:class:`~qai.channels.application.tool_progress_aggregator.ToolProgressAggregator`
falls back to a generic ``tool_name + success/error icon`` line —
addressed by parity-audit row §3.1 F-10 (and also §2.4 L-9 "Rich tool
progress formatting").

The formatter is **stateless and side-effect-free**: callers (the
dispatch bridge in :mod:`apps.api._channel_dispatch_bridge` and the
ai_coding bridge in :mod:`apps.api._ai_coding_channel_bridge`) feed
in ``(tool_name, args)`` plus a status enum and receive a single
formatted line back.  No I/O, no logging, no globals — fits the
"adapter = pure translator" rule of layered-channels.

Tool-name normalisation
-----------------------
The bundled icon table covers Claude-Code's PascalCase tool names
(``Read`` / ``Write`` / ``Edit`` / ``Bash`` / ``Glob`` / ``Grep`` /
``LS`` / ``TodoWrite`` / ``TodoRead`` / ``WebFetch`` / ``WebSearch`` /
``MultiEdit``) and the lower-case names used by the OpenAI-compat
agentic loop (``read`` / ``write`` / ``edit`` / ``exec`` / ``glob`` /
``grep`` / ``ls`` / ``code``); both shapes are mapped to the same
canonical PascalCase name before icon lookup so a single channel
session can display mixed CC + OC progress consistently.

Path extraction
---------------
For file-oriented tools the formatter pulls the path from the first
of ``file_path`` / ``path`` / ``filename`` it finds in the args dict
(legacy CC mode used ``file_path``, the OpenAI-compat exec adapter
uses ``path``).  Long paths are tail-truncated to ~30 chars with a
leading ellipsis so the channel reply stays single-line on mobile.
Long bash commands / URLs are head-truncated to 60 chars.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "ChannelToolFormatter",
    "ToolStatus",
]


class ToolStatus(str, Enum):
    """Lifecycle status of a single tool invocation."""

    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


# Normalisation: map both CC PascalCase and OC lower-case to canonical
# PascalCase so the icon table only needs one entry per tool.
_TOOL_ALIAS: dict[str, str] = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "multiedit": "MultiEdit",
    "exec": "Bash",
    "bash": "Bash",
    "code": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "search": "Grep",
    "ls": "LS",
    "list_dir": "LS",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "background_process": "BackgroundProcess",
}

# Per-canonical-name icon + Chinese verb (matches legacy cc_handler
# output verbatim for parity).
_ICON_VERB: dict[str, tuple[str, str]] = {
    "Read": ("\U0001f4d6", "读取"),       # 📖
    "Write": ("\u270f\ufe0f", "写入"),     # ✏️
    "Edit": ("\U0001f527", "编辑"),       # 🔧
    "MultiEdit": ("\U0001f527", "多处编辑"),
    "Glob": ("\U0001f50d", "搜索"),       # 🔍
    "Grep": ("\U0001f50e", "查找"),       # 🔎
    "Bash": ("\U0001f4bb", "执行"),       # 💻
    "LS": ("\U0001f4c2", "列目录"),       # 📂
    "TodoWrite": ("\U0001f4dd", "更新 Todo"),
    "TodoRead": ("\U0001f4cb", "读取 Todo"),
    "WebFetch": ("\U0001f310", "获取网页"),
    "WebSearch": ("\U0001f50d", "网络搜索"),
    "BackgroundProcess": ("\U0001f680", "后台进程"),  # 🚀
}

_PATH_TAIL_MAX = 30
_CMD_HEAD_MAX = 60
_URL_HEAD_MAX = 60


def _truncate_tail(value: str, *, limit: int = _PATH_TAIL_MAX) -> str:
    """Tail-truncate a path so the *end* (filename) stays visible.

    Mobile channel replies are single-line; the user typically cares
    about the leaf file rather than the project root, so we keep the
    last ``limit`` chars and prefix with an ellipsis when the value
    is longer.
    """
    if len(value) <= limit:
        return value
    return "\u2026" + value[-(limit - 1) :]


def _truncate_head(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\u2026"


def _extract_path(args: Mapping[str, Any]) -> str:
    """Pull the most-likely path argument out of a tool call's args.

    Handles all three legacy keys (``file_path`` for CC,
    ``path`` for OpenAI-compat tools, ``filename`` for some legacy
    paths).  Returns ``""`` when no path is present.
    """
    for key in ("file_path", "path", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class ChannelToolFormatter:
    """Translate ``(tool_name, args, status)`` into a channel-ready line.

    Designed for plug-in into
    :class:`~qai.channels.application.tool_progress_aggregator.ToolProgressAggregator`
    by composition: the aggregator collects raw events and the bridge
    in :mod:`apps.api._channel_dispatch_bridge` calls
    :meth:`format_progress` per event before pushing to the user via
    the realtime delivery service (D-5).

    The formatter has no constructor parameters by design — every
    rule comes from the module-level tables above so behavior is
    fully deterministic and unit-testable without DI.
    """

    def normalize(self, tool_name: str) -> str:
        """Return the canonical PascalCase name for ``tool_name``.

        Unknown names are returned verbatim so they still surface
        in the formatted line (caller falls back to a generic icon).
        """
        if not tool_name:
            return ""
        key = tool_name.strip().lower()
        return _TOOL_ALIAS.get(key, tool_name.strip())

    def format_progress(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        status: ToolStatus = ToolStatus.SUCCESS,
    ) -> str:
        """Build a single-line description of a tool invocation.

        Format matches the legacy ``_format_tool_line`` output so
        existing channel users see no UX regression, with the status
        icon prefixed (\u2705 / \u274c / \u23f3) when ``status`` is supplied.
        """
        canonical = self.normalize(tool_name)
        body = self._format_body(canonical, args or {})
        # Prepend status icon only for terminal states; running events
        # already carry the rotating "\U0001f504" (🔄) header at the
        # aggregator level so we don't duplicate it here.
        if status is ToolStatus.SUCCESS:
            return f"\u2705 {body}"
        if status is ToolStatus.ERROR:
            return f"\u274c {body}"
        return f"\u23f3 {body}"

    # ------------------------------------------------------------------
    # Body builder
    # ------------------------------------------------------------------
    def _format_body(self, canonical: str, args: Mapping[str, Any]) -> str:
        if canonical == "Read":
            return f"\U0001f4d6 读取: {_truncate_tail(_extract_path(args))}"
        if canonical == "Write":
            return f"\u270f\ufe0f 写入: {_truncate_tail(_extract_path(args))}"
        if canonical == "Edit":
            return f"\U0001f527 编辑: {_truncate_tail(_extract_path(args))}"
        if canonical == "MultiEdit":
            return f"\U0001f527 多处编辑: {_truncate_tail(_extract_path(args))}"
        if canonical == "Glob":
            return f"\U0001f50d 搜索: {args.get('pattern', '') or ''}"
        if canonical == "Grep":
            return f"\U0001f50e 查找: {args.get('pattern', '') or ''}"
        if canonical == "Bash":
            cmd = str(args.get("command", "") or "")
            return f"\U0001f4bb 执行: {_truncate_head(cmd, limit=_CMD_HEAD_MAX)}"
        if canonical == "LS":
            return f"\U0001f4c2 列目录: {_extract_path(args)}"
        if canonical == "TodoWrite":
            return "\U0001f4dd 更新 Todo"
        if canonical == "TodoRead":
            return "\U0001f4cb 读取 Todo"
        if canonical == "WebFetch":
            url = str(args.get("url", "") or "")
            return f"\U0001f310 获取网页: {_truncate_head(url, limit=_URL_HEAD_MAX)}"
        if canonical == "WebSearch":
            return f"\U0001f50d 网络搜索: {args.get('query', '') or ''}"
        if canonical == "BackgroundProcess":
            # ``background_process`` is action-oriented (start / list / stop /
            # restart / status / logs), not path-oriented. Surface the action
            # plus the most-informative secondary field so a mobile channel
            # user can see WHAT changed at a glance.
            action = str(args.get("action", "") or "").strip()
            secondary = ""
            if action == "start":
                cmd = str(args.get("command", "") or "")
                if cmd:
                    secondary = f": {_truncate_head(cmd, limit=_CMD_HEAD_MAX)}"
            elif action in ("status", "logs", "stop", "restart"):
                pid = str(args.get("id", "") or "")
                if pid:
                    secondary = f": {pid}"
            return f"\U0001f680 后台进程 {action or 'tool'}{secondary}"
        # Fallback — unknown tool name: hammer icon + raw name, parity
        # with legacy ``f"\U0001f528 {tool_name}"``.
        return f"\U0001f528 {canonical or 'tool'}"

    def format_batch(
        self,
        events: list[tuple[str, Mapping[str, Any], ToolStatus]],
        *,
        batch_index: int = 1,
    ) -> str:
        """Format a list of tool events as a single channel push message.

        Mirrors the "🔄 工具调用进度（第 N 批）：" header used by the
        legacy CC handler; the aggregator (D-1) calls this with a
        flushed batch when the throttle window opens.
        """
        if not events:
            return ""
        lines = [f"\U0001f504 工具调用进度（第 {batch_index} 批）："]
        for name, args, status in events:
            lines.append("  " + self.format_progress(name, args, status))
        return "\n".join(lines)
