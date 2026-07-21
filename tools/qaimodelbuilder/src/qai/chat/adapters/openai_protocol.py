# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""OpenAI-compatible protocol parsing helpers (PR-401a / S7.5 lane L4).

This module migrates four families of pure functions from the legacy
``backend/chat_handler.py`` (3368 LOC, 2026-05-30 snapshot) into the
chat bounded context's adapter layer:

1. **Tool-call parsing** — :func:`parse_openai_tool_call` and
   :func:`extract_xml_tool_call` consume the two on-the-wire formats the
   chat stream may carry (OpenAI ``delta.tool_calls[]`` or local
   ``<tool_call>{json}</tool_call>`` text) and produce a uniform
   in-process dict.
2. **Streaming buffer slicing** — :func:`split_safe_content` lets a
   streaming adapter emit text chunks promptly without ever yielding a
   half-formed ``<tool_call>`` opener; the caller keeps the held-back
   suffix for the next round.
3. **Tool-name sanitisation** — :func:`sanitize_tool_call_name` and
   :func:`sanitize_messages_tool_call_names` enforce the
   ``[a-zA-Z0-9_-]+`` / ``len <= 64`` constraint that AWS Bedrock /
   Anthropic apply to ``tool_use.name``.  Other providers are more
   lenient; passing the strictest filter is always safe.
4. **Tool message structural cleanup** — :func:`sanitize_tool_messages`
   removes orphan ``role=tool`` messages and ``assistant.tool_calls``
   blocks lacking a downstream ``tool`` response, so OpenAI / Azure
   400s ("messages with role 'tool' must be a response to ...") cannot
   leak from a corrupted history.  A companion deduplicating warning
   helper (:func:`_log_orphan_once`) prevents log noise across repeated
   sanitisation passes within one process.
5. **ANSI escape stripping** — :func:`strip_ansi_escapes` removes
   CSI / OSC sequences from tool stdout/stderr before it is passed to
   the LLM, preserving the legacy behaviour of
   ``backend/text_normalize.py``.

All functions in this module are **pure** (no I/O, no clocks, no
network) and **stateless** apart from a single bounded module-level
cache on :data:`_SANITIZE_WARNED_KEYS` used purely for log
de-duplication.

PR-401a does not wire these into :class:`StreamChatUseCase` — that
happens in PR-401c.  This PR only makes the helpers available inside
the chat BC so subsequent PRs can call them without ever importing
``backend.chat_handler`` (forbidden by v2.7 §3.5 ``no-legacy-deps``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from qai.platform.logging import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool-call delimiters (XML-style, used by GenieAPIService and on-device
# models that emit tool calls inside the assistant text channel).
# ---------------------------------------------------------------------------
TOOL_CALL_OPEN: str = "<tool_call>"
TOOL_CALL_CLOSE: str = "</tool_call>"


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------
def parse_openai_tool_call(tc: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a single OpenAI-format ``delta.tool_calls[i]`` object.

    OpenAI streams tool calls as::

        {"id": "...", "type": "function",
         "function": {"name": "read", "arguments": "{\"path\":\"...\"}"}}

    where ``arguments`` is a (possibly chunked) JSON string.  This helper
    decodes ``arguments`` and returns a stable in-process dict::

        {"type": "tool_call",
         "name":  <str>,
         "arguments": <dict>,
         "id": <str>,
         "thought_signature": <str>}   # optional; preserved when present

    Returns ``None`` for malformed entries (unknown type, missing
    ``function.name``).  Bad JSON in ``arguments`` is logged and
    silently coerced to ``{}`` so that streaming continues.
    """
    if tc.get("type") != "function":
        return None
    func = tc.get("function") or {}
    name = func.get("name", "")
    if not name:
        return None
    raw_args = func.get("arguments", {})
    if isinstance(raw_args, str):
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            _log.warning(
                "openai_protocol.parse_tool_call_bad_json",
                preview=raw_args[:200],
            )
            arguments = {}
    elif isinstance(raw_args, dict):
        arguments = raw_args
    else:
        arguments = {}

    result: dict[str, Any] = {
        "type": "tool_call",
        "name": name,
        "arguments": arguments,
        "id": tc.get("id", ""),
    }
    if tc.get("thought_signature"):
        result["thought_signature"] = tc["thought_signature"]
    return result


def extract_xml_tool_call(text: str) -> dict[str, Any] | None:
    """Extract a complete ``<tool_call>{json}</tool_call>`` block from *text*.

    On-device runtimes such as GenieAPIService emit tool calls inside
    the assistant text channel using XML-style delimiters.  A complete
    block has the form::

        <tool_call>{"name": "read", "arguments": {"path": "./README.md"}}</tool_call>

    Returns ``{"type": "tool_call", "name": ..., "arguments": ...}`` on
    success, or ``None`` if the block is incomplete (still streaming),
    absent, or malformed.

    The parser is forgiving in two ways that match the legacy behaviour:

    * It accepts ``"parameters"`` as an alias for ``"arguments"`` (some
      models trained on a slightly different prompt schema emit the
      former).
    * It unwraps a single layer of nested ``{"name": ..., "arguments":
      ...}`` inside ``arguments`` when the model echoes the outer call
      object by mistake.
    """
    start = text.find(TOOL_CALL_OPEN)
    if start == -1:
        return None
    end = text.find(TOOL_CALL_CLOSE, start)
    if end == -1:
        return None  # incomplete - keep buffering

    inner = text[start + len(TOOL_CALL_OPEN):end].strip()
    try:
        obj = json.loads(inner)
    except json.JSONDecodeError as exc:
        _log.warning(
            "openai_protocol.extract_xml_bad_json",
            error=str(exc),
            preview=inner[:200],
        )
        return None

    if not isinstance(obj, dict):
        return None

    name = obj.get("name", "")
    if not name:
        _log.warning(
            "openai_protocol.extract_xml_missing_name",
            preview=inner[:200],
        )
        return None

    arguments = obj.get("arguments")
    if arguments is None:
        arguments = obj.get("parameters") or {}

    # Tolerance: model may echo the whole {"name":..., "arguments":...}
    # object inside arguments; unwrap one layer.
    if isinstance(arguments, dict) and "name" in arguments:
        inner_args = arguments.get("arguments") or arguments.get("parameters")
        if isinstance(inner_args, dict):
            _log.warning(
                "openai_protocol.extract_xml_nested_call_unwrapped",
                preview=inner[:200],
            )
            arguments = inner_args

    if not isinstance(arguments, dict):
        arguments = {}

    return {"type": "tool_call", "name": name, "arguments": arguments}


# ---------------------------------------------------------------------------
# Streaming buffer slicing
# ---------------------------------------------------------------------------
def split_safe_content(text: str) -> tuple[str, str]:
    """Split *text* into (safe-to-emit prefix, held-back suffix).

    The held-back suffix is the longest tail of *text* that could be
    the **start** of a ``<tool_call>`` opening tag (i.e. a prefix of
    ``<tool_call>``).  Emitting only the safe prefix guarantees no
    half-formed opener leaks into the user-facing stream.

    Examples::

        "hello "             -> ("hello ", "")
        "hello <tool"        -> ("hello ", "<tool")
        "hello <"            -> ("hello ", "<")
        "hello <toolcall>"   -> ("hello <toolcall>", "")
        "<tool_call>..."     -> ("", "<tool_call>...")  # complete tag

    The complete-tag case is handled separately by
    :func:`extract_xml_tool_call`; ``split_safe_content`` is only used
    when the buffer does **not** contain a closing tag yet.
    """
    tag = TOOL_CALL_OPEN
    # Walk backwards through possible prefix lengths of the tag so that
    # the longest possible tail wins.
    for length in range(len(tag), 0, -1):
        if text.endswith(tag[:length]):
            return text[: len(text) - length], text[len(text) - length:]
    return text, ""


# ---------------------------------------------------------------------------
# Tool-name sanitisation (AWS Bedrock / Anthropic constraint).
# ---------------------------------------------------------------------------
TOOL_NAME_MAX_LENGTH: int = 64
_TOOL_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def sanitize_tool_call_name(name: str) -> str:
    """Coerce *name* to satisfy ``[a-zA-Z0-9_-]+`` / ``len <= 64``.

    Transformation:

    1. Replace each invalid character with ``_``.
    2. Collapse consecutive ``_`` into a single ``_``.
    3. Strip leading/trailing ``_``.
    4. Truncate to :data:`TOOL_NAME_MAX_LENGTH` characters.
    5. Fall back to ``"unnamed_tool"`` if the result is empty.

    Examples::

        "PPT.Generation"  -> "PPT_Generation"
        "web search v2"   -> "web_search_v2"
        "namespace:tool"  -> "namespace_tool"
        "搜索"             -> "unnamed_tool"
    """
    if not isinstance(name, str) or not name:
        return "unnamed_tool"
    cleaned = _TOOL_NAME_INVALID_CHARS.sub("_", name)
    cleaned = _REPEATED_UNDERSCORE.sub("_", cleaned)
    cleaned = cleaned.strip("_")
    if len(cleaned) > TOOL_NAME_MAX_LENGTH:
        cleaned = cleaned[:TOOL_NAME_MAX_LENGTH].rstrip("_")
    return cleaned or "unnamed_tool"


def sanitize_messages_tool_call_names(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a copy of *messages* with every ``tool_call.name`` cleaned.

    Cleans:

    * ``assistant.tool_calls[*].function.name``
    * ``tool.name`` (some providers carry a ``name`` field on the
      response message)

    The original message dicts are NOT mutated; a shallow copy is
    produced for any message that needs a rewrite.  ``tool_call_id``
    values (which are always opaque IDs and therefore already
    constraint-compliant) are preserved verbatim.
    """
    if not messages:
        return messages

    result: list[dict[str, Any]] = []
    rename_count = 0

    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue

        new_msg: dict[str, Any] = msg

        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            new_tcs: list[Any] = []
            modified = False
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    new_tcs.append(tc)
                    continue
                func = tc.get("function")
                if isinstance(func, dict) and "name" in func:
                    original = func.get("name", "")
                    cleaned = sanitize_tool_call_name(original)
                    if cleaned != original:
                        new_func = {**func, "name": cleaned}
                        new_tcs.append({**tc, "function": new_func})
                        modified = True
                        rename_count += 1
                        _log.warning(
                            "openai_protocol.tool_call_renamed",
                            original=original[:80],
                            cleaned=cleaned,
                        )
                        continue
                new_tcs.append(tc)
            if modified:
                new_msg = {**msg, "tool_calls": new_tcs}

        if msg.get("role") == "tool" and "name" in msg:
            original = msg.get("name", "")
            if isinstance(original, str):
                cleaned = sanitize_tool_call_name(original)
                if cleaned != original:
                    base = new_msg if new_msg is not msg else msg
                    new_msg = {**base, "name": cleaned}
                    rename_count += 1
                    _log.warning(
                        "openai_protocol.tool_message_renamed",
                        original=original[:80],
                        cleaned=cleaned,
                    )

        result.append(new_msg)

    if rename_count > 0:
        _log.info(
            "openai_protocol.tool_names_sanitised",
            count=rename_count,
            max_length=TOOL_NAME_MAX_LENGTH,
        )
    return result


# ---------------------------------------------------------------------------
# Structural sanitisation of tool messages.
# ---------------------------------------------------------------------------
# Bounded log-deduplication cache: same orphan re-appears every send;
# the cache caps log noise to one warning per unique fingerprint per
# process while keeping the INFO summary on every pass.
_SANITIZE_WARNED_KEYS: set[tuple[Any, ...]] = set()
_SANITIZE_WARN_CAP: int = 256


def _log_orphan_once(
    event: str,
    *,
    idx: int,
    msg: dict[str, Any],
    **extra: Any,
) -> None:
    """Emit one WARNING per unique (idx, role, content_signature).

    Concurrency: this module is only used from a single asyncio event
    loop per process; the GIL plus single-loop execution make the
    unlocked set update safe.  Worst case under hypothetical concurrent
    use is one duplicate warning, never a correctness issue.

    Fingerprint: ``(idx, role, content_signature)``.  ``content_signature``
    falls back to ``hash(repr(content))`` when ``content`` is not a
    string (e.g. OpenAI vision multi-part content), which prevents
    distinct dict-based contents from collapsing to the same prefix.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")
    if isinstance(content, str):
        content_sig: str = content[:64]
    else:
        try:
            content_sig = f"<{type(content).__name__}:{hash(repr(content))}>"
        except Exception:  # pragma: no cover - defensive
            content_sig = f"<{type(content).__name__}>"
    key = (idx, role, content_sig)
    if key in _SANITIZE_WARNED_KEYS:
        _log.debug(event, idx=idx, **extra)
        return
    if len(_SANITIZE_WARNED_KEYS) < _SANITIZE_WARN_CAP:
        _SANITIZE_WARNED_KEYS.add(key)
    _log.warning(event, idx=idx, **extra)


def reset_orphan_warning_cache() -> None:
    """Test helper: clear the dedup cache between cases.

    Production code MUST NOT call this; the cache is process-scoped on
    purpose to suppress log noise from permanently corrupted histories.
    """
    _SANITIZE_WARNED_KEYS.clear()


def sanitize_tool_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop structurally invalid tool messages from *messages*.

    Rules enforced (per OpenAI / Azure tool API):

    1. Every ``role=tool`` message must be preceded (across any
       contiguous run of ``role=tool`` messages) by a ``role=assistant``
       message that carries a ``tool_calls`` list.
    2. Every ``role=assistant`` message that carries ``tool_calls``
       must be followed by at least one ``role=tool`` response.

    Anything that fails either rule is dropped from the returned list.
    The input list is NOT mutated.  Removed messages are reported via
    :func:`_log_orphan_once` (deduplicated) and an aggregate INFO log
    on every pass that removed at least one message.
    """
    if not messages:
        return messages

    n = len(messages)
    keep = [True] * n

    # Pass 1: mark orphan tool messages.
    for i in range(n):
        msg = messages[i]
        role = msg.get("role", "")
        if role != "tool":
            continue

        prev_idx = i - 1
        while prev_idx >= 0 and messages[prev_idx].get("role") == "tool":
            prev_idx -= 1

        if prev_idx < 0:
            keep[i] = False
            _log_orphan_once(
                "openai_protocol.sanitize_orphan_tool_no_predecessor",
                idx=i,
                msg=msg,
            )
            continue

        prev_msg = messages[prev_idx]
        if prev_msg.get("role") != "assistant" or not prev_msg.get("tool_calls"):
            keep[i] = False
            _log_orphan_once(
                "openai_protocol.sanitize_orphan_tool_bad_predecessor",
                idx=i,
                msg=msg,
                prev_role=prev_msg.get("role", ""),
                prev_has_tool_calls=bool(prev_msg.get("tool_calls")),
            )

    # Pass 2: mark assistant.tool_calls without any kept tool response.
    for i in range(n):
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        has_response = False
        for j in range(i + 1, n):
            next_role = messages[j].get("role", "")
            if next_role == "tool":
                if keep[j]:
                    has_response = True
                    break
                continue
            break
        if not has_response:
            keep[i] = False
            _log_orphan_once(
                "openai_protocol.sanitize_assistant_tool_calls_no_response",
                idx=i,
                msg=msg,
            )

    result = [m for idx, m in enumerate(messages) if keep[idx]]
    removed = n - len(result)
    if removed:
        _log.info(
            "openai_protocol.sanitize_tool_messages_removed",
            removed=removed,
            total=n,
        )
    return result


# ---------------------------------------------------------------------------
# ANSI escape stripping (migrated from backend/text_normalize.py).
# ---------------------------------------------------------------------------
# CSI: ESC [ <params> <intermediate> <final 0x40-0x7E>
# OSC: ESC ] <text> (BEL | ESC \\)
# Other 7-bit ESC: ESC <0x40-0x5F>
_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b
    (?:
        \[
        [0-?]*
        [ -/]*
        [@-~]
      |
        \]
        [^\x07\x1b]*
        (?:\x07|\x1b\\)
      |
        [@-_]
    )
    """,
    re.VERBOSE,
)


def strip_ansi_escapes(text: str) -> str:
    """Remove ANSI / VT100 CSI + OSC escape sequences from *text*.

    Many CLI programs (``colorama``, ``rich``, ``tqdm`` ...) emit
    colour escapes even when piped to a child process.  These bytes
    have no semantic value to a downstream LLM and confuse string
    matching ("[INFO]" suddenly becomes "\\x1b[32m[INFO]"), so the
    chat tool layer strips them before forwarding tool output to the
    model.

    Returns the input unchanged if it contains no ESC byte (the common
    case) so the regex pass cost is negligible.  Non-string inputs are
    passed through so callers can defensively wrap any value.
    """
    if not isinstance(text, str):
        return text
    if "\x1b" not in text:
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


__all__ = [
    "TOOL_CALL_OPEN",
    "TOOL_CALL_CLOSE",
    "TOOL_NAME_MAX_LENGTH",
    "parse_openai_tool_call",
    "extract_xml_tool_call",
    "split_safe_content",
    "sanitize_tool_call_name",
    "sanitize_messages_tool_call_names",
    "sanitize_tool_messages",
    "strip_ansi_escapes",
    "reset_orphan_warning_cache",
]
