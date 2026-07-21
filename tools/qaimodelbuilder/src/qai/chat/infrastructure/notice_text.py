# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User-facing notice text builders for stream-end edge cases (PR-090, S9 F-1/F-2).

Implements audit items :ref:`F-1` (truncation notice on
``finish_reason=length``) and :ref:`F-2` (content-filter notice on
``finish_reason=content_filter``) from
``docs/90-refactor/S9-final-parity-audit.md`` §3.1.

The legacy stream emitted Chinese, tool-name aware notices when the
model stopped mid-generation — the rewritten adapter regressed these to
generic English placeholders (``"[Response truncated due to max token
limit]"`` / ``"[Response stopped: content filter triggered]"``), losing
the recovery hint (``/compact``-history advice) and the tool-call
context.  This module restores the legacy semantics with two pure
functions; the SSE consumer in
:mod:`qai.chat.infrastructure.llm_stream` calls them when a terminal
``finish_reason`` is observed.

All notices are wire-format-agnostic plain strings; the caller embeds
them in the existing :class:`StreamFrame.CHUNK` payload schema (per
v2.7 §3.1, no new SSE frame shapes).
"""

from __future__ import annotations


__all__ = [
    "make_truncation_notice",
    "make_content_filter_notice",
]


def make_truncation_notice(
    tool_name: str | None = None,
    language: str = "zh",
) -> str:
    """Build a user-facing notice for ``finish_reason=length``.

    The model emitted a ``finish_reason=length`` terminal chunk, meaning
    the generation hit the per-call ``max_tokens`` ceiling before it
    finished its turn.  When the model was mid-tool-call at the cutoff,
    the partial ``tool_calls`` block has been discarded by the caller
    (incomplete arguments would loop the agent forever).

    Parameters
    ----------
    tool_name:
        Name of the tool whose call was truncated, or ``None`` when the
        truncation hit plain assistant text.  When provided, the notice
        names the tool explicitly so the user knows the side effect was
        cancelled.
    language:
        Locale for the notice.  Currently only ``"zh"`` (default) is
        materially distinct; any other value falls back to an English
        rendering.

    Returns
    -------
    str
        Notice text starting with two newlines + ``⚠️``.  The leading
        whitespace lets the caller append the notice directly to the
        end of the assistant content stream without manual spacing.
    """
    if language == "zh":
        head = "\n\n⚠️ **生成被截断**（已达到单次最大 token 限制）。"
        if tool_name:
            body = f"工具调用 `{tool_name}` 的参数不完整，已取消执行。"
        else:
            body = "本轮回复尚未完整结束。"
        tail = "\n\n请使用 `/compact` 压缩历史记录后重试，或在新会话中重新描述任务。"
        return head + body + tail

    # English fallback.
    head = "\n\n⚠️ **Generation truncated** (max_tokens limit reached). "
    if tool_name:
        body = (
            f"The arguments for tool call `{tool_name}` were incomplete; "
            f"execution was cancelled. "
        )
    else:
        body = "The response did not finish cleanly. "
    tail = (
        "\n\nUse `/compact` to compress conversation history and retry, "
        "or restart the task in a new conversation."
    )
    return head + body + tail


def make_content_filter_notice(
    tool_name: str | None = None,
    language: str = "zh",
) -> str:
    """Build a user-facing notice for ``finish_reason=content_filter``.

    The upstream provider's safety filter blocked the in-flight
    completion.  Recovery is **not** the same as for ``length``: the
    user has to rephrase / drop the offending content; ``/compact`` does
    nothing useful here.

    Parameters
    ----------
    tool_name:
        Name of the tool whose call was being assembled when the filter
        fired, or ``None`` for plain text.  Mostly informational; the
        recovery advice does not change.
    language:
        Locale for the notice (``"zh"`` default; otherwise English).
    """
    if language == "zh":
        head = "\n\n⚠️ **内容被过滤**（生成被服务端的内容安全过滤拦截）。"
        if tool_name:
            body = f"工具调用 `{tool_name}` 的输出未完整返回。"
        else:
            body = "回复未完整返回。"
        tail = "\n\n请尝试调整提示措辞、避开敏感词或换一个表述方式重试。"
        return head + body + tail

    head = (
        "\n\n⚠️ **Content filtered** (response blocked by upstream "
        "content-safety filter). "
    )
    if tool_name:
        body = f"Output for tool call `{tool_name}` was not fully returned. "
    else:
        body = "The response did not finish cleanly. "
    tail = (
        "\n\nPlease rephrase the prompt, remove sensitive terms, or try "
        "a different wording and retry."
    )
    return head + body + tail
