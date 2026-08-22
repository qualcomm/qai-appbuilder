# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure text shaping + argument validation for ai_coding channel subcommands.

Architecture cleanup (A-1 step2): the apps-layer
:class:`apps.api._ai_coding_channel_bridge.AiCodingChannelBridge` used to
inline the reply-text formatting for ``/cc`` / ``/oc`` subcommands as
private methods.  Those formatters are pure — they only read attributes
off already-resolved session objects and shape strings — so they belong in
the ai_coding application layer rather than the cross-context bridge.

The bridge keeps the I/O orchestration (resolving use cases, executing
commands); it calls these helpers to build the user-facing strings.

No ``apps`` / ``qai.channels`` import — import-linter-safe ai_coding
application code.
"""

from __future__ import annotations

from typing import Any


def needs_id_text(verb: str, sub: str) -> str:
    """Usage hint shown when a subcommand requires a session id."""
    return f"\u2753 用法：/{verb} {sub} <会话id>"


def missing_field_text(verb: str, sub: str, field: str) -> str:
    """Reply shown when the backing DI use case for a subcommand is absent."""
    return (
        f"\u26a0\ufe0f /{verb} {sub} 暂不可用"
        f"（{field} 未注册）。"
    )


def format_session_list(
    verb: str,
    sessions: list[Any],
    active_session_id: str | None = None,
) -> str:
    """Format the ``/cc list`` / ``/oc list`` reply.

    Reads ``title`` / ``status`` / ``session_id`` (+ the per-session
    model selection) off each session object; pure string shaping with
    no collaborator calls.  RE-OC-5: surface the per-session model when
    known so the channel list matches V1 ``wechat/oc_handler.py:104-111``
    (``| <model>`` suffix) instead of dropping it.

    D-2 (V0.5 ``wechat/cc_handler.py:142-145`` / ``oc:114-119``): mark
    the currently-active session with ``▶`` and the rest with ``○`` and
    append the ``/cc use`` re-enter hint.  ``active_session_id`` is the
    dispatch-resolved current session for this channel user (``None`` when
    no session is active — then every row is ``○``).
    """
    if not sessions:
        return f"\u2139\ufe0f 当前没有活动的 /{verb} 会话。"
    lines = [f"\U0001f4cb /{verb} 活动会话（{len(sessions)}）："]
    for s in sessions:
        title = getattr(s, "title", None) or "(未命名)"
        status = getattr(getattr(s, "status", None), "value", "")
        model = _session_model(s)
        model_suffix = f" | {model}" if model else ""
        marker = (
            "\u25b6"
            if active_session_id is not None
            and str(s.session_id) == str(active_session_id)
            else "\u25cb"
        )
        lines.append(
            f"{marker} {s.session_id} [{status}] {title}{model_suffix}"
        )
    lines.append("\u25b6=当前激活  \u25cb=已暂停")
    lines.append(f"\U0001f4a1 /{verb} use <序号|ID前缀> 可重新进入会话")
    return "\n".join(lines)


def _session_model(session: Any) -> str:
    """Return the per-session model id for display, or ``""``.

    OpenCode sessions carry ``oc_current_model`` (V1
    ``opencode_session_models.py:50`` ``current_model_id``); fall back to
    a generic ``model`` attribute / the session config's effort-adjacent
    ``model`` if present.  Pure attribute reads — never raises.
    """
    model = getattr(session, "oc_current_model", None)
    if isinstance(model, str) and model:
        return model
    model = getattr(session, "model", None)
    if isinstance(model, str) and model:
        return model
    cfg = getattr(session, "config", None)
    cfg_model = getattr(cfg, "model", None)
    if isinstance(cfg_model, str) and cfg_model:
        return cfg_model
    return ""


def format_session_status(session: Any) -> str:
    """Format the ``/cc status`` / ``/oc status`` reply for one session.

    RE-OC-5: align with V1 ``wechat/oc_handler.py:162-197`` /
    ``feishu/oc_handler.py`` by surfacing the conversation turn count,
    cumulative tool-call count, the per-session model / provider and the
    cumulative input-token estimate (V2's aggregate exposes all of these:
    ``entities.py:305-307,321-322,332``).  Counters are only appended when
    non-zero / set so a fresh session's status stays terse.
    """
    title = getattr(session, "title", None) or "(未命名)"
    status = getattr(getattr(session, "status", None), "value", "")
    workspace = getattr(getattr(session, "workspace", None), "path", "")
    turn_count = getattr(session, "turn_count", None)
    if not isinstance(turn_count, int):
        turn_count = len(getattr(session, "messages", []) or [])
    lines = [
        f"\U0001f9de 会话 {session.session_id}",
        f"标题：{title}",
        f"状态：{status}",
        f"工作目录：{workspace}",
        f"对话轮次：{turn_count}",
    ]
    tool_calls = getattr(session, "total_tool_calls", 0)
    if isinstance(tool_calls, int) and tool_calls > 0:
        lines.append(f"工具调用：{tool_calls} 次")
    model = _session_model(session)
    if model:
        lines.append(f"当前模型：{model}")
    provider = getattr(session, "oc_current_provider", None)
    if isinstance(provider, str) and provider:
        lines.append(f"Provider：{provider}")
    in_tokens = getattr(session, "total_input_tokens", 0)
    if isinstance(in_tokens, int) and in_tokens > 0:
        tok_str = (
            f"~{in_tokens}"
            if in_tokens < 10_000
            else f"~{round(in_tokens / 1000)}K"
        )
        lines.append(f"累计 Token：{tok_str}")
    return "\n".join(lines)


def format_unknown_subcommand(verb: str, sub: str) -> str:
    """Reply shown for an unrecognised ``/cc`` / ``/oc`` subcommand."""
    return (
        f"\u2753 未知的 /{verb} 子命令: /{verb} {sub or '<空>'}\n"
        f"可用：new / list / use / status / rename / close / "
        f"cd / fork / stop / models / model"
    )
