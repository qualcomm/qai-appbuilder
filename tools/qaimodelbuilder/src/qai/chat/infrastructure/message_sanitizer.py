# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pre-send message sanitisation pipeline (PR-090, S9 C-4 + F-6).

Implements audit items :ref:`C-4` (orphan tool-message + invalid
tool-name 400 errors on OpenAI / Azure / Bedrock) and :ref:`F-6`
(Vertex AI thought-signature flattening) from
``docs/90-refactor/S9-final-parity-audit.md`` §2.1 + §3.1.

This module is the single sanitisation pipeline applied to a chat
``messages`` list immediately before the JSON request body is sent to
an OpenAI-compatible HTTP endpoint by
:class:`qai.chat.infrastructure.llm_stream.HttpOpenAICompatibleLLMStream._build_payload`.
The three pure functions exposed here:

* :func:`sanitize_tool_messages` — drop orphan ``role=tool`` rows /
  abandoned ``assistant.tool_calls`` rows.
* :func:`sanitize_messages_tool_call_names` — coerce ``function.name``
  into the ``[a-zA-Z0-9_-]+`` / ``len <= 64`` shape required by AWS
  Bedrock & Anthropic.
* :func:`flatten_tool_calls_without_signature` — fold Vertex AI
  ``tool_calls`` history without ``thought_signature`` into plain
  assistant text so the next turn does not 400.

Layering note: parallel implementations of these three helpers existed
in :mod:`qai.chat.adapters.openai_protocol` (PR-401a / S7.5 lane L4) at
the *adapter* layer.  The Clean Architecture ``layered-chat`` contract
forbids ``infrastructure -> adapters`` imports; PR-090 therefore hosts
the wiring-canonical copies at the infrastructure layer next to the
HTTP adapter that consumes them.  Both copies share an identical
contract; the older adapter-layer copies remain available for any
non-infrastructure caller (e.g. test fixtures).  All three functions
here are pure (no I/O, copy-on-write).
"""

from __future__ import annotations

import json
import re
from typing import Any

from qai.platform.logging import get_logger


_log = get_logger(__name__)


__all__ = [
    "sanitize_tool_messages",
    "sanitize_messages_tool_call_names",
    "flatten_tool_calls_without_signature",
    "TOOL_NAME_MAX_LENGTH",
]


# ---------------------------------------------------------------------------
# Tool-name sanitisation (AWS Bedrock / Anthropic constraint).
# ---------------------------------------------------------------------------
TOOL_NAME_MAX_LENGTH: int = 64
_TOOL_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def _sanitize_tool_call_name(name: str) -> str:
    """Coerce *name* to satisfy ``[a-zA-Z0-9_-]+`` / ``len <= 64``.

    Transformation:

    1. Replace each invalid character with ``_``.
    2. Collapse consecutive ``_`` into a single ``_``.
    3. Strip leading/trailing ``_``.
    4. Truncate to :data:`TOOL_NAME_MAX_LENGTH` characters.
    5. Fall back to ``"unnamed_tool"`` if the result is empty.
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
    values are preserved verbatim.
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
                    cleaned = _sanitize_tool_call_name(original)
                    if cleaned != original:
                        new_func = {**func, "name": cleaned}
                        new_tcs.append({**tc, "function": new_func})
                        modified = True
                        rename_count += 1
                        _log.warning(
                            "chat.message_sanitizer.tool_call_renamed",
                            original=str(original)[:80],
                            cleaned=cleaned,
                        )
                        continue
                new_tcs.append(tc)
            if modified:
                new_msg = {**msg, "tool_calls": new_tcs}

        if msg.get("role") == "tool" and "name" in msg:
            original = msg.get("name", "")
            if isinstance(original, str):
                cleaned = _sanitize_tool_call_name(original)
                if cleaned != original:
                    base = new_msg if new_msg is not msg else msg
                    new_msg = {**base, "name": cleaned}
                    rename_count += 1
                    _log.warning(
                        "chat.message_sanitizer.tool_message_renamed",
                        original=original[:80],
                        cleaned=cleaned,
                    )

        result.append(new_msg)

    if rename_count > 0:
        _log.info(
            "chat.message_sanitizer.tool_names_sanitised",
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
    """Emit one WARNING per unique (idx, role, content_signature)."""
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
    3. Every individual ``tool_call`` id inside an ``assistant.tool_calls``
       array must have a matching ``role=tool`` reply (``tool_call_id``).
       A *partial orphan* — an assistant with N calls but only M<N answered
       ids — is illegal even though it satisfies rule 2; the unanswered call
       entries are pruned in place. This closes the loophole where a lost
       sub-``agent`` round (``output`` absent → no paired ``role:tool``)
       produced a malformed sequence that the upstream proxy masked as an
       empty HTTP 200 (``empty_response``) on every follow-up turn
       (AGENTS.md 🔴 State-Truth-First / 🟡🟡 "发现缺陷必须修").

    Anything that fails rule 1 or 2 is dropped from the returned list;
    rule-3 violations have the offending call entries pruned (and the
    assistant row then re-evaluated by rule 2). The input list is NOT
    mutated.
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
                "chat.message_sanitizer.orphan_tool_no_predecessor",
                idx=i,
                msg=msg,
            )
            continue

        prev_msg = messages[prev_idx]
        if prev_msg.get("role") != "assistant" or not prev_msg.get("tool_calls"):
            keep[i] = False
            _log_orphan_once(
                "chat.message_sanitizer.orphan_tool_bad_predecessor",
                idx=i,
                msg=msg,
                prev_role=prev_msg.get("role", ""),
                prev_has_tool_calls=bool(prev_msg.get("tool_calls")),
            )

    # Pass 1b: prune individual unanswered tool_call ids (partial orphans).
    # For each assistant.tool_calls, collect the set of tool_call_ids answered
    # by a *kept* role:tool reply in the immediately-following tool run, and
    # drop any call entry whose id is not in that set. Copy-on-write: only the
    # assistant rows that actually need pruning are rebuilt.
    pruned: dict[int, dict[str, Any]] = {}
    for i in range(n):
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            continue
        answered: set[str] = set()
        for j in range(i + 1, n):
            if messages[j].get("role") != "tool":
                break
            if not keep[j]:
                continue
            tcid = messages[j].get("tool_call_id")
            if isinstance(tcid, str) and tcid:
                answered.add(tcid)
        kept_calls = [
            c
            for c in calls
            if isinstance(c, dict) and c.get("id") in answered
        ]
        if len(kept_calls) != len(calls):
            _log_orphan_once(
                "chat.message_sanitizer.assistant_tool_calls_partial_orphan",
                idx=i,
                msg=msg,
                total_calls=len(calls),
                answered_calls=len(kept_calls),
            )
            if kept_calls:
                pruned[i] = {**msg, "tool_calls": kept_calls}
            else:
                # All calls were unanswered → this is a tool-call round with no
                # surviving call. An empty ``tool_calls: []`` is not a valid
                # OpenAI assistant shape, so drop the row (Pass-2 below would
                # skip it since the pruned array is falsy). Its content is the
                # blanked sentinel, so no conversational text is lost.
                keep[i] = False

    # Pass 2: mark assistant.tool_calls without any kept tool response.
    # Evaluate against the pruned view; rows whose calls were all pruned were
    # already dropped (keep[i]=False) above.
    for i in range(n):
        msg = pruned.get(i, messages[i])
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
                "chat.message_sanitizer.assistant_tool_calls_no_response",
                idx=i,
                msg=msg,
            )

    result = [
        pruned.get(idx, m)
        for idx, m in enumerate(messages)
        if keep[idx]
    ]
    removed = n - len(result)
    if removed:
        _log.info(
            "chat.message_sanitizer.tool_messages_removed",
            removed=removed,
            total=n,
        )
    return result


# ---------------------------------------------------------------------------
# Vertex AI thought-signature flattening.
# ---------------------------------------------------------------------------
def flatten_tool_calls_without_signature(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten ``assistant{tool_calls}`` + ``tool`` pairs lacking signatures.

    Vertex AI thinking models require every historical ``tool_calls``
    entry to carry a ``thought_signature``.  Some OpenAI-compatible
    proxies do not pass it through.  When a request would carry such a
    pair, the API returns 400 ("content block is missing a
    thought_signature").  This helper folds the offending pair into a
    single plain-text ``assistant`` message that summarises the call
    and its result, dropping the ``tool_calls`` field entirely.

    Rules:

    * Pairs where every ``tool_calls[i].thought_signature`` is truthy
      pass through unchanged.
    * Otherwise the assistant text plus ``[Tool Call: <name>]`` /
      ``[Tool Result] ...`` blocks are concatenated into a new
      assistant message; the ``tool_calls`` field is removed and the
      following ``role=tool`` messages are absorbed.
    * Tool result content longer than 2000 characters is truncated.
    * Stray ``role=tool`` messages outside any pair are dropped (this
      should not happen because :func:`sanitize_tool_messages` runs
      first).
    """
    if not messages:
        return messages

    result: list[dict[str, Any]] = []
    i = 0
    n = len(messages)

    while i < n:
        msg = messages[i]
        role = msg.get("role", "")

        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]
            all_have_sig = all(
                isinstance(tc, dict) and tc.get("thought_signature")
                for tc in tool_calls
            )
            if all_have_sig:
                # Fully-signed round → pass the assistant{tool_calls} through
                # UNCHANGED *together with its paired ``role:tool`` replies*.
                # Bug fix (父子统一 / Bedrock prefill 400): previously only the
                # assistant message was appended (``i += 1``), so the following
                # ``role:tool`` replies fell through to the stray-tool branch
                # below and were DROPPED — leaving an ``assistant{tool_calls}``
                # with no replies AND making the wire END WITH AN ASSISTANT
                # message. A model that triggered this flatten (e.g. Claude-4
                # thinking via Bedrock, ``thought_signature.required``) then
                # 400'd ("does not support assistant message prefill; the
                # conversation must end with a user message"). Carrying the
                # replies keeps the pair intact (ends with ``role:tool``) and the
                # tool_call↔reply pairing valid.
                result.append(msg)
                i += 1
                while i < n and messages[i].get("role") == "tool":
                    result.append(messages[i])
                    i += 1
                continue

            content_parts: list[str] = []
            original_content = msg.get("content") or ""
            if original_content:
                content_parts.append(str(original_content))

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function") or {}
                tc_name = func.get("name", "unknown")
                tc_args = func.get("arguments", "{}")
                if isinstance(tc_args, str):
                    try:
                        tc_args_obj = json.loads(tc_args) if tc_args else {}
                        tc_args_display = json.dumps(
                            tc_args_obj, ensure_ascii=False, indent=2,
                        )
                    except (json.JSONDecodeError, TypeError):
                        tc_args_display = tc_args
                else:
                    tc_args_display = json.dumps(
                        tc_args, ensure_ascii=False, indent=2,
                    )
                content_parts.append(
                    f"\n[Tool Call: {tc_name}]\nArguments: {tc_args_display}",
                )

            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                tool_msg = messages[j]
                tool_content = tool_msg.get("content") or ""
                tool_content = str(tool_content)
                if len(tool_content) > 2000:
                    tool_content = tool_content[:2000] + "\n... [truncated]"
                content_parts.append(f"\n[Tool Result]\n{tool_content}")
                j += 1

            merged_content = "\n".join(content_parts)
            result.append({
                "role": "assistant",
                "content": merged_content,
            })
            i = j
            continue

        if role == "tool":
            # Stray tool message; drop (shouldn't happen post-sanitize).
            i += 1
            continue

        result.append(msg)
        i += 1

    # P6 — output invariant: the flattened wire MUST NOT end with a bare
    # ``assistant`` message (Anthropic-family upstream rejects assistant
    # prefill with HTTP 400 "does not support assistant message prefill;
    # the conversation must end with a user message"). This used to be
    # enforceable from inside the loop because the only ``assistant``
    # synthesised here was an assistant-without-tool_calls that fold a
    # tool_use+result pair into plain text; if such a fold landed on the
    # tail (no following user / tool message), the wire ended with
    # assistant → prefill 400.
    #
    # Resolution: if the tail is a non-tool_calls ``assistant`` AND the
    # original wire's tail was NOT (i.e. flatten itself moved the tail to
    # assistant), ROLL BACK the entire flatten and return ``messages``
    # unchanged. Rolling back is strictly safer than appending an empty
    # ``user`` placeholder: an empty user changes the conversation
    # semantics ("please continue with no prompt" is undefined behaviour
    # across providers) and may itself trigger validation errors, while
    # the un-flattened wire is at most subject to the original
    # signature-missing 400 (a known, reportable error) instead of a
    # silent semantic shift. The caller can fall back to a non-Vertex
    # provider on signature errors; it cannot recover from corrupted
    # turn semantics.
    if result and result[-1].get("role") == "assistant":
        # An assistant that still carries ``tool_calls`` is benign here
        # (the fully-signed pass-through branch already kept its paired
        # ``role:tool`` replies, so the real tail is the last ``tool``
        # row — a tail ``assistant{tool_calls}`` means there were no
        # replies, which is itself an upstream-rejected shape that the
        # caller's sanitiser should have already removed). The dangerous
        # case is a flatten-synthesised assistant with NO ``tool_calls``
        # — that is what we rollback.
        if not result[-1].get("tool_calls"):
            # Only rollback when flatten ACTUALLY changed the tail role
            # — if the input already ended with a tool_calls-less
            # assistant, flatten is a no-op for the tail and we must not
            # mask that pre-existing condition (it is the caller's job).
            orig_last_role = (
                messages[-1].get("role") if messages else None
            )
            orig_last_has_tc = bool(
                messages and messages[-1].get("tool_calls")
            )
            tail_was_safe = (
                orig_last_role in ("user", "tool", "system")
                or (orig_last_role == "assistant" and orig_last_has_tc)
            )
            if tail_was_safe:
                _log.warning(
                    "chat.message_sanitizer.flatten_rolled_back",
                    reason="flatten_produced_trailing_assistant",
                    orig_tail_role=orig_last_role,
                    flattened_tail_role="assistant",
                )
                return messages

    return result
