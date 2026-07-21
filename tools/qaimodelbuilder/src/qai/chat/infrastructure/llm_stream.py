# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP-backed :class:`LLMStreamPort` (PR-042 + PR-090 S9 hardening).

Speaks to any OpenAI-compatible HTTP endpoint
(``POST {base_url}/chat/completions``) using ``httpx.AsyncClient`` and
the streaming server-sent-event response shape OpenAI ships.

This is the *base* implementation in PR-042's MultiProviderLLMStream
contract: the port name ``HttpOpenAICompatibleLLMStream`` reflects that
PR-042 ships only the OpenAI-compatible flavour.  Local-model and
Anthropic-native flavours are wired in companion adapters.

Adapter behaviour:

* yields one :class:`StreamFrame.CHUNK` per non-empty content delta;
* yields a terminal :class:`StreamFrame.END` (reason="completed") when
  the upstream sends ``data: [DONE]`` or closes the stream cleanly;
* if the upstream returns a non-2xx status or raises, yields a single
  :class:`StreamFrame.ERROR` followed by :class:`StreamFrame.END`
  (reason="failed") and stops.

If ``base_url`` is unconfigured (empty string / None / host omitted),
the adapter degrades to a deterministic offline reply -- a single
``"[no LLM endpoint configured]"`` chunk followed by an END frame.
This keeps integration tests that don't mock the HTTP layer green
without forcing every test to spin up a server.

PR-090 (S9 audit items C-1, C-3..C-5, F-1..F-3, F-6, H-1, H-3 in
``docs/90-refactor/S9-final-parity-audit.md``) hardens this adapter
in seven user-visible ways:

* :ref:`C-1`  capture ``thought_signature`` from streaming tool_call
  deltas so Vertex AI thinking models accept the next turn;
* :ref:`C-4` + :ref:`F-6`  pre-send sanitisation pipeline (orphan tool
  message drop / tool-name slug / Vertex thought-signature flatten)
  applied inside :meth:`_build_payload` via
  :mod:`qai.chat.adapters.message_sanitizer`;
* :ref:`C-5`  per-model parameter clamping/filtering via
  :mod:`qai.chat.adapters.model_param_resolver`;
* :ref:`F-1` + :ref:`F-2`  Chinese tool-name aware truncation /
  content-filter notices via :mod:`qai.chat.adapters.notice_text`;
* :ref:`F-3`  five distinct ``httpx`` exception branches with
  ``retryable: bool`` hints in the error frame;
* :ref:`H-1`  stream-health counters that warn on
  empty-but-200 responses (proxy-masked errors);
* :ref:`H-3`  ``max_tokens`` learning — when the upstream returns
  HTTP 400 mentioning ``max_tokens``, the observed limit is recorded in
  the injected :class:`~qai.chat.application.ports.RuntimeLimitStorePort`
  (D2 dealign) so subsequent requests for the same model auto-clamp.
  The store is a process singleton wired in ``apps/api/_chat_di`` so the
  learned ceilings survive across requests for the process lifetime,
  matching V1's process-global ``_runtime_limits`` dict.  Adapter
  instances that are constructed without a store (unit/integration
  tests) fall back to a per-instance store so the learning still works
  within a single adapter's lifetime — there is no longer any
  module-level mutable global.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import re
import socket
import ssl
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from qai.chat.application.ports import (
    LLMStreamRequest,
    RuntimeLimitStorePort,
)
from qai.chat.domain.error_disposition import retry_disposition_for
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.infrastructure.message_sanitizer import (
    flatten_tool_calls_without_signature,
    sanitize_messages_tool_call_names,
    sanitize_tool_messages,
)
from qai.chat.infrastructure.model_param_resolver import (
    ModelProfile,
    TUNABLE_KEYS,
    profile_from_config_params,
    resolve_params,
)
from qai.chat.infrastructure.notice_text import (
    make_content_filter_notice,
    make_truncation_notice,
)
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger


__all__ = [
    "HttpOpenAICompatibleLLMStream",
    "MultiProviderLLMStream",
]

# Store key under which the runtime-learned ``max_tokens`` ceiling is
# recorded / read.  Matches the legacy ``_runtime_limits[model_id]
# ["max_tokens_max"]`` shape now owned by ``RuntimeLimitStorePort``.
_MAX_TOKENS_LIMIT_KEY = "max_tokens_max"


_log = get_logger(__name__)
# Base HTTP timeout for cloud streaming (connect / write / pool).  Mirrors V1
# ``cloud_shared.timeout_seconds`` default (config_manager.py:453-454 = 120s).
# The SSE *read* timeout is derived from this as ``base * _READ_TIMEOUT_FACTOR``
# (see ``_default_client_factory``) so a slow inter-token gap on a thinking /
# queued / network-jittery cloud turn is not cut off mid-stream.
_DEFAULT_TIMEOUT_SECONDS: float = 120.0
# V1 parity (chat_handler.py:1385 ``httpx.Timeout(timeout, read=timeout * 5)``):
# the per-read timeout governs the MAX gap between two SSE chunks, not the whole
# turn, so it must be far larger than connect/write.  Previously the cloud
# transport used a single scalar 60s for ALL of connect/read/write/pool, which
# truncated long cloud replies into a ``chat.llm.timeout`` (ReadTimeout) error
# and an ``[interrupted]`` bubble.
_READ_TIMEOUT_FACTOR: float = 5.0
_OFFLINE_NOTICE: str = "[no LLM endpoint configured]"

# Application-level "meaningful content" stall ceiling for the SSE read loop.
# httpx's ``read`` timeout only measures the gap between two successful socket
# READS — a half-open upstream / proxy that keeps the TCP connection alive with
# keep-alive bytes (SSE comment lines, periodic empty ``data:`` frames, gateway
# heartbeats) resets that timer on every byte, so a model that has gone silent
# at the APPLICATION layer (no more real tokens) is never detected and the turn
# hangs with a half-streamed reply (the reported "输出一半卡死"). This budget
# instead measures the gap since the last MEANINGFUL chunk (a non-blank text
# delta / tool-call / reasoning delta); when it elapses we abandon the stream
# and surface a retryable timeout, independent of socket-level keep-alive.
#
# TWO-TIER sizing (2026-07-08, measured, NOT guessed):
#   Empirical probes (native + OpenAI-compat, three gateways, several models)
#   showed the stall risk is confined to TOOL-CALL turns. When the model builds
#   a structured tool-call argument (e.g. a long HTML ``write`` body) it can go
#   silent for a LONG time — measured gaps: Claude ~30 s mid-args, and
#   Gemini-3.1-pro a FULL ~46–173 s of zero bytes BEFORE the first tool_call
#   signal even arrives (it buffers the whole tool call and bursts it at once).
#   Crucially that pre-signal silence means we cannot rely on "detect a
#   tool_call delta, then relax" — the longest silence precedes any signal. But
#   we DO know, at request-build time, whether the turn offered ``tools`` at
#   all. Plain-text (no-tools) turns stream smoothly (sub-second gaps) and
#   should be held to a TIGHT budget so a genuinely wedged upstream is caught
#   fast; tool-capable (agentic) turns get a GENEROUS budget to ride out the
#   structuring pause. These are ceilings, not targets — a healthy stream never
#   approaches them.
#
# ``_CONTENT_STALL_TIMEOUT_SECONDS`` is kept as the tool-turn (generous) value
# AND as the name existing tests monkeypatch; ``_CONTENT_STALL_TEXT_TURN_SECONDS``
# is the tight budget applied only when the request offered no tools.
_CONTENT_STALL_TIMEOUT_SECONDS: float = 600.0
_CONTENT_STALL_TEXT_TURN_SECONDS: float = 60.0

# tool-call "generating arguments" progress throttle (V2 UX enhancement).
# Emit a progress frame only when BOTH enough time has passed since the last
# one AND the accumulated argument has grown by at least this many chars — so a
# long argument produces ~10-25 frames, not thousands.  The first frame for a
# given tool index (name just resolved) is always emitted immediately so the
# card appears as early as possible.
_ARGS_PROGRESS_MIN_INTERVAL_S: float = 0.25
_ARGS_PROGRESS_MIN_GROWTH: int = 200

# Tool-call index offset for Anthropic list-form ``delta.content`` blocks
# (see :func:`_accumulate_tool_calls`). Position-derived keys for tool_use
# blocks land at ``_ANTHROPIC_INDEX_BASE + pos`` so they never collide with
# OpenAI ``delta.tool_calls[i].index`` values a mixed proxy might also
# emit in the same stream. Large enough that OpenAI tool_calls (indices
# 0, 1, 2, …) and Anthropic tool_use blocks (1_000_000+) sort cleanly in
# the consumer's ``sorted(accumulated_tool_calls)`` iteration order.
_ANTHROPIC_INDEX_BASE: int = 1_000_000

# Chat-context orchestration metadata carried on ``LLMStreamRequest.extra``.
# These keys are consumed by the use case / system-prompt builder (or popped
# explicitly in ``_build_payload``) and are NOT valid OpenAI request-body
# fields, so the generic "forward remaining extra keys" pass must skip them
# to keep them off the wire.  ``system_prompt`` / ``messages`` are popped
# earlier; the rest are listed here defensively.
_CHAT_CONTROL_KEYS: frozenset[str] = frozenset(
    {
        "system_prompt",
        "system_prompt_suffix",
        "tool_mode",
        "tool_params",
        "latest_user_message",
        "persona",
        "persona_name",
        "memory_context",
        "skill_content",
        "tools_xml",
        "tools_schemas",
        "skill_catalog",
        "app_builder_skill_files",
        "app_builder_pack_catalog",
        # Multi-model inference-code refs for the selected App Builder model(s)
        # (tuple of the ``AppBuilderModelCode`` DTO). Consumed ONLY by the
        # system-prompt builder (``_render_app_builder_model_code``); it is not
        # a valid OpenAI body field AND is a non-JSON-serializable dataclass, so
        # it MUST be filtered here — otherwise it leaks onto the wire payload and
        # crashes request serialization ("Object of type AppBuilderModelCode is
        # not JSON serializable"). Mirrors its two siblings above.
        "app_builder_model_code",
        "_effective_tool_mode",
        # Per-conversation workspace override + auto-injected project-context
        # files (AGENTS.md / CLAUDE.md). Consumed by the use case /
        # system-prompt builder only; their content is already inlined into
        # the system prompt, so forwarding them as top-level body fields is
        # pure noise (and ``workspace_context_files`` can be tens of KB).
        "_session_workspace_root",
        "workspace_context_files",
        # Per-turn runtime debug flags stashed by ``StreamChatUseCase._run``
        # from the forge-config ``service_launch`` reader (prompt_debug /
        # show_prompt_in_ui). Consumed by the use case only (console dump +
        # snapshot gate); never valid OpenAI body fields.
        "_prompt_debug",
        "_show_prompt_in_ui",
    }
)


# ---------------------------------------------------------------------------
# H-3 — runtime ``max_tokens`` learning (D2 dealign).
# ---------------------------------------------------------------------------
# Some providers expose model-specific ``max_tokens`` ceilings only via the
# 400 response body ("max_tokens above maximum value, expected <= N").  We
# extract N once and clamp subsequent requests for the same ``model_id`` to
# at most that observed value, eliminating repeated user-visible failures.
#
# The *state* lives behind :class:`RuntimeLimitStorePort` (D2): the process
# singleton ``InMemoryRuntimeLimitStore`` is injected via ``apps/api/_chat_di``
# so learned ceilings survive across requests for the process lifetime,
# matching V1's process-global ``_runtime_limits`` dict.  There is no longer
# a module-level mutable global here.  When an adapter is constructed without
# a store (unit / integration tests), :class:`_PerInstanceRuntimeLimitStore`
# below provides an equivalent per-instance cache so the learning behaviour is
# preserved within a single adapter's lifetime.


class _PerInstanceRuntimeLimitStore:
    """Per-adapter-instance fallback :class:`RuntimeLimitStorePort`.

    Used only when an :class:`HttpOpenAICompatibleLLMStream` is built
    without an injected store (tests / minimal wiring).  Holds the same
    ``{model_id: {key: int}}`` map the production
    :class:`qai.chat.adapters.InMemoryRuntimeLimitStore` keeps, but scoped
    to one adapter instance instead of the process — which is exactly the
    pre-D2 module-global behaviour minus the *global* (each test gets a
    fresh adapter, so there is no cross-test bleed).  Infrastructure must
    not import the adapters layer (``layered-chat`` contract), so this thin
    duplicate of the port shape lives here rather than reusing the adapter.
    """

    __slots__ = ("_limits",)

    def __init__(self) -> None:
        self._limits: dict[str, dict[str, int]] = {}

    def record_limit(self, *, model_id: str, max_tokens_max: int) -> None:
        if not model_id or max_tokens_max <= 0:
            return
        self._limits.setdefault(model_id, {})["max_tokens_max"] = max_tokens_max

    def get_limit(self, *, model_id: str, key: str) -> int | None:
        return self._limits.get(model_id, {}).get(key)

    def clear(self, *, model_id: str | None = None) -> None:
        if model_id is None:
            self._limits.clear()
        else:
            self._limits.pop(model_id, None)


# Regex to extract the upper bound from common 400-error phrasings.
# Examples we want to capture:
#   "max_tokens above maximum value, expected <= 131072"
#   "max output tokens cannot exceed 32768"
#   "max_tokens must be <= 4096"
#   "max_tokens (value 100000) exceeds the maximum supported value of 8192"
_MAX_TOKENS_LIMIT_RE = re.compile(
    r"(?:expected\s*<=?|<=?|cannot\s+exceed|maximum\s+(?:supported\s+)?"
    r"value\s+of|less\s+than\s+or\s+equal\s+to|max(?:imum)?:?)\s*(\d{2,7})",
    re.IGNORECASE,
)


_ANTHROPIC_MODEL_MARKERS = ("claude", "anthropic", "sonnet", "opus", "haiku")

# Aged tool-result placeholder — the SAME wire-format sentinel the application
# aging transform writes (``qai.chat.application.use_cases._agentic_kernel
# .AGED_TOOL_OUTPUT_PLACEHOLDER``). Imported (not re-literalled) so the two
# never drift: the adapter re-derives the Anthropic cache anchor by locating
# these frozen placeholders in the final wire (改动2b). Infrastructure →
# application is the allowed layered direction (this module already imports
# ``qai.chat.application.ports``).
from qai.chat.application.use_cases._agentic_kernel import (  # noqa: E402
    AGED_TOOL_OUTPUT_PLACEHOLDER as _AGED_TOOL_OUTPUT_PLACEHOLDER,
)

# Minimum token count Anthropic requires before a cache_control breakpoint.
# Breakpoints on shorter prefixes are silently ignored by the gateway but still
# consume one of the 4-breakpoint budget — so we skip them to avoid wasting
# the budget. Value: 1024 tokens (Anthropic documented minimum).
_ANTHROPIC_MIN_CACHE_TOKENS: int = 1024


def _estimate_prefix_tokens(items: list) -> int:
    """BPE-free chars/4 token estimate for a list of wire messages or tool dicts.

    Mirrors ``_maybe_estimate_usage``'s chars//4 heuristic. Used only as a
    guard against wasting Anthropic cache_control breakpoints on prefixes
    shorter than the 1024-token minimum (方案C).
    """
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        # Tool schema: count description + parameters JSON
        fn = item.get("function")
        if isinstance(fn, dict):
            total += len(str(fn.get("description") or ""))
            total += len(str(fn.get("parameters") or ""))
        # Message: count content + tool_calls arguments
        c = item.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    total += len(str(block.get("text") or ""))
        for tc in item.get("tool_calls") or ():
            if isinstance(tc, dict):
                f = tc.get("function")
                if isinstance(f, dict):
                    total += len(str(f.get("arguments") or ""))
    return total // 4


def _is_anthropic_family(model_id: str | None) -> bool:
    """True when the target model speaks the Anthropic Messages API (prompt
    caching via ``cache_control`` is supported). Local GenieAPIService and
    non-Anthropic cloud providers return False so their payload is untouched."""
    if not model_id:
        return False
    lower = model_id.lower()
    return any(marker in lower for marker in _ANTHROPIC_MODEL_MARKERS)


def _apply_anthropic_prompt_caching(
    payload: dict, *, stable_aged_prefix: int | None = None
) -> None:
    """Mark the stable prefix (tools + system + last non-system message) with
    Anthropic ``cache_control: ephemeral`` breakpoints so round 2+ bills the
    prefix as cache_read (~10% price). Anthropic allows at most 4 breakpoints;
    we spend them tools-first (most stable), then system, then the last
    non-system message.

    Transport note: this adapter targets the OpenAI-compatible
    ``/v1/chat/completions`` envelope (``model`` / ``messages`` /
    ``stream_options.include_usage``; system injected as a ``role:system``
    message; tools via ``payload["tools"]``). The Anthropic-bridging gateway
    used for Claude (``cloud LLM service`` / Bedrock-OpenAI) forwards the upstream's
    typed content blocks AND already reports Anthropic prompt-cache usage back
    (``prompt_tokens_details.cache_write_tokens`` / ``cache_read_tokens`` — see
    :func:`_extract_usage`), i.e. the OpenRouter-style convention where a
    ``cache_control`` marker on a message/tool content block passes through to
    the Anthropic upstream. We therefore attach the marker on content blocks
    inside the OpenAI envelope (never as a top-level Anthropic ``system``
    array, which this envelope does not use).

    ``stable_aged_prefix`` (optional, DEFAULT ``None``): the COUNT of the
    OLDEST ``role:tool`` messages that the ``StreamChatUseCase`` has FROZEN
    into ``[Old tool result content cleared]`` placeholders via its monotonic
    aging boundary (改动2b). Breakpoint (c) — normally the LAST ``str``
    user/assistant message — is the one that keeps MISSING the cache: the
    sliding aging window re-cuts mid-history every round so a prefix ENDING at
    "the last message" drifts byte-wise and Anthropic (which requires a
    byte-identical prefix up to the breakpoint) never hits it. When this count
    is supplied and > 0 we RE-DERIVE the anchor against the FINAL wire (this
    is deliberate — the count survives the intervening system-prompt insert /
    orphan-tool repair / sanitisation that would invalidate a raw index): we
    locate the ``stable_aged_prefix``-th oldest aged-placeholder ``role:tool``
    message, then place breakpoint (c) on the last eligible ``str``
    user/assistant message AT OR BEFORE it — i.e. inside the byte-stable frozen
    region — so its prefix (tools + system + frozen aged region) is identical
    round over round and Anthropic bills it as ``cache_read``. When ``None`` /
    ``0`` (the DEFAULT — no monotonic boundary, e.g. sub-agent / legacy /
    first rounds before any aging) the scan falls back to the prior "last
    ``str`` message" behaviour, keeping the wire byte-for-byte identical for
    callers that do not opt in.

    In-place mutation. Caller guarantees the target is Anthropic-family. Content
    that is not a plain ``str`` (already a multimodal / typed block list) is
    left untouched so we neither double-wrap nor corrupt existing structure.
    """
    breakpoints_left = 4

    # 1) tools array — most stable, cache first. Mark the LAST tool (the
    #    breakpoint covers all preceding tools too).
    tools = payload.get("tools")
    if isinstance(tools, list) and tools and breakpoints_left > 0:
        last = tools[-1]
        if isinstance(last, dict):
            # 方案C: skip if the tools prefix is below the 1024-token minimum
            # (silently ignored by Anthropic but wastes a breakpoint slot).
            if _estimate_prefix_tokens(tools) >= _ANTHROPIC_MIN_CACHE_TOKENS:
                tools[-1] = {**last, "cache_control": {"type": "ephemeral"}}
                breakpoints_left -= 1

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    # 2) first system message — convert str content to a single text block
    #    carrying the cache_control marker.
    if breakpoints_left > 0:
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    # 方案C: estimate prefix = tools + this system message.
                    _sys_prefix = (list(tools) if isinstance(tools, list) else []) + [msg]
                    if _estimate_prefix_tokens(_sys_prefix) >= _ANTHROPIC_MIN_CACHE_TOKENS:
                        msg["content"] = [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }]
                        breakpoints_left -= 1
                break

    # 3) most-recent user/assistant message whose content is a plain ``str`` —
    #    everything up to that point becomes cacheable. We scan from the end and
    #    SKIP over messages we cannot safely mark, continuing until we find an
    #    eligible one:
    #    * ``role:tool`` — SKIPPED: in the OpenAI-compatible envelope a tool
    #      message's ``content`` must stay a string; rewriting it to a typed
    #      block array risks a 400 from stricter gateways.
    #    * ``assistant`` tool-call turns carry ``content=None`` (not str) — also
    #      skipped, so we keep scanning to the preceding user/assistant text
    #      turn rather than wasting the breakpoint on a non-markable message.
    #    This keeps the breakpoint useful in the common agentic tail
    #    (…user, assistant{tool_calls}, tool) by landing it on the user turn.
    #
    #    When ``stable_aged_prefix`` (> 0) is supplied we RE-DERIVE the anchor
    #    against THIS final wire: find the Nth oldest aged-placeholder
    #    ``role:tool`` message (N = stable_aged_prefix) and start the reverse
    #    scan there, so the breakpoint lands on the last eligible ``str``
    #    message WITHIN the byte-stable frozen region → the cache actually hits.
    #    ``None`` / 0 → start at the end (prior behaviour, byte-for-byte
    #    unchanged).
    if breakpoints_left > 0:
        _start = len(messages) - 1
        if stable_aged_prefix is not None and stable_aged_prefix > 0:
            # Walk oldest→newest counting aged-placeholder tool messages; the
            # Nth one's index is the top of the byte-stable frozen region.
            _aged_seen = 0
            _anchor: int | None = None
            for _i, _m in enumerate(messages):
                if not isinstance(_m, dict) or _m.get("role") != "tool":
                    continue
                _c = _m.get("content")
                if isinstance(_c, str) and _c == _AGED_TOOL_OUTPUT_PLACEHOLDER:
                    _aged_seen += 1
                    if _aged_seen >= stable_aged_prefix:
                        _anchor = _i
                        break
            if _anchor is not None:
                _start = _anchor
        for idx in range(_start, -1, -1):
            msg = messages[idx]
            if not isinstance(msg, dict):
                continue
            if msg.get("role") in ("system", "tool"):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                # 方案C: estimate prefix = tools + messages[0..idx].
                # Skip (don't mark) if below the 1024-token minimum, but
                # always break — scanning further back only finds shorter
                # prefixes, so there is no point continuing.
                _pfx_tools = list(tools) if isinstance(tools, list) else []
                _pfx_msgs = messages[: idx + 1]
                if _estimate_prefix_tokens(_pfx_tools + _pfx_msgs) >= _ANTHROPIC_MIN_CACHE_TOKENS:
                    msg["content"] = [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }]
                    breakpoints_left -= 1
                break
            # Non-str user/assistant (e.g. assistant tool_call with content=None,
            # or an already-typed block list) → keep scanning for an eligible one.


def _normalize_history_message(msg: Any) -> dict[str, Any]:
    """Coerce a history element into an OpenAI ``{"role","content"}`` dict.

    ``LLMStreamRequest.history`` is normally a tuple of domain
    :class:`~qai.chat.domain.message.Message` objects (``msg.role.value`` /
    ``msg.content.text``).  But the agentic loop's context compressor
    (:meth:`StreamChatUseCase._compress_history`) returns **plain dicts**
    and threads them back through ``history`` on a compressed round, so
    this builder must accept both shapes.  Previously it assumed the
    ``Message`` shape unconditionally and crashed the entire SSE turn with
    ``AttributeError: 'dict' object has no attribute 'role'`` the moment a
    compression ran mid tool-loop — the model-build "runs a few tool
    rounds then silently stops" regression.

    * ``Message`` object → ``{"role": msg.role.value, "content":
      msg.content.text}``.
    * ``dict`` (compressed) → passed through, defaulting ``role`` to
      ``"user"`` and coercing a missing / non-str ``content`` to ``""``.
    * anything else → ``{"role": "user", "content": str(msg)}`` (defensive).
    """
    # Already a wire dict (compressed history / pre-assembled message).
    if isinstance(msg, dict):
        role = msg.get("role")
        content = msg.get("content")
        out: dict[str, Any] = {
            "role": role if isinstance(role, str) and role else "user",
            "content": content if isinstance(content, str) else (
                "" if content is None else content
            ),
        }
        # Preserve tool-call linkage fields if the dict carried them so a
        # compressed ``assistant{tool_calls}`` / ``tool`` block stays valid.
        for k in ("tool_calls", "tool_call_id", "name"):
            if k in msg:
                out[k] = msg[k]
        return out
    # Domain Message object.
    role_obj = getattr(msg, "role", None)
    role_val = getattr(role_obj, "value", None)
    content_obj = getattr(msg, "content", None)
    text_val = getattr(content_obj, "text", None)
    out: dict[str, Any] = {
        "role": role_val if isinstance(role_val, str) and role_val else "user",
        "content": text_val if isinstance(text_val, str) else "",
    }
    # Preserve tool-call linkage carried on the domain ``Message`` itself
    # (``message.py:50-51`` — ``tool_calls`` / ``tool_results`` tuples). The
    # earlier version dropped both, so a history threaded as domain
    # ``Message`` objects through ``LLMStreamRequest.history`` (e.g. a
    # sub-agent loop) lost the ``assistant{tool_calls}`` → ``tool{tool_call_id}``
    # pairing; round ≥2 then sent an orphan ``role:tool`` that
    # :func:`sanitize_tool_messages` deletes, stranding the model.  Forward
    # the linkage ONLY when it is already OpenAI-shaped (so we never fabricate
    # a malformed block): ``tool_calls`` entries must carry ``id`` +
    # ``function``; a ``role:tool`` message's ``tool_call_id`` is lifted from
    # its first ``tool_results`` entry that records one.
    tool_calls = getattr(msg, "tool_calls", None)
    if isinstance(tool_calls, (list, tuple)) and tool_calls:
        wire_calls = [
            dict(tc)
            for tc in tool_calls
            if isinstance(tc, dict) and tc.get("id") and tc.get("function")
        ]
        if wire_calls:
            out["tool_calls"] = wire_calls
    if out["role"] == "tool":
        tool_results = getattr(msg, "tool_results", None)
        if isinstance(tool_results, (list, tuple)):
            for tr in tool_results:
                if isinstance(tr, dict) and isinstance(
                    tr.get("tool_call_id"), str
                ):
                    out["tool_call_id"] = tr["tool_call_id"]
                    name = tr.get("tool_name")
                    if isinstance(name, str) and name:
                        out["name"] = name
                    break
    return out


def _extract_max_tokens_limit(body: str) -> int | None:
    """Best-effort extraction of an integer ``max_tokens`` ceiling from a 400 body.

    Returns ``None`` when no plausible integer can be matched.
    """
    if not body:
        return None
    m = _MAX_TOKENS_LIMIT_RE.search(body)
    if not m:
        return None
    try:
        candidate = int(m.group(1))
    except (ValueError, TypeError):
        return None
    # Sanity bounds; reject obviously bogus tiny / huge values.
    if candidate < 16 or candidate > 10_000_000:
        return None
    return candidate


def _looks_like_max_tokens_error(body: str) -> bool:
    """Heuristic: does *body* describe a ``max_tokens`` parameter rejection?"""
    if not body:
        return False
    lower = body.lower()
    return "max_tokens" in lower or "max output tokens" in lower or (
        "max output_tokens" in lower
    )


#: Sampling parameters the user can declare as supported/unsupported in
#: Settings → Cloud Models.  Used to map an "unsupported parameter" 400 from
#: the upstream back to the exact param so the error notice can name it and
#: point the user at the right toggle.
_UNSUPPORTED_PARAM_NAMES: tuple[str, ...] = (
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "stop",
)

#: Phrases that indicate the upstream rejected a *parameter* (rather than the
#: prompt length / content / auth).  Combined with a param name match below.
_UNSUPPORTED_PARAM_PHRASES: tuple[str, ...] = (
    "unsupported",
    "not supported",
    "does not support",
    "unrecognized",
    "unknown parameter",
    "unexpected parameter",
    "not allowed",
    "is not permitted",
    "cannot be used",
    "only the default",
    "unsupported value",
    "invalid parameter",
    "extra inputs are not permitted",
)


def _detect_unsupported_param(body: str) -> str | None:
    """Return the sampling-param name an "unsupported parameter" 400 names.

    Heuristic: the upstream 400 body mentions one of the configurable
    sampling parameters (``temperature`` / ``top_p`` / ...) together with a
    phrase indicating the parameter itself is rejected (not the prompt
    length / content filter / auth).  Returns the param name on a match so
    the chat error notice can tell the user exactly which param to turn off
    in Settings → Cloud Models; ``None`` otherwise.

    Guards against false positives:

    * ``max_tokens`` is intentionally NOT a configurable-via-this-path param —
      its rejections are handled by the dedicated max_tokens self-healing
      learning path, so it is excluded here.
    * A body that looks like a prompt-too-long / context-window / throttling
      error is excluded (those have their own, more specific handling); only
      a genuine parameter rejection should surface the "go fix the param"
      guidance.
    """
    if not body:
        return None
    lower = body[:2048].lower()
    # Exclude errors that are really about prompt length / throttling — those
    # must keep their own dedicated codes, never the unsupported-param one.
    if any(kw in lower for kw in _PTL_KEYWORDS):
        return None
    if any(kw in lower for kw in _THROTTLE_KEYWORDS):
        return None
    if "content_filter" in lower or "content_policy" in lower:
        return None
    if not any(phrase in lower for phrase in _UNSUPPORTED_PARAM_PHRASES):
        return None
    for name in _UNSUPPORTED_PARAM_NAMES:
        if name in lower:
            return name
    return None


# ---------------------------------------------------------------------------
# P2-b: inline HTTP error classification (V1 api_error_classifier.py parity)
# ---------------------------------------------------------------------------
# Prompt-too-long keywords (shared with adapters/error_classifier.py but
# inlined here to avoid a cross-layer import).
_PTL_KEYWORDS: tuple[str, ...] = (
    "prompt is too long", "context_length_exceeded", "maximum context length",
    "tokens > ", "input is too long", "context window", "token limit",
    "too many tokens", "request too large", "reduce the length",
    "maximum allowed", "exceed max message tokens", "exceed max input tokens",
)
_PTL_EXCLUSIONS: tuple[str, ...] = ("above maximum value",)

_THROTTLE_KEYWORDS: tuple[str, ...] = (
    "throttlingexception", "rate_limit_exceeded", "too many requests",
    "quota exceeded", "rate limit", "serveroverloaded", "toomanyrequests",
    "service unavailable", "overloaded", "server overload", " 429", "(429)",
)


def _classify_http_error(
    *, status_code: int, body_text: str
) -> tuple[str, bool]:
    """Classify an HTTP error into (code, retryable) using status + keywords.

    V1 parity (api_error_classifier.py): structured classification using
    HTTP status code + keyword fallback.  Returns a tuple of
    ``(error_code_for_frame, retryable_bool)``.
    """
    msg_lower = body_text[:1024].lower()

    # 429: throttling
    if status_code == 429:
        return ("throttling", True)

    # 401: authentication failed (bad / missing API key).
    if status_code == 401:
        return ("chat.llm.auth_failed", False)

    # 403: authenticated but not authorized (key valid, access denied) —
    # distinct from 401 so the frontend can show the right actionable bubble.
    if status_code == 403:
        return ("chat.llm.permission_denied", False)

    # 404: model or endpoint not found (status-only; NO body-parsing /
    # provider-adapter — out of scope). The MESSAGE is built by the caller.
    if status_code == 404:
        return ("chat.llm.model_unavailable", False)

    # 503: server overload → throttling
    if status_code == 503:
        return ("throttling", True)

    # 500/502/504: server error
    if status_code >= 500:
        return ("chat.llm.server_error", True)

    # 400: inspect body for prompt_too_long or content_filter
    if status_code == 400:
        if not any(ex in msg_lower for ex in _PTL_EXCLUSIONS):
            if any(kw in msg_lower for kw in _PTL_KEYWORDS):
                return ("prompt_too_long", False)
        if "content_filter" in msg_lower or "content_policy" in msg_lower:
            return ("chat.llm.content_filtered", False)

    # Keyword fallback for any status
    if not any(ex in msg_lower for ex in _PTL_EXCLUSIONS):
        if any(kw in msg_lower for kw in _PTL_KEYWORDS):
            return ("prompt_too_long", False)
    if any(kw in msg_lower for kw in _THROTTLE_KEYWORDS):
        return ("throttling", True)

    return ("chat.llm.http_error", status_code in (429, 502, 503, 504))


# ── ConnectError sub-classification ──────────────────────────────────────
# A single ``httpx.ConnectError`` previously collapsed SSL-cert failure, DNS
# failure and connection-refused into ONE ``chat.llm.connect_error`` code →
# RetryCategory.NETWORK → infinite retry (bug). We now walk the exception
# chain and inspect the EXCEPTION TYPE + errno FIRST (deterministic), with a
# string-match fallback (SSL reason strings vary across OpenSSL builds but are
# stable enough as a secondary signal), to emit a precise code so the retry
# policy and the frontend can treat each cause correctly.
#
# TLS reason substrings (lowercased). Types are checked first; these classify
# WITHIN ``ssl.SSLCertVerificationError`` / ``ssl.SSLError``.
_TLS_UNTRUSTED_SUBSTRINGS: tuple[str, ...] = (
    "certificate verify failed",
    "self-signed",
    "self signed",
    "unable to get local issuer",
    "unable to get issuer",
)
_TLS_HOSTNAME_SUBSTRINGS: tuple[str, ...] = (
    "hostname mismatch",
    "doesn't match",
    "does not match",
    "ip address mismatch",
)
_TLS_EXPIRED_SUBSTRINGS: tuple[str, ...] = (
    "certificate has expired",
    "certificate is not yet valid",
)

# Winsock / POSIX errno values we recognise by number (checked after type).
_ECONNREFUSED_CODES: frozenset[int] = frozenset(
    {errno.ECONNREFUSED, 10061},  # 10061 = WSAECONNREFUSED
)
_HOST_UNREACHABLE_CODES: frozenset[int] = frozenset(
    {
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        10065,  # WSAEHOSTUNREACH
        10051,  # WSAENETUNREACH
    }
)


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` then its ``__cause__`` / ``__context__`` chain.

    Walks the causal chain breadth-first with a ``seen`` guard so a
    self-referential ``__context__`` cannot loop forever. httpx wraps the
    underlying ``ssl.SSLError`` / ``socket.gaierror`` / ``OSError`` as the
    cause of its ``ConnectError``, so the real signal lives deeper than the
    top exception.
    """
    seen: set[int] = set()
    queue: list[BaseException | None] = [exc]
    while queue:
        cur = queue.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        queue.append(cur.__cause__)
        queue.append(cur.__context__)


def _errno_of(exc: BaseException) -> int | None:
    """Best-effort extract an OS errno from an exception."""
    for attr in ("errno", "winerror"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


def _classify_connect_error(exc: BaseException) -> tuple[str, bool]:
    """Sub-classify an ``httpx.ConnectError`` into ``(code, retryable)``.

    Inspects the FULL exception chain (``exc`` + ``__cause__`` +
    ``__context__``), preferring EXCEPTION TYPE + errno over string matching.
    Returns the most specific ``(error_code, retryable)`` found; falls back to
    the generic ``chat.llm.connect_error`` (network, budget-capped) when no
    specific cause is recognised.

    Precedence (most-specific first): TLS cert-verify problems → other SSL →
    DNS → connection refused → host/net unreachable → generic connect error.
    """
    chain = list(_iter_exception_chain(exc))

    # 1. TLS: certificate verification / handshake failures (never retry —
    #    deterministic trust problem the user must resolve).
    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError):
            reason = f"{getattr(e, 'verify_message', '') or ''} {e}".lower()
            if any(s in reason for s in _TLS_HOSTNAME_SUBSTRINGS):
                return ("chat.llm.tls_hostname_mismatch", False)
            if any(s in reason for s in _TLS_EXPIRED_SUBSTRINGS):
                return ("chat.llm.tls_cert_expired", False)
            # Default cert-verify failure = untrusted issuer / self-signed.
            return ("chat.llm.tls_cert_untrusted", False)
    for e in chain:
        if isinstance(e, ssl.SSLError):
            reason = str(e).lower()
            # A plain SSLError may still carry a verify/expiry reason string
            # (older builds raise SSLError, not the subclass) — honour it.
            if any(s in reason for s in _TLS_HOSTNAME_SUBSTRINGS):
                return ("chat.llm.tls_hostname_mismatch", False)
            if any(s in reason for s in _TLS_EXPIRED_SUBSTRINGS):
                return ("chat.llm.tls_cert_expired", False)
            if any(s in reason for s in _TLS_UNTRUSTED_SUBSTRINGS):
                return ("chat.llm.tls_cert_untrusted", False)
            return ("chat.llm.tls_handshake_failed", False)

    # 2. DNS resolution failure (bounded-fast: transient blip or wrong host).
    for e in chain:
        if isinstance(e, socket.gaierror):
            return ("chat.llm.dns_error", True)

    # 3. Connection refused (bounded-fast).
    for e in chain:
        if isinstance(e, ConnectionRefusedError):
            return ("chat.llm.connection_refused", True)
        code = _errno_of(e)
        if code is not None and code in _ECONNREFUSED_CODES:
            return ("chat.llm.connection_refused", True)

    # 4. Host / network unreachable (bounded-fast).
    for e in chain:
        code = _errno_of(e)
        if code is not None and code in _HOST_UNREACHABLE_CODES:
            return ("chat.llm.host_unreachable", True)

    # 5. Fallback: generic transient connect error (network, budget-capped).
    return ("chat.llm.connect_error", True)


# Rate-limit aware backoff: hard ceiling (5 minutes) on any server-advised
# ``Retry-After`` delay we will honour. Mirrors
# ``qai.chat.adapters.error_classifier.RETRY_AFTER_MAX_SECONDS`` — deliberately
# INLINED here (not imported) because ``llm_stream`` is *infrastructure* and
# ``error_classifier`` is *adapters*; the import-linter ``layered-chat``
# contract forbids infrastructure → adapters. This adapter already inlines
# other keyword sets (``_THROTTLE_KEYWORDS`` etc.) for the same reason.
_RETRY_AFTER_MAX_SECONDS: float = 300.0


def _parse_retry_after_header(
    value: str | None,
    *,
    now: datetime | None = None,
    max_seconds: float = _RETRY_AFTER_MAX_SECONDS,
) -> float | None:
    """Parse a ``Retry-After`` header into a clamped non-negative delay (s).

    Accepts the RFC 7231 ``delay-seconds`` integer form OR an ``HTTP-date``.
    Returns ``None`` for absent / malformed / already-expired values so the
    retry policy keeps its existing exponential backoff (unchanged behaviour).

    INLINE mirror of ``error_classifier.parse_retry_after`` (see the
    ``_RETRY_AFTER_MAX_SECONDS`` note above for why it is duplicated rather
    than imported across the adapters/infrastructure layer boundary).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return min(float(int(text)), max_seconds)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    delta = (parsed - current).total_seconds()
    if delta <= 0.0:
        return None
    return min(delta, max_seconds)


class HttpOpenAICompatibleLLMStream:
    """Stream chat completions from an OpenAI-compatible HTTP endpoint.

    Construction parameters:

    * ``base_url``: base URL up to and including ``/v1`` (e.g.
      ``"https://api.openai.com/v1"``). Empty / None disables the
      adapter (falls back to offline mode).
    * ``api_key``: bearer token sent in the ``Authorization`` header.
    * ``model``: default model id used when
      :class:`LLMStreamRequest.model_hint` is unset.
    * ``ids``: id generator used to mint frame ids.
    * ``client_factory``: callable returning an :class:`httpx.AsyncClient`
      (allows tests to inject ``respx`` mocks). Default builds a fresh
      client per stream call.
    * ``timeout_seconds``: base HTTP timeout (connect / write / pool) for the
      stream.  The SSE *read* timeout is derived as
      ``timeout_seconds * _READ_TIMEOUT_FACTOR`` by the default client factory
      (V1 parity, chat_handler.py:1385), so a slow inter-token gap on a long
      cloud turn is not cut off.  A custom ``client_factory`` may override this.
    * ``runtime_limit_store``: :class:`RuntimeLimitStorePort` holding the
      runtime-learned ``max_tokens`` ceilings (D2 dealign).  When omitted
      the adapter uses a per-instance fallback store so learning still
      works within the instance's lifetime (tests); production wiring in
      ``apps/api/_chat_di`` injects the process singleton.
    """

    __slots__ = (
        "_base_url",
        "_api_key",
        "_model",
        "_ids",
        "_client_factory",
        "_timeout",
        "_runtime_limits",
        "_ssl_verify",
        "_ssl_verify_provider",
        "_expects_api_key",
    )

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str,
        ids: IdGenerator,
        client_factory: Any | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        runtime_limit_store: RuntimeLimitStorePort | None = None,
        ssl_verify: bool = True,
        ssl_verify_provider: Callable[[], bool] | None = None,
        expects_api_key: bool = True,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/") or None
        self._api_key = api_key or None
        self._model = model
        self._ids = ids
        self._ssl_verify = ssl_verify
        # Live Settings.ssl_verify provider (apps/api._global_proxy
        # .build_ssl_verify_provider). When present it is read at client-build
        # time so the global SSL toggle hot-applies to every new stream client;
        # the frozen ``ssl_verify`` bool is the back-compat fallback for callers
        # (tests) that pass the bool directly.
        self._ssl_verify_provider = ssl_verify_provider
        # Whether this endpoint is expected to carry an API key. Cloud
        # providers do (a missing key means "user hasn't configured it yet" →
        # emit a friendly ``provider_api_key_missing`` guard instead of a raw
        # upstream 401). Local on-device endpoints do NOT (they run keyless on
        # localhost), so the guard must never fire for them. Defaults to True
        # so any caller that does not opt out keeps the safe cloud behaviour;
        # the local-routing path passes ``expects_api_key=False``.
        self._expects_api_key = expects_api_key
        # When no factory is injected, bind the module default to this
        # instance's ``ssl_verify`` (unified Settings.ssl_verify switch;
        # edition-derived default) so outbound LLM HTTPS follows the same
        # TLS-verification policy as every other outbound client. The verify
        # value is resolved LIVE inside the closure (per client build) via the
        # provider when present, so a runtime SSL toggle hot-applies without
        # re-constructing the adapter; falls back to the frozen bool otherwise.
        self._client_factory = client_factory or (
            lambda *, timeout: _default_client_factory(
                timeout=timeout,
                ssl_verify=(
                    self._ssl_verify_provider()
                    if self._ssl_verify_provider is not None
                    else self._ssl_verify
                ),
            )
        )
        self._timeout = timeout_seconds
        self._runtime_limits: RuntimeLimitStorePort = (
            runtime_limit_store or _PerInstanceRuntimeLimitStore()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stream(
        self,
        request: LLMStreamRequest,
    ) -> AsyncIterator[StreamFrame]:
        """Open the upstream stream and yield :class:`StreamFrame` values."""
        return self._iter(request)

    # ------------------------------------------------------------------
    # H-3 — runtime max_tokens learning
    # ------------------------------------------------------------------

    def _record_learned_max_tokens(self, model_id: str, observed_max: int) -> None:
        """Record ``observed_max`` as the latest known ceiling for *model_id*.

        Subsequent requests with the same ``model_id`` will clamp the
        outgoing ``max_tokens`` to ``min(requested, observed_max)``.  The
        store only shrinks the cached value (``InMemoryRuntimeLimitStore``
        keeps the smallest observed ceiling), so a later, larger
        ``observed_max`` reading is treated as transient noise.

        D2 dealign: the learned ceiling now lives in the injected
        :class:`RuntimeLimitStorePort` instead of a module-level global.
        Implements audit item :ref:`H-3` from
        ``docs/90-refactor/S9-final-parity-audit.md`` §2.2 (parity with
        legacy ``backend/chat_handler.py:1400-1419 record_api_limit_error``).
        """
        if not model_id or observed_max < 16:
            return
        prior = self._runtime_limits.get_limit(
            model_id=model_id, key=_MAX_TOKENS_LIMIT_KEY
        )
        if prior is None or observed_max < prior:
            self._runtime_limits.record_limit(
                model_id=model_id, max_tokens_max=observed_max
            )
            _log.info(
                "chat.llm_stream.max_tokens_learned",
                model_id=model_id,
                observed_max=observed_max,
                prior=prior,
            )

    # ------------------------------------------------------------------
    # Streaming entry point
    # ------------------------------------------------------------------

    async def _iter(
        self, request: LLMStreamRequest
    ) -> AsyncIterator[StreamFrame]:
        sequence = 0
        if self._base_url is None:
            yield StreamFrame.chunk(
                frame_id=self._ids.new_id(),
                sequence=sequence,
                text=_OFFLINE_NOTICE,
            )
            sequence += 1
            yield StreamFrame.end(
                frame_id=self._ids.new_id(),
                sequence=sequence,
                reason="completed",
            )
            return

        # Pre-upstream guard: a cloud request (base_url set) with NO api_key
        # would reach the provider without an Authorization header and come
        # back as a generic HTTP 401 ("Missing Authorization header"), which
        # is opaque to users. Instead, short-circuit with a precise,
        # contract-stable error code so the front-end can render a friendly
        # "set your API key" affordance (and open the in-place key dialog)
        # rather than surfacing the raw upstream 401. This also avoids a
        # wasted round-trip. Purely additive error code (v2.7 §3.1). Gated on
        # ``_expects_api_key`` so it NEVER fires for local on-device endpoints
        # (which are keyless by design); and it distinguishes "no key" from
        # "wrong key" (a real 401 from upstream still classifies as
        # ``chat.llm.auth_failed``).
        if self._expects_api_key and not self._api_key:
            async for frame in self._error_then_end(
                sequence=sequence,
                code="chat.llm.provider_api_key_missing",
                message=(
                    "This cloud model has no API key configured. Set your "
                    "API key to start using it."
                ),
                retryable=False,
            ):
                yield frame
            return

        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(request)
        headers = self._build_headers()
        model_id = payload.get("model", self._model) or self._model

        # D5 (V1 chat_handler.py:1668-1681): track whether any content has
        # already streamed to the client. If the upstream closes a chunked
        # stream non-conformantly (RemoteProtocolError) AFTER we have emitted
        # content, V1 treats it as a normal end (the reply is already there)
        # instead of surfacing a scary protocol error. Only a protocol error
        # with NO content received is reported.
        emitted_content = False

        try:
            async with self._client_factory(
                timeout=self._timeout
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        body_bytes = await response.aread()
                        body_text = body_bytes.decode("utf-8", "replace")

                        # H-3: learn max_tokens ceiling from 400 errors so the
                        # next request for this model auto-clamps.
                        if (
                            response.status_code == 400
                            and _looks_like_max_tokens_error(body_text)
                        ):
                            observed = _extract_max_tokens_limit(body_text)
                            if observed is not None:
                                self._record_learned_max_tokens(model_id, observed)
                            async for frame in self._error_then_end(
                                sequence=sequence,
                                code="chat.llm.max_tokens_clamped",
                                message=(
                                    "模型 max_tokens 参数超出 API 上限。"
                                    "系统已自动学习实际上限，请重试。"
                                    f"（{body_text[:200]}）"
                                ),
                                retryable=True,
                            ):
                                yield frame
                            return

                        # Unsupported sampling parameter (e.g. a model that
                        # rejects ``temperature``): name the offending param
                        # and surface ``model_id`` so the UI can point the
                        # user at Settings → Cloud Models to turn it off.
                        if response.status_code == 400:
                            bad_param = _detect_unsupported_param(body_text)
                            if bad_param is not None:
                                async for frame in self._error_then_end(
                                    sequence=sequence,
                                    code="chat.llm.unsupported_param",
                                    message=(
                                        f"当前模型不支持参数 “{bad_param}”，"
                                        "云端接口因此拒绝了请求。请到"
                                        "“设置 → 云端模型”中编辑该模型，"
                                        f"将参数 “{bad_param}” 标记为不支持后重试。"
                                        f"（{body_text[:200]}）"
                                    ),
                                    retryable=False,
                                    param=bad_param,
                                    model_id=model_id,
                                ):
                                    yield frame
                                return

                        # P2-b: use structured error classification
                        # (V1 api_error_classifier.py parity) to determine
                        # the error code and retryable status.
                        _classified_code, _retryable = _classify_http_error(
                            status_code=response.status_code,
                            body_text=body_text,
                        )

                        # Rate-limit aware backoff: when the throttling
                        # response carries a ``Retry-After`` header, parse it
                        # (integer seconds OR HTTP-date, clamped) and thread it
                        # to the retry policy via the ERROR frame payload
                        # (append-only key ``retry_after_seconds``). The chat
                        # streaming use case reads it and passes it to
                        # ``RetryPolicyPort.next_attempt`` so the THROTTLING
                        # branch honours the server-advised delay instead of
                        # its exponential fallback. Only attached for the
                        # throttling category; absent / malformed header →
                        # ``None`` → key omitted → unchanged backoff.
                        _retry_after_s: float | None = None
                        if _classified_code == "throttling":
                            _retry_after_s = _parse_retry_after_header(
                                response.headers.get("Retry-After"),
                            )

                        # Message: most codes use the raw upstream body for
                        # diagnostics; 404/model_unavailable gets an actionable
                        # Chinese hint (the status alone is opaque to the user)
                        # naming the two things to check — model + base_url.
                        # (NO body-parsing / provider-adapter — out of scope.)
                        if _classified_code == "chat.llm.model_unavailable":
                            _http_message = (
                                "模型或端点不存在，请检查模型与 base_url。"
                                f"（HTTP {response.status_code}: "
                                f"{body_text[:256]}）"
                            )
                        else:
                            _http_message = (
                                f"upstream returned HTTP {response.status_code}: "
                                f"{body_text[:512]}"
                            )

                        async for frame in self._error_then_end(
                            sequence=sequence,
                            code=_classified_code,
                            message=_http_message,
                            retryable=_retryable,
                            retry_after_seconds=_retry_after_s,
                            # Preserve the upstream response body verbatim
                            # (truncated) so the chat error card's [Copy
                            # diagnostics] path can surface exactly what the
                            # provider said. Especially useful for 403 /
                            # permission_denied: providers commonly return a
                            # JSON body naming the offending model / plan
                            # restriction that the generic localized
                            # ``permissionDenied`` message cannot convey.
                            # Additive payload key (§3.1); frontend degrades
                            # gracefully when absent.
                            provider_message=body_text[:2048] if body_text else None,
                        ):
                            yield frame
                        return
                    async for frame in self._iter_sse(
                        response,
                        sequence,
                        request_messages=payload.get("messages") or [],
                        model_id=model_id,
                        # Two-tier stall budget: a turn that offered tools is
                        # agentic and may ride out a long tool-arg structuring
                        # pause (see _CONTENT_STALL_* docs); a no-tools turn is
                        # plain text and held to the tight budget.
                        has_tools=bool(payload.get("tools")),
                    ):
                        if frame.frame_type == StreamFrameType.CHUNK:
                            emitted_content = True
                        yield frame

        # F-3 — httpx exception branches with Chinese user-friendly messages
        # and retryable hints. The ConnectError branch now SUB-CLASSIFIES the
        # failure (TLS / DNS / refused / unreachable / generic) via
        # ``_classify_connect_error`` so a cert-verify failure is NOT retried
        # forever as a generic network blip.
        except httpx.ConnectError as exc:
            _connect_code, _connect_retryable = _classify_connect_error(exc)
            # Per-code Chinese message. The raw ``str(exc)`` is kept in the
            # message for backend diagnostics (the frontend masks it); the
            # deterministic ``code`` drives the actionable bubble + retry
            # disposition.
            _connect_messages: dict[str, str] = {
                "chat.llm.tls_cert_untrusted": (
                    "无法建立安全连接：服务器证书不受信任"
                    "（自签名或颁发机构未知）。请检查 base_url 的证书，"
                    "或在设置中调整 TLS 校验。"
                ),
                "chat.llm.tls_hostname_mismatch": (
                    "无法建立安全连接：服务器证书与主机名不匹配。"
                    "请检查 base_url 是否与证书签发的域名一致。"
                ),
                "chat.llm.tls_cert_expired": (
                    "无法建立安全连接：服务器证书已过期或尚未生效。"
                    "请检查服务器证书有效期与本机系统时间。"
                ),
                "chat.llm.tls_handshake_failed": (
                    "无法建立安全连接：TLS 握手失败。"
                    "请检查 base_url 与网络/代理的 TLS 配置。"
                ),
                "chat.llm.dns_error": (
                    "无法解析模型服务的主机名（DNS 解析失败）。"
                    "请检查 base_url 的域名与本机 DNS 设置。"
                ),
                "chat.llm.connection_refused": (
                    "连接被模型服务拒绝（端口未监听或服务未启动）。"
                    "请检查 base_url 的端口与服务状态。"
                ),
                "chat.llm.host_unreachable": (
                    "无法访问模型服务主机（主机或网络不可达）。"
                    "请检查网络连接与 base_url 配置。"
                ),
                "chat.llm.connect_error": (
                    "无法连接到模型服务（连接失败）。"
                    "请检查网络与 base_url 配置。"
                ),
            }
            _connect_msg = _connect_messages.get(
                _connect_code, _connect_messages["chat.llm.connect_error"]
            )
            async for frame in self._error_then_end(
                sequence=sequence,
                code=_connect_code,
                message=f"{_connect_msg}（{exc}）",
                retryable=_connect_retryable,
            ):
                yield frame
            return
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ) as exc:
            # Observability (was a blind spot): the ReadTimeout branch
            # previously had NO log and an error message whose
            # ``ReadTimeout: {exc}`` rendered as an empty ``ReadTimeout: ``
            # because ``str(httpx.ReadTimeout)`` is empty for a stream read
            # timeout.  We now log the relevant threshold, whether any content
            # had already streamed (distinguishes a mid-stream gap from a
            # first-token stall), and emit a message that names WHICH kind of
            # timeout this is + the actual threshold, so the cause is
            # diagnosable from the error alone.
            #
            # ``ReadTimeout`` is the SSE *read* (idle-gap) timeout = base * 5
            # (the long window that lets a model pause between tokens).
            # ``WriteTimeout`` (sending the request body) and ``PoolTimeout``
            # (acquiring a pooled connection) are governed by the *base*
            # timeout, NOT the read window — so they get a distinct, accurate
            # message instead of the misleading "model produced no output for
            # 600s".
            is_read = isinstance(exc, httpx.ReadTimeout)
            read_timeout = self._timeout * _READ_TIMEOUT_FACTOR
            threshold = read_timeout if is_read else self._timeout
            _log.warning(
                "chat.llm.timeout",
                exc_type=type(exc).__name__,
                is_read_timeout=is_read,
                read_timeout_s=read_timeout,
                base_timeout_s=self._timeout,
                threshold_s=threshold,
                emitted_content=emitted_content,
                model_id=model_id,
            )
            if not is_read:
                # Write / pool timeout: the request body upload or pooled
                # connection acquisition exceeded the base timeout — a network /
                # connectivity issue, not the model being slow to respond.
                _phase_hint = (
                    f"发送请求或建立连接超过 {threshold:.0f} 秒未完成"
                    "（通常是网络不稳定或连接受限）。"
                )
            elif emitted_content:
                # Mid-stream read gap: tokens were already flowing, then the gap
                # between two SSE chunks exceeded the read window — typically the
                # model pausing to organise a long reply / tool-call argument.
                _phase_hint = (
                    f"已收到部分内容后，模型超过 {threshold:.0f} 秒未输出新内容"
                    "（通常是模型在组织一段较长的回复或工具调用）。"
                )
            else:
                # First-token stall: nothing arrived within the read window.
                _phase_hint = (
                    f"模型在 {threshold:.0f} 秒内未返回任何内容"
                    "（可能是服务排队、网络不稳定或服务未就绪）。"
                )
            async for frame in self._error_then_end(
                sequence=sequence,
                code="chat.llm.timeout",
                message=(
                    "模型服务响应超时。"
                    f"{_phase_hint}"
                    "可直接重试；若经常发生，可在设置中调大流式超时时间。"
                    f"（{type(exc).__name__}，超时阈值={threshold:.0f}s）"
                ),
                retryable=True,
            ):
                yield frame
            return
        except httpx.RemoteProtocolError as exc:
            # D5 (V1 chat_handler.py:1668-1681): a non-conformant chunked
            # stream close AFTER content was already delivered is treated as a
            # normal end — the reply is already on screen, so do not scare the
            # user with a protocol error. Only report when NO content arrived.
            if emitted_content:
                _log.warning(
                    "chat.llm.protocol_error_after_content",
                    error=str(exc),
                )
                yield StreamFrame.end(
                    frame_id=self._ids.new_id(),
                    sequence=sequence,
                    reason="completed",
                )
                return
            async for frame in self._error_then_end(
                sequence=sequence,
                code="chat.llm.protocol_error",
                message=(
                    "模型服务返回了不符合协议的响应（连接被对端意外关闭）。"
                    f"通常为代理或网关问题，可重试。（{exc}）"
                ),
                retryable=True,
            ):
                yield frame
            return
        except httpx.ReadError as exc:
            async for frame in self._error_then_end(
                sequence=sequence,
                code="chat.llm.read_error",
                message=(
                    "读取模型服务响应失败（网络中断或对端断开）。"
                    f"可重试。（{exc}）"
                ),
                retryable=True,
            ):
                yield frame
            return
        except httpx.HTTPError as exc:
            # Fallthrough for any other httpx error (TransportError /
            # InvalidURL / etc).  Conservative: not retryable by default.
            async for frame in self._error_then_end(
                sequence=sequence,
                code="chat.llm.network_error",
                message=(
                    "访问模型服务失败。"
                    f"（{type(exc).__name__}: {exc}）"
                ),
                retryable=False,
            ):
                yield frame
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "chat.llm_stream.unexpected_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            async for frame in self._error_then_end(
                sequence=sequence,
                code="chat.llm.unexpected_error",
                message=f"unexpected error during LLM stream: {exc}",
                retryable=False,
            ):
                yield frame
            return

    async def _iter_sse(
        self,
        response: httpx.Response,
        start_sequence: int,
        *,
        request_messages: list[dict[str, Any]] | None = None,
        model_id: str | None = None,
        has_tools: bool = False,
    ) -> AsyncIterator[StreamFrame]:
        sequence = start_sequence
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None

        # Round lead-in text: the assistant text streamed (as CHUNK frames)
        # *before* the tool calls of this round. We re-attach it to the first
        # drained TOOL_CALL frame so the UI can commit it as a standalone
        # visible message even under chunk/tool_call timing races (cloud-path
        # parity with local_model_stream.py:234,284 — fixes the "lead-in text
        # disappears the instant the tool runs" bug).
        accumulated_text = ""

        # V1 parity (chat_handler.py:1371) — remember the LAST non-None
        # finish_reason seen, so the post-loop finaliser can decide how to
        # handle any tool calls accumulated without a ``finish_reason=
        # tool_calls`` chunk (``length`` → discard incomplete args; otherwise
        # → emit) and which truncation / content-filter notice to append.
        last_finish_reason: str | None = None

        # H-1 — stream-health counters.  Detects the proxy-masked-error
        # case where the upstream returned HTTP 200 but never produced
        # any meaningful content (silent failure).
        chunk_count = 0
        meaningful_chunk_count = 0

        # ── tool-call "generating arguments" progress (V2 UX enhancement) ──────
        # While the model streams a long tool-call argument (e.g. a big code
        # block as a write/edit argument), the deltas are accumulated silently
        # (``_accumulate_tool_calls``) and the TOOL_CALL frame is only emitted
        # once everything is assembled — so the UI shows NOTHING until the end
        # and the turn looks frozen.  We surface progress by emitting throttled
        # ``tool_result`` frames with ``phase="generating_args"`` + ``partial=
        # True`` (no new frame_type — §3.1 reuses tool_result with a tail
        # ``phase`` field) carrying the new argument fragment + tool name, so
        # the front end can show the tool card early in a "generating
        # arguments" state with a live char-count / typewriter effect.
        # Throttled by BOTH a min interval and a min byte growth so a 5k-char
        # argument emits ~10-25 progress frames, not thousands.
        progress_last_emit_ts: dict[int, float] = {}
        progress_last_len: dict[int, int] = {}
        # Per tool index: monotonic time of the FIRST progress frame (≈ when the
        # model started streaming this tool call's arguments).  Used at drain to
        # compute ``generation_ms`` so the UI can show a tool's TOTAL time
        # (generation + execution), persisted across reloads.
        progress_started_ts: dict[int, float] = {}

        # ── V1 parity (chat_handler.py:1463-1591): the SSE loop NEVER ends on
        # a ``finish_reason`` — only on ``data: [DONE]`` or the upstream
        # closing the connection (``aiter_lines`` exhausted).  Ending the loop
        # the instant a ``finish_reason`` arrives (the previous design) dropped
        # every frame the provider sends *after* it — most importantly the
        # trailing ``{"choices": [], "usage": {...}}`` chunk that OpenAI's
        # ``stream_options.include_usage`` emits AFTER the ``finish_reason=stop``
        # / ``tool_calls`` chunk and BEFORE ``[DONE]`` — so cloud turns lost
        # their token usage entirely (and an empty-string ``finish_reason``
        # truncated the reply mid-stream).  We now mirror V1: accumulate, emit
        # content live, record ``last_finish_reason``, and defer ALL draining /
        # notices / the single END frame to the post-loop finaliser.
        # Content-stall watchdog (root-cause: upstream goes silent at the
        # application layer while the connection stays alive via keep-alive, so
        # httpx's read-timeout never fires and the reply hangs half-streamed).
        # We track the monotonic time of the last MEANINGFUL event and abandon
        # the stream if no meaningful content arrives within the stall budget —
        # whether because no line arrives at all (caught by the per-``__anext__``
        # wait_for) or because only keep-alive / empty frames arrive (caught by
        # the post-line check).
        #
        # Two-tier budget (see _CONTENT_STALL_* docs): a tool-capable (agentic)
        # turn may legitimately go silent for a long "structuring pause" while
        # the model builds a large tool-call argument, so it gets the generous
        # ceiling; a plain-text turn streams smoothly and gets the tight one so
        # a genuinely wedged upstream is caught fast.
        stall_budget = (
            _CONTENT_STALL_TIMEOUT_SECONDS
            if has_tools
            else _CONTENT_STALL_TEXT_TURN_SECONDS
        )
        loop = asyncio.get_event_loop()
        last_meaningful_ts = loop.time()
        _line_iter = response.aiter_lines().__aiter__()
        # ── Stall diagnostics (READ-ONLY: counters + logs, no control-flow /
        #    behaviour change) ──────────────────────────────────────────────
        # Added to pin down "为什么 write 卡在生成参数中 / 前端 0 字符 / 120s stall":
        # distinguish (A) upstream truly sent 0 bytes for 120s, (B) upstream sent
        # args-delta lines the classifier didn't recognise as tool_call (so
        # neither the progress frame nor the watchdog reset fired), (C) recognised
        # fine but the front-end didn't render the char count. These counters make
        # the real path observable in the log so the user can reproduce.
        _diag_raw_lines = 0            # every raw SSE line (incl keep-alive/empty)
        _diag_data_events = 0         # parsed `data:` JSON events
        _diag_toolcall_events = 0     # events _delta_has_tool_call() matched
        _diag_text_events = 0         # events carrying visible text
        _diag_reasoning_events = 0    # events carrying reasoning text
        _diag_unclassified_events = 0  # parsed events that matched NONE of the above
        _diag_args_chars = 0          # total accumulated tool-call args characters
        _diag_last_line_ts = loop.time()  # monotonic time of the last raw line
        _diag_last_progress_log_ts = 0.0
        while True:
            try:
                remaining = stall_budget - (loop.time() - last_meaningful_ts)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(
                    _line_iter.__anext__(), timeout=remaining
                )
            except StopAsyncIteration:
                break  # upstream closed the connection — normal end of stream
            except asyncio.TimeoutError:
                # No meaningful content within the budget → wedged/silent
                # upstream. Close the connection and surface a RETRYABLE timeout
                # (same category the httpx ReadTimeout path uses) so the turn
                # ends instead of hanging, and the retry policy can re-open.
                _log.warning(
                    "chat.llm.content_stall_timeout",
                    stall_seconds=stall_budget,
                    has_tools=has_tools,
                    meaningful_chunks=meaningful_chunk_count,
                    # Diagnostics (read-only): tell apart the stall causes.
                    #  - raw_lines grew but data_events==0 → only keep-alive/blank
                    #  - toolcall_events==0 & unclassified>0 → args-delta shape the
                    #    classifier missed (root cause B)
                    #  - toolcall_events>0 & args_chars grew → upstream DID stream
                    #    args (front-end char count SHOULD have moved); a big gap
                    #    then means upstream buffered a big chunk (root cause A)
                    #  - secs_since_last_line ≈ budget → truly 0 bytes for the budget
                    diag_raw_lines=_diag_raw_lines,
                    diag_data_events=_diag_data_events,
                    diag_toolcall_events=_diag_toolcall_events,
                    diag_text_events=_diag_text_events,
                    diag_reasoning_events=_diag_reasoning_events,
                    diag_unclassified_events=_diag_unclassified_events,
                    diag_args_chars=_diag_args_chars,
                    diag_secs_since_last_line=round(
                        loop.time() - _diag_last_line_ts, 1
                    ),
                )
                with contextlib.suppress(Exception):
                    await response.aclose()
                async for frame in self._error_then_end(
                    sequence=sequence,
                    code="chat.llm.timeout",
                    message=(
                        "LLM stream stalled: no content for "
                        f"{stall_budget:.0f}s"
                    ),
                    retryable=True,
                ):
                    yield frame
                return
            # Diagnostics (read-only): a raw line arrived (any kind).
            _diag_raw_lines += 1
            _diag_last_line_ts = loop.time()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue

            chunk_count += 1
            _diag_data_events += 1

            # --- Extract usage from any chunk that carries it -----------
            event_usage = _extract_usage(event)
            if event_usage is not None:
                usage = event_usage

            # --- Accumulate tool_calls from delta -----------------------
            # Diagnostics (read-only): snapshot args-chars BEFORE accumulation so
            # we can compute this event's args growth after — a non-zero growth
            # means upstream did stream args bytes in this event, regardless of
            # whether `_delta_has_tool_call` recognises the event shape.
            _diag_prev_args_chars = _diag_args_chars
            _accumulate_tool_calls(event, accumulated_tool_calls)
            _diag_args_chars = 0
            for _tc in accumulated_tool_calls.values():
                _fn = _tc.get("function") if isinstance(_tc, dict) else None
                if isinstance(_fn, dict):
                    _a = _fn.get("arguments")
                    if isinstance(_a, str):
                        _diag_args_chars += len(_a)
            _diag_args_growth = _diag_args_chars - _diag_prev_args_chars
            # Per-event classification (used by the summary + the throttled log).
            _diag_has_toolcall = _delta_has_tool_call(event)
            _diag_has_text = bool(_extract_chunk_text(event))
            _diag_has_reasoning = bool(_extract_reasoning_text(event))
            if _diag_has_toolcall:
                _diag_toolcall_events += 1
            if _diag_has_text:
                _diag_text_events += 1
            if _diag_has_reasoning:
                _diag_reasoning_events += 1
            # An event that carried NONE of the three but DID grow args-chars
            # is the smoking gun for "classifier missed the args shape" — log
            # it every time (usually rare; a flood means we found the bug).
            if (
                not _diag_has_toolcall
                and not _diag_has_text
                and not _diag_has_reasoning
            ):
                _diag_unclassified_events += 1
                if _diag_args_growth > 0:
                    _log.warning(
                        "chat.llm.stream_diag.unclassified_with_args_growth",
                        args_growth=_diag_args_growth,
                        args_chars=_diag_args_chars,
                        keys=sorted(list(event.keys()))[:12],
                    )
            # Throttled progress heartbeat (≤ once per 2s) so a real reproduction
            # leaves a breadcrumb trail without flooding the log. Shows how many
            # of each event type arrived and the current args-chars total; if
            # this line stops appearing >60s before a stall, upstream truly went
            # silent (root cause A). If args_chars keeps growing but the
            # front-end shows 0 characters, the front-end pipeline is the
            # culprit (root cause C).
            _now = loop.time()
            if _now - _diag_last_progress_log_ts >= 2.0:
                _diag_last_progress_log_ts = _now
                # DEBUG level: a per-2s breadcrumb of stream cadence, kept for
                # future diagnosis (it pinned the "long tool_call args go silent
                # mid-stream" root cause on 2026-07-08). Not needed at INFO in
                # normal operation.
                _log.debug(
                    "chat.llm.stream_diag.progress",
                    raw_lines=_diag_raw_lines,
                    data_events=_diag_data_events,
                    toolcall_events=_diag_toolcall_events,
                    text_events=_diag_text_events,
                    reasoning_events=_diag_reasoning_events,
                    unclassified_events=_diag_unclassified_events,
                    args_chars=_diag_args_chars,
                    meaningful_chunks=meaningful_chunk_count,
                    secs_since_last_meaningful=round(
                        _now - last_meaningful_ts, 1
                    ),
                )

            # --- Emit throttled "generating arguments" progress ----------
            # (V2 UX): surface a tool card early + a live arg preview while the
            # model is still streaming a long tool-call argument, instead of
            # showing nothing until the whole argument is assembled.
            if _delta_has_tool_call(event):
                for prog in self._emit_args_progress(
                    accumulated_tool_calls=accumulated_tool_calls,
                    sequence=sequence,
                    last_emit_ts=progress_last_emit_ts,
                    last_len=progress_last_len,
                    started_ts=progress_started_ts,
                ):
                    yield prog
                    sequence += 1

            # --- Extract text content (existing behaviour) --------------
            text = _extract_chunk_text(event)
            if text:
                if text.strip():
                    meaningful_chunk_count += 1
                    last_meaningful_ts = loop.time()  # reset stall watchdog
                accumulated_text += text
                yield StreamFrame.chunk(
                    frame_id=self._ids.new_id(),
                    sequence=sequence,
                    text=text,
                )
                sequence += 1

            # --- Surface reasoning ("thinking") tokens ------------------
            # Reasoning models stream ``delta.reasoning_content`` alongside (or
            # before) the visible answer. Previously this was DISCARDED (only
            # counted toward the meaningful-chunk watchdog). Now we emit it as a
            # dedicated REASONING frame so the UI can render the model's thinking
            # in a collapsible block, separate from the answer CHUNK text. The
            # meaningful-count / stall-watchdog reset is preserved (V1 parity,
            # chat_handler.py:1490 — a thinking-only chunk must not trip the H-1
            # empty-response warning). Independent of ``content``: a single event
            # may carry both (handled above) or reasoning only (handled here).
            reasoning_text = _extract_reasoning_text(event)
            if reasoning_text:
                if reasoning_text.strip():
                    meaningful_chunk_count += 1
                    last_meaningful_ts = loop.time()  # reset stall watchdog
                yield StreamFrame.reasoning(
                    frame_id=self._ids.new_id(),
                    sequence=sequence,
                    text=reasoning_text,
                )
                sequence += 1
            elif not text and _delta_has_tool_call(event):
                # Tool-call deltas (no visible content, no reasoning) still
                # count as meaningful even though they don't surface as a CHUNK
                # frame here (the accumulated tool_calls are drained later on
                # ``finish_reason``). V1 parity: chat_handler.py:1490.
                meaningful_chunk_count += 1
                last_meaningful_ts = loop.time()  # reset stall watchdog


            # --- Record finish_reason (do NOT end the loop) -------------
            finish_reason = _get_finish_reason(event)
            if finish_reason is None:
                continue

            if finish_reason == "tool_calls":
                # V1 parity (chat_handler.py:1522-1531): emit the accumulated
                # tool_calls immediately and clear them, then ``continue`` —
                # the stream keeps running so the trailing usage chunk / [DONE]
                # are still read.  Already-emitted calls are cleared so the
                # finaliser does not re-emit them.
                for frame in self._drain_tool_calls(
                    accumulated_tool_calls,
                    sequence,
                    lead_in=accumulated_text,
                    started_ts=progress_started_ts,
                ):
                    yield frame
                    sequence += 1
                accumulated_tool_calls.clear()
                # Reset the args-progress throttle state alongside the cleared
                # tool calls: a single SSE stream can carry MULTIPLE batches of
                # tool_calls reusing the same ``index`` (e.g. a provider that
                # ends one tool-call batch with ``finish_reason=tool_calls`` and
                # then streams another in the same connection).  Without this,
                # the stale ``progress_last_len[idx]`` from the previous batch
                # would make the next batch's growth delta go negative and
                # silently suppress its progress frames (and emit empty/garbled
                # deltas).  Clearing keeps each batch's progress independent.
                progress_last_emit_ts.clear()
                progress_last_len.clear()
                progress_started_ts.clear()
                accumulated_text = ""
                last_finish_reason = finish_reason
                continue

            # ``stop`` / ``length`` / ``content_filter`` / "" / unknown: just
            # record it (V1 chat_handler.py:1518-1520) and keep reading.  All
            # terminal handling happens once, after the loop.
            last_finish_reason = finish_reason

        # ── Post-loop finaliser (V1 chat_handler.py:1592-1809) ──────────────
        # Reached on [DONE] or a clean upstream close.  Handle any tool calls
        # accumulated without a ``finish_reason=tool_calls`` chunk, append the
        # truncation / content-filter notice for the plain-text case, run the
        # empty-response health check, then emit exactly ONE END frame carrying
        # the (now fully-read) usage.
        async for frame in self._finalize_sse(
            sequence=sequence,
            accumulated_tool_calls=accumulated_tool_calls,
            accumulated_text=accumulated_text,
            last_finish_reason=last_finish_reason,
            usage=usage,
            chunk_count=chunk_count,
            meaningful_chunk_count=meaningful_chunk_count,
            request_messages=request_messages,
            model_id=model_id,
            started_ts=progress_started_ts,
        ):
            yield frame

    async def _finalize_sse(
        self,
        *,
        sequence: int,
        accumulated_tool_calls: dict[int, dict[str, Any]],
        accumulated_text: str,
        last_finish_reason: str | None,
        usage: dict[str, Any] | None,
        chunk_count: int,
        meaningful_chunk_count: int,
        request_messages: list[dict[str, Any]] | None = None,
        model_id: str | None = None,
        started_ts: dict[int, float] | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Terminal handling shared by the [DONE] and clean-close exits.

        V1 parity (chat_handler.py:1592-1648 + 1801-1809): once the stream
        is fully drained,

        * if tool calls are still accumulated (the provider ended a
          tool-using turn with ``finish_reason=stop`` / "" / no chunk, common
          for Claude-via-proxy), emit them — UNLESS ``finish_reason=length``
          truncated their arguments, in which case discard them and emit a
          truncation notice instead (chat_handler.py:1593-1617);
        * otherwise (plain-text turn), emit the ``length`` truncation notice
          or the ``content_filter`` notice when applicable; unknown
          finish_reasons are silent (chat_handler.py:1618-1648);
        * run the empty-response health check (H-1) — when the stream
          produced framing chunks but no meaningful content / tool_calls /
          notice, surface a retryable ``empty_response`` ERROR instead of a
          silent ``completed`` END (V1 chat_handler.py:1767-1789): a proxy
          that masks an upstream error as an empty HTTP 200 must not look
          like a successful empty reply;
        * emit a single terminal END frame carrying the final usage.
        """
        emitted_notice = False
        if accumulated_tool_calls:
            if last_finish_reason == "length":
                # Generation truncated mid-tool-call: arguments are
                # incomplete, so discard to avoid empty-argument execution
                # and warn the user (V1 chat_handler.py:1594-1609).
                names = [
                    tc.get("function", {}).get("name", "unknown")
                    for tc in accumulated_tool_calls.values()
                ]
                _log.warning(
                    "chat.llm_stream.tool_calls_truncated",
                    num_calls=len(accumulated_tool_calls),
                    tool_names=names,
                )
                yield StreamFrame.chunk(
                    frame_id=self._ids.new_id(),
                    sequence=sequence,
                    text=make_truncation_notice(
                        tool_name=names[0] if names else None,
                        language="zh",
                    ),
                )
                sequence += 1
                emitted_notice = True
            else:
                # V1 chat_handler.py:1610-1617 — emit accumulated tool calls
                # regardless of the literal finish_reason (stop / "" / unknown
                # / None), so a Claude-via-proxy turn that ends with
                # finish_reason=stop does not silently drop its tool calls and
                # terminate the agentic loop mid-task.
                for frame in self._drain_tool_calls(
                    accumulated_tool_calls,
                    sequence,
                    lead_in=accumulated_text,
                    started_ts=started_ts,
                ):
                    yield frame
                    sequence += 1
        else:
            # Plain-text turn: surface the truncation / content-filter notice
            # (V1 chat_handler.py:1618-1648).  Unknown finish_reasons are
            # silent (forward compatibility).
            if last_finish_reason == "length":
                yield StreamFrame.chunk(
                    frame_id=self._ids.new_id(),
                    sequence=sequence,
                    text=make_truncation_notice(tool_name=None, language="zh"),
                )
                sequence += 1
                emitted_notice = True
            elif last_finish_reason == "content_filter":
                yield StreamFrame.chunk(
                    frame_id=self._ids.new_id(),
                    sequence=sequence,
                    text=make_content_filter_notice(
                        tool_name=None, language="zh"
                    ),
                )
                sequence += 1
                emitted_notice = True

        # ── Empty-response health check (V1 chat_handler.py:1757-1789) ──────
        # A proxy can mask an upstream 4xx/5xx as an empty HTTP 200 (framing
        # chunks but no content / tool_calls / finish_reason).  V1 surfaces a
        # retryable ``empty_response`` error here instead of pretending the
        # turn completed successfully; V2 previously only logged a warning,
        # so the user saw a silent empty reply with no retry affordance.
        self._warn_if_empty_response(chunk_count, meaningful_chunk_count)
        if (
            chunk_count > 0
            and meaningful_chunk_count == 0
            and not accumulated_tool_calls
            and not emitted_notice
        ):
            yield StreamFrame(
                frame_id=self._ids.new_id(),
                frame_type=StreamFrameType.ERROR,
                sequence=sequence,
                payload={
                    "code": "empty_response",
                    "message": (
                        "模型返回了空响应（HTTP 200 但无任何输出）。"
                        "可能原因：中间代理将上游错误掩盖为 200 OK，"
                        "或上游 API 内部错误导致流提前中断，"
                        "或请求被静默拒绝（内容审核 / 参数校验失败）。"
                        "建议检查代理日志或直接重试。"
                    ),
                    "retryable": True,
                },
            )
            yield StreamFrame.end(
                frame_id=self._ids.new_id(),
                sequence=sequence + 1,
                reason="failed",
            )
            return

        # D4 / H-4 — local prompt-token fallback (V1 chat_handler.py:1791-1798).
        # Some OpenAI-compatible proxies ignore ``stream_options.include_usage``
        # and never emit a ``usage`` block, leaving the UI token counter stuck
        # at zero.  When an estimator is wired and the upstream sent no usage
        # (or a usage with no prompt_tokens), estimate prompt_tokens locally
        # from the exact wire messages so the terminal END still carries a
        # plausible figure.  Best-effort: the estimator never raises, but we
        # guard defensively so a counting failure can never abort the stream.
        usage = self._maybe_estimate_usage(
            usage=usage,
            request_messages=request_messages,
            model_id=model_id,
        )

        yield StreamFrame.end(
            frame_id=self._ids.new_id(),
            sequence=sequence,
            reason="completed",
            usage=usage,
        )

    def _maybe_estimate_usage(
        self,
        *,
        usage: dict[str, Any] | None,
        request_messages: list[dict[str, Any]] | None,
        model_id: str | None,
    ) -> dict[str, Any] | None:
        """Fill ``prompt_tokens`` via a local chars/4 estimate when missing.

        V1 parity (``backend/chat_handler.py:1791-1798``).  Returns the
        (possibly augmented) usage dict.  No-op when no messages are available
        or the upstream already reported a non-zero ``prompt_tokens``.

        Phase B: the estimate is a BPE-free chars/4 heuristic over
        ``request_messages`` (same口径 as
        ``_agentic_kernel.estimate_wire_tokens``) — this only backfills a
        MISSING cloud ``prompt_tokens`` so the running counter has a coarse
        value; the authoritative figure is always the provider-measured cloud
        usage when present.  No BPE / tiktoken estimator is involved.
        """
        if not request_messages:
            return usage
        existing_prompt = 0
        if isinstance(usage, dict):
            try:
                existing_prompt = int(usage.get("prompt_tokens") or 0)
            except (TypeError, ValueError):
                existing_prompt = 0
        if existing_prompt > 0:
            return usage
        # BPE-free chars/4 estimate over content + tool_calls args (mirrors
        # estimate_wire_tokens口径).
        total_chars = 0
        for m in request_messages:
            total_chars += len(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or ():
                if isinstance(tc, dict):
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        total_chars += len(str(fn.get("arguments") or ""))
        estimated = total_chars // 4
        if estimated <= 0:
            return usage
        new_usage: dict[str, Any] = dict(usage) if isinstance(usage, dict) else {}
        new_usage["prompt_tokens"] = estimated
        completion = 0
        try:
            completion = int(new_usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            completion = 0
        # Only synthesise total_tokens when absent so we don't clobber a
        # provider-reported total.
        if not new_usage.get("total_tokens"):
            new_usage["total_tokens"] = estimated + completion
        _log.info(
            "chat.llm_stream.prompt_tokens_estimated",
            prompt_tokens=estimated,
            model_id=model_id or self._model,
        )
        return new_usage

    def _emit_args_progress(
        self,
        *,
        accumulated_tool_calls: dict[int, dict[str, Any]],
        sequence: int,
        last_emit_ts: dict[int, float],
        last_len: dict[int, int],
        started_ts: dict[int, float],
    ) -> Iterator[StreamFrame]:
        """Yield throttled ``generating_args`` progress frames (V2 UX).

        While the model streams a long tool-call argument, this surfaces the
        tool card early + a live preview so the turn does not look frozen.
        Reuses the ``tool_result`` frame (no new frame_type — §3.1) with
        ``phase="generating_args"`` + ``partial=True`` and the freshly grown
        argument text in ``delta`` / ``result``.

        Throttle: emit for an index when its name is known AND either it has
        never been emitted (first frame → card appears immediately) OR both
        ``_ARGS_PROGRESS_MIN_INTERVAL_S`` has elapsed and the argument has grown
        by ``_ARGS_PROGRESS_MIN_GROWTH`` chars since the last emit.  ``sequence``
        is advanced by the caller (one per yielded frame).

        ``started_ts`` records, per tool index, the monotonic time of the FIRST
        progress frame (≈ when the model started streaming this tool call's
        arguments).  ``_drain_tool_calls`` reads it to compute ``generation_ms``
        (the time spent generating the arguments) so the UI can show a tool's
        TOTAL time (generation + execution), not just execution — see
        ``StreamFrame.tool_call``.
        """
        now = time.monotonic()
        offset = 0
        for idx in sorted(accumulated_tool_calls):
            tc = accumulated_tool_calls[idx]
            name = (tc.get("function") or {}).get("name") or ""
            if not name:
                # Wait until the tool name is known so the card is meaningful.
                continue
            args_str = (tc.get("function") or {}).get("arguments") or ""
            cur_len = len(args_str)
            prev_len = last_len.get(idx)
            first = prev_len is None
            if not first:
                grew = cur_len - prev_len
                elapsed = now - last_emit_ts.get(idx, 0.0)
                if (
                    grew < _ARGS_PROGRESS_MIN_GROWTH
                    or elapsed < _ARGS_PROGRESS_MIN_INTERVAL_S
                ):
                    continue
            if first:
                # Stamp the generation start (first time we see args for this
                # tool index) so the total time can be derived at drain.
                started_ts.setdefault(idx, now)
            delta_text = args_str if first else args_str[prev_len:]
            last_emit_ts[idx] = now
            last_len[idx] = cur_len
            yield StreamFrame.tool_result(
                frame_id=self._ids.new_id(),
                sequence=sequence + offset,
                tool_name=name,
                # ``result`` mirrors the cumulative args so a consumer ignoring
                # ``phase`` still sees text; ``delta`` carries only the new
                # fragment for incremental UI append.
                result=args_str,
                delta=delta_text,
                partial=True,
                phase="generating_args",
                tool_call_id=tc.get("id") or None,
            )
            offset += 1

    def _drain_tool_calls(
        self,
        accumulated_tool_calls: dict[int, dict[str, Any]],
        sequence: int,
        lead_in: str | None = None,
        started_ts: dict[int, float] | None = None,
    ) -> Iterator[StreamFrame]:
        """Yield a TOOL_CALL frame for each accumulated tool call.

        V1 parity (chat_handler.py:1611-1617): tool calls accumulated from
        the delta stream are emitted regardless of which terminal
        finish_reason carried them (``tool_calls`` / ``stop`` / ``None``),
        so an OpenAI-compatible proxy that ends a tool-using turn with
        ``finish_reason="stop"`` (common for Claude bridges) does not silently
        drop the calls and prematurely end the agentic loop. The caller is
        responsible for advancing ``sequence`` (one per yielded frame) and for
        clearing ``accumulated_tool_calls`` afterwards. ``length`` /
        ``content_filter`` truncation are handled by the caller *before*
        draining (incomplete arguments must be discarded, not executed).

        ``lead_in`` is the round's assistant text the model streamed *before*
        the tool call(s). It is also sent as ordinary ``chunk`` frames, but we
        attach it to the **first** TOOL_CALL frame here so the UI can commit
        the lead-in as a standalone, permanently-visible assistant message even
        if the preceding chunk frame had not yet been applied to the live
        buffer when the tool_call arrived — matching the local-model path
        (``local_model_stream.py:234,284``) and V1 behaviour
        (``useChat.js:2460-2470``: each round's lead-in stays visible). Only the
        first frame carries it (the UI de-dupes; later frames in the same round
        share the same lead-in). Cloud (OpenAI delta) parity for the
        "text disappears the instant the tool runs" bug.
        """
        start = sequence
        now = time.monotonic()
        for offset, _idx in enumerate(sorted(accumulated_tool_calls)):
            tc = accumulated_tool_calls[_idx]
            arguments_str = tc["function"]["arguments"]
            try:
                arguments = json.loads(arguments_str)
            except (json.JSONDecodeError, TypeError):
                arguments = {"_raw": arguments_str}
            # Generation time = first progress frame for this index → now (drain
            # of the assembled call).  Only when this index actually had a
            # streamed-args phase (``started_ts`` recorded); short one-delta
            # calls have none and omit the field.
            generation_ms: int | None = None
            if started_ts is not None and _idx in started_ts:
                generation_ms = max(0, int((now - started_ts[_idx]) * 1000))
            yield StreamFrame.tool_call(
                frame_id=self._ids.new_id(),
                sequence=start + offset,
                tool_name=tc["function"]["name"],
                arguments=arguments,
                tool_call_id=tc.get("id") or None,
                # PR-090 C-1: forward the captured Vertex AI thought_signature
                # so the agentic loop can echo it back on the next turn
                # (V1 chat_handler.py:677-679/787-789 fidelity).
                thought_signature=tc.get("thought_signature") or None,
                # Only the first tool_call of the round carries the lead-in;
                # the UI commits it once and de-dupes against the live buffer.
                lead_in=(lead_in or None) if offset == 0 else None,
                generation_ms=generation_ms,
            )

    @staticmethod
    def _warn_if_empty_response(
        chunk_count: int, meaningful_chunk_count: int
    ) -> None:
        """H-1 — log a warning when the stream closed without producing meaningful content.

        ``chunk_count > 0 and meaningful_chunk_count == 0`` is the
        signature of an upstream proxy that swallowed an error and
        returned an empty 200.  The upstream itself never sent text /
        tool_calls / finish_reason, but it did send framing chunks.
        """
        if chunk_count > 0 and meaningful_chunk_count == 0:
            _log.warning(
                "chat.stream.empty_response_detected",
                chunk_count=chunk_count,
                meaningful_chunk_count=meaningful_chunk_count,
            )

    async def _error_then_end(
        self,
        *,
        sequence: int,
        code: str,
        message: str,
        retryable: bool = False,
        param: str | None = None,
        model_id: str | None = None,
        retry_after_seconds: float | None = None,
        provider_message: str | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Emit one ERROR frame followed by a failed END frame.

        ``retryable`` is exposed as an additional payload key on the
        ERROR frame so the front-end can decide whether to surface a
        "retry" affordance.  Per v2.7 §3.1 the SSE frame *shape* is
        unchanged (still ``code`` + ``message``); ``retryable`` is an
        additive payload field.

        ``param`` / ``model_id`` are additional additive payload fields
        carried only for the ``chat.llm.unsupported_param`` error code so
        the front-end can name the offending parameter and deep-link to the
        model's entry in Settings → Cloud Models (where the user turns the
        param's "supported" toggle off).

        ``retry_after_seconds`` (additive, §3.1) is a server-advised
        throttling delay parsed from the upstream ``Retry-After`` header
        (integer seconds or HTTP-date, clamped). It is emitted only when a
        non-``None`` value is supplied (typically on a ``throttling`` ERROR
        frame); the chat streaming use case reads
        ``payload.get("retry_after_seconds")`` and forwards it to
        ``RetryPolicyPort.next_attempt(..., server_advised_delay_s=...)`` so
        the THROTTLING backoff honours the server directive. Absent →
        key omitted → existing exponential backoff unchanged.

        ``provider_message`` (additive, §3.1) is the raw upstream response
        body (truncated) preserved verbatim for frontend diagnostics — the
        chat error card's [Copy diagnostics] path surfaces it so the user
        can see what the provider actually said (e.g. a 403 body naming the
        exact model / plan restriction) without cluttering the primary
        localized ``message``. Emitted only when non-``None`` / non-empty.
        """
        payload: dict[str, Any] = {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            # ``retry_disposition`` (additive, §3.1 tail-only): a coarse,
            # frontend-facing hint derived from ``code`` via the single
            # source of truth in ``qai.chat.domain.error_disposition``. It
            # describes the SHAPE of the auto-retry/user-action the turn will
            # take ("never" / "bounded_fast" / "network_wait" / ...), letting
            # the frontend pick the right bubble/affordance without
            # re-classifying the code. ``retryable`` stays for back-compat.
            "retry_disposition": retry_disposition_for(code),
        }
        if param:
            payload["param"] = param
        if model_id:
            payload["model_id"] = model_id
        if retry_after_seconds is not None:
            payload["retry_after_seconds"] = float(retry_after_seconds)
        if provider_message:
            payload["provider_message"] = str(provider_message)
        yield StreamFrame(
            frame_id=self._ids.new_id(),
            frame_type=StreamFrameType.ERROR,
            sequence=sequence,
            payload=payload,
        )
        yield StreamFrame.end(
            frame_id=self._ids.new_id(),
            sequence=sequence + 1,
            reason="failed",
        )

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    def _build_payload(self, request: LLMStreamRequest) -> dict[str, Any]:
        """Assemble the JSON request body sent to the upstream endpoint.

        PR-090 wires three additive layers on top of the original
        message-flattening logic:

        1. **Message sanitisation** (C-4 + F-6):
           orphan tool-message drop → tool-name slug → Vertex AI
           thought-signature flatten (only when the request opts in via
           ``extra["__flatten_no_signature__"]`` so non-Vertex providers
           are not affected).
        2. **Per-model parameter resolution** (C-5):
           :func:`resolve_params` clamps / filters the seven sampling
           tunables according to the resolved
           :class:`ModelProfile`.  The profile is read from
           ``extra["__profile__"]`` (optional; absent => empty profile,
           i.e. forward whatever the request supplied).
        3. **H-3 ``max_tokens`` clamp**:
           if the injected :class:`RuntimeLimitStorePort` has a learned
           ceiling for ``model_id``, clamp the resolved ``max_tokens`` to
           it.
        """
        extra = dict(request.extra or {})

        # Pull the system prompt out of ``extra`` BEFORE the generic
        # "forward remaining extra keys" loop below, so it is injected as
        # a proper ``system`` role message rather than leaking onto the
        # JSON payload as an unknown top-level key (which OpenAI-compatible
        # endpoints silently ignore).  This mirrors the on-device adapter
        # (``LocalModelStream._build_payload``) which already prepends
        # ``extra["system_prompt"]`` as the first message.  Without this,
        # feature-mode prompts (translate / code / ppt persona) produced by
        # ``RichSystemPromptBuilder`` never reached the model.
        system_prompt = extra.pop("system_prompt", None)
        if not (isinstance(system_prompt, str) and system_prompt.strip()):
            system_prompt = None

        # ---- 1. Build the OpenAI message list. ----
        # ``request.history`` is normally a tuple of domain ``Message``
        # objects, but the agentic loop's context compressor
        # (``streaming.py:_compress_history`` / ``_maybe_presend_compress``)
        # returns **plain dicts** (``{"role": ..., "content": ...}``) which
        # are then threaded back through ``LLMStreamRequest.history``. Both
        # shapes must be accepted here: assuming ``msg.role.value`` blindly
        # crashed the whole SSE turn with ``AttributeError: 'dict' object
        # has no attribute 'role'`` the moment a compression ran mid-loop
        # (the reported "工具跑几轮后无故停止、且不给原因" regression — the
        # turn aborted server-side on the follow-up LLM request).
        messages: list[dict[str, Any]] = []
        for msg in request.history:
            messages.append(_normalize_history_message(msg))
        messages.append({"role": "user", "content": request.prompt.text})

        # Allow callers to pre-prepend a richer history (e.g. with
        # tool_calls + tool messages) via ``extra["messages"]``.  When
        # supplied, it replaces the simple prompt+history flattening
        # above.  This preserves backwards compatibility while letting
        # the chat use case forward fully assembled OpenAI message
        # arrays once the agentic loop wires them in.
        custom_messages = extra.pop("messages", None)
        if isinstance(custom_messages, list) and custom_messages:
            messages = [m for m in custom_messages if isinstance(m, dict)]

        # Prepend the system prompt unless the assembled list already
        # opens with a system message (don't double-inject when the
        # caller pre-built a full message array that includes its own
        # system turn).
        if system_prompt is not None:
            first_role = messages[0].get("role") if messages else None
            if first_role != "system":
                messages.insert(0, {"role": "system", "content": system_prompt})

        # ---- 2. Pre-send sanitisation pipeline (C-4 + F-6). ----
        messages = sanitize_tool_messages(messages)
        messages = sanitize_messages_tool_call_names(messages)

        # Vertex AI thought-signature flatten is opt-in: callers that
        # know the resolved model is a Vertex thinking model set
        # ``extra["__flatten_no_signature__"] = True``.  Non-Vertex
        # providers leave this unset and skip the flatten entirely.
        flatten_flag = bool(extra.pop("__flatten_no_signature__", False))
        if flatten_flag:
            messages = flatten_tool_calls_without_signature(messages)

        # Anthropic prompt-cache stable-prefix boundary (改动2b): the
        # ``StreamChatUseCase`` maintains a per-conversation MONOTONIC
        # tool-output aging boundary and forwards the COUNT of oldest frozen
        # aged-tool placeholders here as an adapter-internal control key, so the
        # cache breakpoint (c) can re-derive its anchor inside that byte-stable
        # region instead of the drifting tail. Pop it BEFORE the generic
        # ``__``-prefixed filter loop so it never reaches the wire; ``None`` /
        # ``0`` (absent — main-agent early rounds / sub-agent / non-Anthropic)
        # restores the prior "last message" breakpoint scan.
        _cache_stable_aged_raw = extra.pop("__cache_stable_aged_prefix__", None)
        cache_stable_aged: int | None = None
        if isinstance(_cache_stable_aged_raw, int) and not isinstance(
            _cache_stable_aged_raw, bool
        ):
            cache_stable_aged = _cache_stable_aged_raw

        model_id = request.model_hint or self._model
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # ---- 3. Per-model param resolution (C-5). ----
        # Build the per-model profile from the user-configured sampling
        # constraints (cloud catalog ``models[].params``), injected by the
        # provider-routing layer as ``extra["__model_params__"]``.  This is
        # what makes a user's "this model does not support temperature"
        # declaration actually drop the param from the wire (overriding the
        # family-regex default).  A pre-built ``__profile__`` (set by a caller)
        # still takes precedence; absent both, an empty profile forwards
        # whatever the request carried.
        profile = extra.pop("__profile__", None)
        config_params = extra.pop("__model_params__", None)
        if not isinstance(profile, ModelProfile):
            if isinstance(config_params, dict) and config_params:
                profile = profile_from_config_params(model_id, config_params)
            else:
                profile = ModelProfile(model_id=model_id)
        resolved = resolve_params(profile, extra)

        # VERIFY (Problem 1 — sub-agent max_tokens parity): surface the
        # max_tokens that will actually be SENT on the wire for THIS request,
        # before the H-3 clamp below. Lets a tester confirm at a glance that
        # both the main agent AND a cloud sub-agent now send the family default
        # (e.g. opus 16384) instead of the sub-agent silently omitting it and
        # degrading to the upstream gateway's 4096 default. ``max_tokens=None``
        # in the log = the request carries NO max_tokens (the old sub-agent bug,
        # or an intentionally-unset local model).
        _log.info(
            "chat.llm_stream.max_tokens_outgoing",
            model_id=model_id,
            max_tokens=resolved.get("max_tokens"),
        )

        # H-3: clamp max_tokens against the learned ceiling for this
        # model_id, regardless of how it was supplied.  The ceiling lives
        # in the injected RuntimeLimitStorePort (D2 dealign).
        if "max_tokens" in resolved:
            learned = self._runtime_limits.get_limit(
                model_id=model_id, key=_MAX_TOKENS_LIMIT_KEY
            )
            if learned is not None and learned > 0:
                _before_clamp = int(resolved["max_tokens"])
                _after_clamp = min(_before_clamp, learned)
                resolved["max_tokens"] = _after_clamp
                # DIAG (Problem A — opus clamped to 4096): the H-3 runtime
                # store applies a per-``model_id`` learned ceiling regardless
                # of the family default. The store is a PROCESS-GLOBAL in-memory
                # singleton (apps/api/_chat_di.py: ``InMemoryRuntimeLimitStore()``)
                # so a ceiling learned from an earlier 400 in THIS process keeps
                # clamping every subsequent request for the SAME model_id until
                # restart — even when ``resolved["max_tokens"]`` (the family
                # default, e.g. opus 16384) is higher. This log surfaces the
                # exact model_id, the family-resolved request value, the learned
                # ceiling and the final clamped value so the user can confirm
                # whether opus's 4096 is a stale/wrong learned limit vs a real
                # upstream-proxy cap. (Pair with ``chat.llm_stream.max_tokens_learned``
                # which records WHEN/WHAT 400 the ceiling was learned from.)
                if _after_clamp < _before_clamp:
                    _log.info(
                        "chat.llm_stream.max_tokens_clamp_applied",
                        model_id=model_id,
                        requested_max_tokens=_before_clamp,
                        learned_ceiling=learned,
                        final_max_tokens=_after_clamp,
                    )

        payload.update(resolved)

        # ---- 3b. Cloud-path tool advertisement (PR-fix-cloud-tools). ----
        # When the streaming use case has populated ``extra["tools_schemas"]``
        # AND the resolved model is NOT local (i.e. an OpenAI-compatible
        # cloud provider that supports the standard function-calling
        # protocol), forward the schemas as the OpenAI standard
        # ``payload["tools"]`` array plus ``tool_choice="auto"`` so the
        # model can emit native ``tool_calls`` deltas.
        #
        # Tools are advertised to cloud models ONLY through this standard
        # ``payload["tools"]`` parameter — the system prompt body NO LONGER
        # carries a ``<tools>`` XML copy (removed 2026-06-15, AGENTS.md 🟡🟡:
        # the V1 double-advertisement was protocol misuse + harmful redundancy;
        # the model service injects the tools itself from ``tools``).  Local
        # on-device models likewise advertise via ``payload["tools"]`` (handled
        # in the local adapter), forwarded to the daemon's PromptOptimizer.
        #
        # Detection: the upstream ``ProviderRoutingLLMStream`` injects
        # ``__is_local_model__`` (bool) into ``extra`` based on the
        # resolver's ``ResolvedModel.is_local`` flag.  Absence (legacy
        # callers that bypass the routing wrapper) defaults to ``False``
        # i.e. "treat as cloud" so unit tests that exercise the HTTP
        # transport directly with explicit ``tools_schemas`` see the
        # advertisement on the wire.  Adapter-internal control key (the
        # ``__`` prefix loop below filters it out before forwarding the
        # rest of ``extra``).
        is_local_model = bool(extra.pop("__is_local_model__", False))
        tools_schemas = extra.pop("tools_schemas", None)
        if (
            not is_local_model
            and isinstance(tools_schemas, (list, tuple))
            and tools_schemas
        ):
            # Coerce to a fresh list of dicts; defensive against callers
            # that pass tuples / Mapping subtypes.  Reject any non-dict
            # entries silently (the wire format requires dicts).
            advertised: list[dict[str, Any]] = [
                dict(item) for item in tools_schemas if isinstance(item, dict)
            ]
            if advertised:
                payload["tools"] = advertised
                # Mirror V1 (``backend/chat_handler.py`` cloud path):
                # ``tool_choice="auto"`` lets the model decide whether to
                # call a tool or respond with plain text.  Callers can
                # still override by passing an explicit ``tool_choice``
                # in ``extra`` — the generic forward loop below will
                # then overwrite this default.
                payload["tool_choice"] = "auto"

        # ---- 4. Forward any remaining extra keys that are NOT tunables
        # the resolver already handled and not adapter-internal control
        # keys.  Reserved OpenAI top-level keys are filtered out.
        reserved = {"model", "messages", "stream", "stream_options"}
        for key, value in extra.items():
            if key in reserved:
                continue
            if key in _CHAT_CONTROL_KEYS:
                # Chat-context orchestration metadata consumed by the use
                # case / system-prompt builder — never valid OpenAI body
                # fields, so they must not leak onto the wire (the
                # upstream silently ignores unknown keys, but forwarding
                # them is noise and can confuse strict gateways).
                continue
            if key in TUNABLE_KEYS:
                # Already processed by resolve_params (and possibly
                # dropped if profile said "supported=False").
                continue
            if key.startswith("__"):
                # Adapter-internal control keys ("__profile__",
                # "__flatten_no_signature__", ...) never go on the wire.
                continue
            payload[key] = value

        # P7 — ``tool_choice`` integrity guard. When the caller (use case
        # extra / cloud catalog override) passed an explicit
        # ``{"type": "function", "function": {"name": "X"}}`` ``tool_choice``,
        # validate that ``X`` is actually advertised in ``payload["tools"]``.
        # Anthropic and several OpenAI gateways return HTTP 400 if it is
        # not ("tool_choice.name not in tools"); protect by SOFT-DEGRADING
        # to ``tool_choice="auto"`` (model picks any advertised tool or
        # responds with plain text) and logging a warning so the operator
        # can see the misconfiguration. We do NOT delete the field — the
        # model may legitimately want to be forced into ANY tool call,
        # which ``auto`` no longer guarantees but is the closest valid
        # request shape that does not block the turn. Caller-supplied
        # string forms (``"none"``, ``"required"``, ``"auto"``,
        # ``"any"``) pass through unchanged.
        _tc = payload.get("tool_choice")
        if isinstance(_tc, dict) and _tc.get("type") == "function":
            _fn = _tc.get("function") if isinstance(_tc.get("function"), dict) else {}
            _wanted = _fn.get("name") if isinstance(_fn, dict) else None
            if isinstance(_wanted, str) and _wanted:
                _advertised_tools = payload.get("tools")
                _names: set[str] = set()
                if isinstance(_advertised_tools, list):
                    for _t in _advertised_tools:
                        if not isinstance(_t, dict):
                            continue
                        _tfn = _t.get("function")
                        if isinstance(_tfn, dict):
                            _n = _tfn.get("name")
                            if isinstance(_n, str) and _n:
                                _names.add(_n)
                if _wanted not in _names:
                    _log.warning(
                        "chat.llm_stream.tool_choice_unknown",
                        requested=_wanted,
                        advertised=sorted(_names),
                        action="downgraded_to_auto",
                    )
                    payload["tool_choice"] = "auto"

        # ---- 5. Anthropic prompt caching (PR-D). ----
        # Only Anthropic-family targets get ``cache_control`` breakpoints so
        # round 2+ bills the stable prefix (tools + system + last message) as
        # cache_read (~10% price). Non-Anthropic models (gpt / local / other
        # cloud) leave this branch untaken → their payload is byte-for-byte
        # unchanged (hard requirement: some providers 400 on unknown
        # cache_control fields).
        if _is_anthropic_family(model_id):
            _apply_anthropic_prompt_caching(
                payload, stable_aged_prefix=cache_stable_aged
            )

        # DIAG (token-consumption breakdown, DEBUG level): per-block token split
        # of the FINAL wire payload (system / history / tools), chars//4 rough
        # estimate. Kept at debug level as a diagnostic aid for future token
        # investigations (silent by default; enable debug logging to surface it).
        # Pure read of the assembled ``payload``; wrapped so it can NEVER break a
        # request. Pair with ``chat.diag.extract_usage`` to reconcile self vs
        # gateway-reported totals.
        try:
            _msgs = payload.get("messages") or []
            _sys_chars = 0
            _hist_chars = 0
            for _i, _m in enumerate(_msgs):
                _c = _m.get("content") if isinstance(_m, dict) else None
                # content may be a str OR a list of blocks (after cache_control)
                if isinstance(_c, list):
                    _clen = sum(
                        len(_b.get("text", ""))
                        for _b in _c
                        if isinstance(_b, dict)
                    )
                else:
                    _clen = len(_c) if isinstance(_c, str) else 0
                if (
                    _i == 0
                    and isinstance(_m, dict)
                    and _m.get("role") == "system"
                ):
                    _sys_chars = _clen
                else:
                    _hist_chars += _clen
            _tools = payload.get("tools") or []
            _tools_json = json.dumps(_tools, ensure_ascii=False)
            _total_json = json.dumps(payload, ensure_ascii=False)
            _log.debug(
                "chat.diag.payload_breakdown",
                model_id=model_id,
                msg_count=len(_msgs),
                system_chars=_sys_chars,
                system_est_tokens=_sys_chars // 4,
                history_chars=_hist_chars,
                history_est_tokens=_hist_chars // 4,
                tools_count=len(_tools),
                tools_json_chars=len(_tools_json),
                tools_est_tokens=len(_tools_json) // 4,
                total_payload_chars=len(_total_json),
                total_est_tokens=len(_total_json) // 4,
            )
        except Exception:  # noqa: BLE001 — diagnostic only, never break a request
            pass

        return payload

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


def _default_client_factory(
    *, timeout: float, ssl_verify: bool = True
) -> httpx.AsyncClient:
    # V1 parity (chat_handler.py:1385): split connect/write/pool from the SSE
    # *read* timeout so a slow inter-token gap on a long cloud turn does not
    # raise ``httpx.ReadTimeout`` (surfaced as ``chat.llm.timeout``) and
    # truncate the reply.  ``timeout`` is the base (connect/write/pool); read
    # is scaled by ``_READ_TIMEOUT_FACTOR`` (default 120 * 5 = 600s), mirroring
    # the on-device adapter's long per-read timeout (local_model_stream.py:1055).
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, read=timeout * _READ_TIMEOUT_FACTOR),
        verify=ssl_verify,
    )


def _extract_chunk_text(event: dict[str, Any]) -> str:
    """Extract the text delta from one OpenAI SSE event.

    Handles the two shapes a proxy may emit on ``choices[0].delta.content``:

    1. **Plain string** (vanilla OpenAI / most providers)::

        {"choices": [{"delta": {"content": "..."}, "finish_reason": ...}]}

    2. **Anthropic-style content-block list** (e.g. claude-4 via
       cloud LLM service/Bedrock-OpenAI bridges that forward the upstream's typed
       blocks rather than flattening them)::

        {"choices": [{"delta": {"content": [
            {"type": "text", "text": "..."},
            {"type": "tool_use", ...},
            ...
        ]}}]}

    Without the list-form handling, every event whose ``content`` is a list
    silently returned ``""`` — the model's text was generated (counted in
    ``completion_tokens``) but NEVER reached ``state.assistant_text_parts`` →
    the final assistant message was truncated to whatever subset of events
    happened to use the string form (the reported "模型话没说完" bug).
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Concat the ``text`` fields of every ``{"type":"text",...}`` block in
        # order. Non-text blocks (tool_use / image / thinking / signature) are
        # ignored here: tool_use accumulates via ``_accumulate_tool_calls`` and
        # thinking surfaces via ``_extract_reasoning_text`` — this function is
        # ONLY the visible-answer-text extractor.
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _delta_has_tool_call(event: dict[str, Any]) -> bool:
    """Return True iff the SSE event carries any tool-call delta.

    Recognises two shapes (matches :func:`_accumulate_tool_calls`):

    1. **OpenAI**: ``choices[0].delta.tool_calls`` — non-empty list.
    2. **Anthropic-style list-form ``content``** (Claude-via-gateway /
       Bedrock-OpenAI bridges that forward typed content blocks rather than
       flattening to ``delta.tool_calls``)::

           {"choices":[{"delta":{"content":[
               {"type":"tool_use","id":"toolu_…","name":"…","input":{…}},
               {"type":"input_json_delta","partial_json":"…"}
           ]}}]}

       Either a ``tool_use`` start block or an ``input_json_delta``
       continuation counts as a tool-call delta — without recognising them
       here, ``_drain_round`` would mistake the round for "text-only" and
       trip the H-1 empty-completion path / no-progress retry. Symmetric to
       the fix in :func:`_extract_chunk_text` for ``delta.content`` lists.
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    if not isinstance(first, dict):
        return False
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return False
    tcs = delta.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        return True
    # Anthropic list-form content: look for tool_use / input_json_delta blocks.
    content = delta.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("tool_use", "input_json_delta"):
                return True
    return False


def _delta_has_reasoning(event: dict[str, Any]) -> bool:
    """Return True iff the SSE event carries a non-empty reasoning delta.

    Recognises three shapes (matches :func:`_extract_reasoning_text`):

    1. **DeepSeek-R1 / QwQ via OpenAI-compat**: ``delta.reasoning_content`` (str).
    2. **Anthropic thinking** (Claude-via-gateway / Bedrock-OpenAI bridges
       that forward typed blocks)::

           {"choices":[{"delta":{"content":[
               {"type":"thinking","thinking":"…"}
           ]}}]}

    3. **OpenAI o1/o3 list-form**::

           {"choices":[{"delta":{"content":[
               {"type":"reasoning","reasoning":"…"}
           ]}}]}

    V1 parity (chat_handler.py:1490): reasoning-model "thinking" deltas
    count as a *meaningful* chunk even though they are not surfaced as
    visible CHUNK text — otherwise a turn that streamed only reasoning
    before a tool call / empty stop would wrongly trip the H-1
    empty-response warning. Adding the list-form arms (2) and (3) closes
    the same gap fix #4 closed for visible text — symmetric root cause.
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    if not isinstance(first, dict):
        return False
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return False
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return True
    content = delta.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                if isinstance(block.get("thinking"), str) and block["thinking"]:
                    return True
            elif btype == "reasoning":
                if isinstance(block.get("reasoning"), str) and block["reasoning"]:
                    return True
            elif btype == "thinking_delta":
                # Anthropic streaming continuation: ``{"type":"thinking_delta",
                # "thinking":"…"}`` carries partial thinking text the same way
                # ``input_json_delta`` carries partial tool args.
                if isinstance(block.get("thinking"), str) and block["thinking"]:
                    return True
    return False


def _extract_reasoning_text(event: dict[str, Any]) -> str:
    """Extract the reasoning ("thinking") delta from one OpenAI SSE event.

    Handles three shapes a proxy may emit (symmetric to
    :func:`_extract_chunk_text` for visible text):

    1. **Plain string** (DeepSeek-R1 / QwQ via OpenAI-compat)::

           {"choices":[{"delta":{"reasoning_content":"…"}}]}

    2. **Anthropic-style content-block list** (Claude-via-gateway /
       Bedrock-OpenAI bridges that forward the upstream's typed blocks
       rather than flattening them to ``reasoning_content``)::

           {"choices":[{"delta":{"content":[
               {"type":"thinking","thinking":"…"},
               {"type":"thinking_delta","thinking":"…"}
           ]}}]}

    3. **OpenAI o1/o3 typed-block list**::

           {"choices":[{"delta":{"content":[
               {"type":"reasoning","reasoning":"…"}
           ]}}]}

    Without the list-form handling, every event whose reasoning lives in
    a typed block silently returned ``""`` → REASONING frame lost AND
    ``_delta_has_reasoning`` returned False → the H-1 empty-completion
    guard mis-fired for thinking-only rounds (the symmetric bug to fix
    #4's "模型话没说完").
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    content = delta.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("thinking", "thinking_delta"):
                text = block.get("thinking")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif btype == "reasoning":
                text = block.get("reasoning")
                if isinstance(text, str) and text:
                    parts.append(text)
        if parts:
            return "".join(parts)
    return ""



# Anthropic ``stop_reason`` → OpenAI ``finish_reason`` mapping.
# Some OpenAI-compatible bridges (Bedrock-OpenAI, cloud-gateway-via-Anthropic,
# direct Anthropic Compat shims) forward the upstream's native ``stop_reason``
# field without remapping it onto OpenAI's ``finish_reason``. Without this
# mapping, ``_drain_round`` interpreted a Claude turn that ended on a tool call
# (``stop_reason="tool_use"``) as a generic ``stop`` — bypassing the tool-call
# drain branch and triggering the H-1 empty-completion retry. Symmetric to the
# P1/P2 list-form fixes — same class of "bridge forwards Anthropic shape
# verbatim" gap.
_ANTHROPIC_STOP_REASON_MAP: dict[str, str] = {
    "tool_use": "tool_calls",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
}


def _get_finish_reason(event: dict[str, Any]) -> str | None:
    """Return the ``finish_reason`` string from a parsed SSE event, or None.

    Prefers the OpenAI-standard ``choices[0].finish_reason``. Falls back
    to Anthropic's ``stop_reason`` (read from either ``choices[0]`` or
    the top-level event — both have been observed in the wild on
    OpenAI-compat bridges that forward Claude / Bedrock verbatim) and
    maps it onto the OpenAI vocabulary via :data:`_ANTHROPIC_STOP_REASON_MAP`:

    * ``tool_use`` → ``tool_calls``
    * ``end_turn`` → ``stop``
    * ``stop_sequence`` → ``stop``
    * ``max_tokens`` → ``length``

    Unknown Anthropic stop_reason values are returned verbatim (forward
    compatibility — the downstream finaliser treats unknown reasons as
    "stop"). This is symmetric to the P1/P2 list-form fixes for
    ``delta.content`` — same root cause: an OpenAI-Compat bridge that
    forwards Anthropic shape without flattening it.
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        # Some bridges put ``stop_reason`` at the top level (no ``choices``
        # wrapper on the terminal event). Honour that shape too.
        top_reason = event.get("stop_reason")
        if isinstance(top_reason, str) and top_reason:
            return _ANTHROPIC_STOP_REASON_MAP.get(top_reason, top_reason)
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    reason = first.get("finish_reason")
    if isinstance(reason, str) and reason:
        return reason
    # Anthropic stop_reason fallback (mapped to OpenAI vocabulary).
    stop_reason = first.get("stop_reason")
    if not (isinstance(stop_reason, str) and stop_reason):
        stop_reason = event.get("stop_reason")
    if isinstance(stop_reason, str) and stop_reason:
        return _ANTHROPIC_STOP_REASON_MAP.get(stop_reason, stop_reason)
    return None


def _extract_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    """Extract token usage from an SSE event (OpenAI or Anthropic format).

    Returns a normalized dict with keys:
      prompt_tokens, completion_tokens, total_tokens,
      cache_read_tokens (optional),
      reasoning_tokens (optional, present only when > 0 — CCD-4)

    ``cache_read_tokens`` is used by the Anthropic-family effective-prompt
    computation (Phase A): Claude normally splits cache reads out of
    ``prompt_tokens``, so the running counter adds them back.  HOWEVER, when
    ``prompt_tokens`` is corrected to the authoritative ``total_tokens -
    completion_tokens - reasoning_tokens`` (cache hit/write turns where the
    gateway under-reports ``prompt_tokens``), the corrected value already
    contains the cached portion, so ``cache_read_tokens`` is zeroed to avoid
    double-counting.

    ``reasoning_tokens`` (CCD-4 from PENDING-WORK.md §1): OpenAI o1/o3 and
    Gemini "thinking" mode report hidden chain-of-thought tokens via
    ``completion_tokens_details.reasoning_tokens``. These are counted INSIDE
    ``completion_tokens`` AND added on top of ``completion_tokens`` inside
    ``total_tokens`` by some providers. The total-completion correction
    subtracts ``reasoning_tokens`` from the derivation so it does NOT inflate
    ``prompt_tokens`` by ``reasoning_tokens`` on a non-cache turn.

    Returns None if no usage block is present in the event.
    """
    raw = event.get("usage")
    if not isinstance(raw, dict):
        return None

    # --- OpenAI format ---
    # usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
    prompt_tokens: int | None = raw.get("prompt_tokens")
    completion_tokens: int | None = raw.get("completion_tokens")
    total_tokens: int | None = raw.get("total_tokens")

    # --- Anthropic format ---
    # usage.input_tokens, usage.output_tokens
    if prompt_tokens is None and "input_tokens" in raw:
        prompt_tokens = raw.get("input_tokens")
    if completion_tokens is None and "output_tokens" in raw:
        completion_tokens = raw.get("output_tokens")

    # --- Gemini / Vertex native format (usageMetadata) ---
    # GEMINI-USAGE-1 (token-accuracy audit): OpenAI-Compat gateways normally
    # translate Gemini's ``usageMetadata`` into the OpenAI ``usage`` shape read
    # above, so this branch is dormant on the gateway path. But a DIRECT-Vertex
    # or failed-translation path would surface the raw Gemini block, and without
    # this fallback ``prompt_tokens`` stays None → coerced to 0 below →
    # ``_maybe_estimate_usage`` silently downgrades to a chars/4 estimate instead
    # of using the AUTHORITATIVE provider count (violates State-Truth-First 铁律 1).
    # Pure-additive: only fills fields still unset by the OpenAI/Anthropic paths,
    # so the primary path is byte-for-byte unchanged. Gemini keys:
    #   promptTokenCount        → prompt_tokens
    #   candidatesTokenCount    → completion_tokens
    #   totalTokenCount         → total_tokens
    #   cachedContentTokenCount → prompt_tokens_details.cached_tokens (folded INTO
    #                             promptTokenCount, mirroring OpenAI cached_tokens —
    #                             NOT the Anthropic split-out cache_read semantics)
    #   thoughtsTokenCount      → completion_tokens_details.reasoning_tokens
    #                             (mirror o1/o3 so budget/display already handle it)
    # We fold cache/thoughts into the OpenAI-shape nested dicts so the EXISTING
    # downstream reasoning/cache machinery below picks them up (no new consumer).
    if prompt_tokens is None and "promptTokenCount" in raw:
        try:
            prompt_tokens = int(raw.get("promptTokenCount") or 0)
        except (TypeError, ValueError):
            prompt_tokens = None
    if completion_tokens is None and "candidatesTokenCount" in raw:
        try:
            completion_tokens = int(raw.get("candidatesTokenCount") or 0)
        except (TypeError, ValueError):
            completion_tokens = None
    if total_tokens is None and "totalTokenCount" in raw:
        try:
            total_tokens = int(raw.get("totalTokenCount") or 0)
        except (TypeError, ValueError):
            total_tokens = None
    if "cachedContentTokenCount" in raw and not isinstance(
        raw.get("prompt_tokens_details"), dict
    ):
        try:
            _gem_cached = int(raw.get("cachedContentTokenCount") or 0)
        except (TypeError, ValueError):
            _gem_cached = 0
        if _gem_cached > 0:
            # Fold into OpenAI-shape so the existing prompt_tokens_details reader
            # surfaces it as cached_tokens (Gemini explicit cache is INSIDE
            # promptTokenCount, exactly like OpenAI cached_tokens).
            raw = {**raw, "prompt_tokens_details": {"cached_tokens": _gem_cached}}
    if "thoughtsTokenCount" in raw and not isinstance(
        raw.get("completion_tokens_details"), dict
    ):
        try:
            _gem_thoughts = int(raw.get("thoughtsTokenCount") or 0)
        except (TypeError, ValueError):
            _gem_thoughts = 0
        if _gem_thoughts > 0:
            # Fold into OpenAI-shape so the existing reasoning_tokens reader
            # surfaces it (mirror o1/o3 completion_tokens_details.reasoning_tokens).
            raw = {**raw, "completion_tokens_details": {"reasoning_tokens": _gem_thoughts}}

    # Coerce to int (some providers send None for fields)
    prompt_tokens = int(prompt_tokens) if prompt_tokens is not None else 0
    completion_tokens = int(completion_tokens) if completion_tokens is not None else 0

    # Compute total if missing
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    else:
        total_tokens = int(total_tokens)

    # --- Reasoning tokens (OpenAI o1/o3 family + Gemini "thinking" mode) ---
    # ``completion_tokens_details.reasoning_tokens`` is the portion of
    # ``completion_tokens`` that the provider already counts INSIDE
    # ``completion_tokens`` but charges as a separate concept (hidden chain-of-
    # thought, billed but not surfaced). Some o1/o3-style providers report
    # ``total_tokens = prompt_tokens + completion_tokens + reasoning_tokens``
    # — i.e. reasoning is ADDED ON TOP of completion in ``total`` even though
    # it is also counted INSIDE completion. Without subtracting it from
    # ``total`` here, ``derived = total - completion`` would over-state the
    # real prompt by ``reasoning_tokens`` (the total-comp correction below
    # would then falsely flag the wire as a "cache-hit" and pump the badge).
    # We subtract it from the derivation only; we do NOT modify the surfaced
    # ``completion_tokens`` (the streaming counter already used that value).
    # CCD-4 (PENDING-WORK.md §1): see test_extract_usage_reasoning_tokens.py.
    reasoning_tokens: int = 0
    completion_details = raw.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        try:
            reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            reasoning_tokens = 0

    # ``total - completion - reasoning`` is the AUTHORITATIVE source of the
    # real prompt size. OpenAI-compatible gateways (e.g. cloud LLM service) report
    # Anthropic prompt-cache hit/write turns with ``prompt_tokens`` holding
    # ONLY the non-cached delta (e.g. 2) while the TRUE prompt hides in
    # ``total_tokens`` (the cached volume is recorded in
    # ``prompt_tokens_details.cache_write_tokens`` and a ``cache_read_tokens``
    # field is not guaranteed to be present). The derived value is correct for
    # cache hit / write / miss, and for OpenAI/Azure (prompt_tokens already
    # accurate → total-comp == prompt, so ``max`` is a no-op). When
    # ``total_tokens`` was inferred above (total = prompt + comp) the derived
    # value == prompt_tokens → ``max`` leaves it unchanged (harmless fallback).
    # ``reasoning_tokens`` subtraction (CCD-4) prevents over-correction on o1/o3
    # providers where ``total`` already includes reasoning on top of completion.
    _derived_prompt = total_tokens - completion_tokens - reasoning_tokens
    _prompt_corrected = False
    if _derived_prompt > prompt_tokens:
        prompt_tokens = _derived_prompt
        _prompt_corrected = True

    # --- Cache tokens (OpenAI extended format) ---
    cache_read_tokens: int | None = None

    # OpenAI: usage.prompt_tokens_details.cached_tokens
    prompt_details = raw.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached = prompt_details.get("cached_tokens")
        if cached is None:
            # Claude via the internal LLM gateway nests cache reads as
            # ``prompt_tokens_details: {cache_read_tokens: N}`` instead of the
            # OpenAI ``cached_tokens`` key — read it here so the running
            # full-history counter sees Claude's split-out cache reads.
            cached = prompt_details.get("cache_read_tokens")
        if cached is not None:
            cache_read_tokens = int(cached)

    # Some providers surface these at usage top level
    if cache_read_tokens is None:
        cr = raw.get("cache_read_tokens")
        if cr is not None:
            cache_read_tokens = int(cr)

    # Anthropic: usage.cache_read_input_tokens
    if cache_read_tokens is None:
        cr = raw.get("cache_read_input_tokens")
        if cr is not None:
            cache_read_tokens = int(cr)

    # --- Display-only cache-WRITE observation (raw-based) --------------------
    # DISPLAY-CACHE-WRITE (token badge ↑): the ``adjustedInput``
    # = ``input − cache_read − cache_write``. On a prompt-cache WRITE turn the
    # freshly-cached prefix lives in ``cache_write_tokens`` /
    # ``cache_creation_input_tokens`` and, when the corrected branch fires, is
    # already FOLDED into ``prompt_tokens`` (the full wire). To let the UI show
    # the true NON-cached增量 (input − read − write) we OBSERVE the write volume
    # from RAW here — BEFORE the corrected branch may drop
    # ``prompt_tokens_details`` — and surface it as a SEPARATE
    # ``cache_write_observed`` field that eff-prompt / counter math NEVER reads
    # (mirrors ``cache_read_observed``). Best-effort; only emitted when > 0 so
    # the majority no-write path stays byte-for-byte identical.
    cache_write_observed: int | None = None
    if isinstance(prompt_details, dict):
        _cw_raw = prompt_details.get("cache_write_tokens")
        if _cw_raw is None:
            _cw_raw = prompt_details.get("cache_creation_input_tokens")
        if isinstance(_cw_raw, (int, float)) and not isinstance(_cw_raw, bool):
            cache_write_observed = int(_cw_raw)
    if cache_write_observed is None:
        for _cwk in ("cache_write_tokens", "cache_creation_input_tokens"):
            _cw_top = raw.get(_cwk)
            if isinstance(_cw_top, (int, float)) and not isinstance(_cw_top, bool):
                cache_write_observed = int(_cw_top)
                break

    # --- 方案B: gateway prompt-cache-support signal (raw-based) ---------------
    # Whether the gateway ECHOED BACK any Anthropic prompt-cache field is a
    # GATEWAY capability signal — a gateway that supports prompt caching surfaces
    # cache accounting; one that doesn't never mentions cache. We derive this
    # boolean from the RAW usage block HERE, BEFORE the ``_prompt_corrected``
    # branch below zeros ``cache_read_tokens`` (which would otherwise make an
    # aicegrok cache-hit indistinguishable from a no-cache internal gateway turn — the坑
    # noted in the plan). Signal fields (any present + non-None ⇒ True):
    #   * prompt_tokens_details.cached_tokens / .cache_read_tokens
    #     / .cache_write_tokens
    #   * top-level cache_read_tokens / cache_read_input_tokens
    #     / cache_write_tokens / cache_creation_input_tokens
    # This is a pure OBSERVATION of raw (归零前), never affected by the
    # eff-prompt correction, and is surfaced as a tail-appended boolean so the
    # ProviderCacheCapabilityRegistry can learn the gateway's cache support.
    _reported_cache = False
    if isinstance(prompt_details, dict):
        for _k in ("cached_tokens", "cache_read_tokens", "cache_write_tokens"):
            if prompt_details.get(_k) is not None:
                _reported_cache = True
                break
    if not _reported_cache:
        for _k in (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cache_write_tokens",
            "cache_creation_input_tokens",
        ):
            if raw.get(_k) is not None:
                _reported_cache = True
                break

    # When ``prompt_tokens`` was corrected to the authoritative ``total -
    # completion`` above, it ALREADY contains the cached portion (the true
    # wire size). Downstream consumers (``assistant_eff_prompt``,
    # ``agent_tool`` sub-agent口径) add ``cache_read_tokens`` back ONLY for the
    # Anthropic family because Claude normally splits cache reads OUT of
    # ``prompt_tokens``. After this correction that split no longer holds, so
    # surfacing a non-zero ``cache_read_tokens`` would double-count it. Zero it
    # to keep the single-source-of-truth: the corrected ``prompt_tokens`` is
    # the whole real prompt. (When NOT corrected — total-comp == prompt, the
    # genuine cache-split case — ``cache_read_tokens`` is preserved so the
    # add-back stays correct.)
    #
    # 改动3 (observe-only cache visibility): zeroing ``cache_read_tokens`` in
    # the corrected branch keeps the eff-prompt accounting single-sourced (no
    # double-add) BUT makes the actual Anthropic cache-hit volume completely
    # invisible — which defeats verifying that prompt caching is working. We
    # therefore STASH the observed cache-read value into a SEPARATE
    # ``cache_read_observed`` field that downstream eff-prompt math NEVER reads
    # (it is not ``cache_read_tokens`` and not in ``prompt_tokens_details``), so
    # the running accounting stays byte-for-byte identical while the cache hit
    # becomes observable (e.g. a /context badge or diag log). Best-effort: only
    # set when the provider actually reported a positive cache-read.
    cache_read_observed: int | None = None
    if _prompt_corrected:
        if isinstance(cache_read_tokens, int) and cache_read_tokens > 0:
            cache_read_observed = cache_read_tokens
        cache_read_tokens = None

    # DIAG (token-display investigation): dump the RAW provider usage block and
    # the normalized result so we can confirm exactly what the gateway returns
    # for Claude prompt-cache-hit/write turns. Now also surfaces whether the
    # total-completion correction fired (``prompt_corrected``) and the
    # ``reasoning_tokens`` extracted (CCD-4). Remove once root-caused.
    _log.info(
        "chat.diag.extract_usage",
        raw_usage=raw,
        raw_keys=sorted(raw.keys()),
        out_prompt_tokens=prompt_tokens,
        out_completion_tokens=completion_tokens,
        out_total_tokens=total_tokens,
        out_cache_read_tokens=cache_read_tokens,
        out_cache_read_observed=cache_read_observed,
        out_reasoning_tokens=reasoning_tokens,
        derived_prompt=_derived_prompt,
        prompt_corrected=_prompt_corrected,
        total_minus_completion=total_tokens - completion_tokens,
    )

    result: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
    }
    # 方案B: tail-appended gateway prompt-cache-support signal (raw-based, set
    # BEFORE the corrected-branch zeroing above, so it survives). Downstream the
    # ProviderCacheCapabilityRegistry reads this to learn whether the gateway
    # echoes cache accounting → decides tool-output aging on/off per gateway.
    # Emitted ONLY when True (gateway reported a cache field), mirroring the
    # ``reasoning_tokens`` / ``cache_read_observed`` convention: the majority
    # no-cache path keeps the output dict byte-for-byte identical to the prior
    # behaviour (key simply ABSENT). The round-end readers treat a real usage
    # dict WITHOUT this key as "gateway did NOT report cache" (mark no-cache) —
    # ``_extract_usage`` always computes the signal, so absent ⇔ False on the
    # real path, while legacy hand-built stub usage dicts stay unaffected.
    if _reported_cache:
        result["provider_reported_cache"] = True
    # 改动3: surface the observe-only cache-read volume when the corrected
    # branch zeroed ``cache_read_tokens`` (so eff-prompt math is unaffected) —
    # ONLY when positive, so the majority path (no correction / no cache) keeps
    # existing consumers byte-for-byte equivalent (key simply absent).
    if cache_read_observed is not None and cache_read_observed > 0:
        result["cache_read_observed"] = cache_read_observed
    # DISPLAY-CACHE-WRITE: surface the observe-only cache-WRITE volume (raw-based,
    # set BEFORE any corrected-branch drop of ``prompt_tokens_details``) so the
    # token badge can subtract it for the ↑ display (input − read − write).
    # eff-prompt / full-history counter NEVER read this key. Emitted ONLY when
    # positive → majority no-write path keeps the dict byte-for-byte identical.
    if cache_write_observed is not None and cache_write_observed > 0:
        result["cache_write_observed"] = cache_write_observed
    # Surface ``reasoning_tokens`` when present (>0) so downstream consumers
    # (turn-finalize accumulator, /context badge debug) can track it
    # explicitly. Absent / zero is the cloud-providers majority path; not
    # emitting the key there keeps existing consumers byte-for-byte equivalent.
    if reasoning_tokens > 0:
        result["reasoning_tokens"] = reasoning_tokens
    # Edge-case rescue (State-Truth-First 铁律 1): when the total-completion
    # correction did NOT fire (``_prompt_corrected=False`` — e.g. a provider
    # that omitted ``total_tokens`` so the recovery at the ``_derived_prompt >
    # prompt_tokens`` guard could not run), ``prompt_tokens`` may still be the
    # tiny non-cached Claude delta (e.g. 2) with the real volume living in
    # ``prompt_tokens_details.cache_write_tokens``. Downstream consumers
    # (``agent_tool._on_round_end`` autonomous path AND the take-over persist
    # path in ``streaming._persist_subagent_takeover``) have a cache_write
    # fallback that recovers the true wire size — but ONLY if they can still
    # SEE the details block. So surface ``prompt_tokens_details`` here in the
    # un-corrected case. When the correction DID fire, ``prompt_tokens`` already
    # holds the full wire (cache included) and ``cache_read_tokens`` was zeroed
    # to prevent double-counting — so we deliberately OMIT details then (adding
    # it back would let the fallback double-add the cached volume).
    if not _prompt_corrected and isinstance(prompt_details, dict):
        _cw = prompt_details.get("cache_write_tokens")
        _cr_d = prompt_details.get("cache_read_tokens")
        _preserved = {
            k: int(v)
            for k, v in (
                ("cache_write_tokens", _cw),
                ("cache_read_tokens", _cr_d),
            )
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if _preserved:
            result["prompt_tokens_details"] = _preserved
    return result


def _accumulate_tool_calls(
    event: dict[str, Any],
    accumulated: dict[int, dict[str, Any]],
) -> None:
    """Merge tool_call deltas from *event* into *accumulated*.

    Each SSE chunk may carry partial tool_call data in
    ``choices[0].delta.tool_calls`` — an array of objects each with an
    ``index`` key.  The first chunk for a given index carries ``id``,
    ``type``, and ``function.name``; subsequent chunks append to
    ``function.arguments``.

    PR-090 / S9 C-1: Vertex AI thinking models stream a
    ``thought_signature`` field on tool_call deltas that the next turn
    must echo back verbatim — otherwise the API returns 400 ("content
    block is missing a thought_signature").  We persist the latest
    non-empty signature observed for each tool_call onto the assembled
    entry so :class:`StreamChatUseCase` can carry it into the next
    history turn.

    **Anthropic list-form ``delta.content`` (symmetric to the fix in**
    :func:`_extract_chunk_text` **— same root cause as the "模型话没说完"
    bug for tool_use)**: some OpenAI-compatible bridges that wrap Claude /
    Bedrock pass the upstream's typed content blocks straight through
    instead of flattening them onto ``delta.tool_calls``::

        {"choices":[{"delta":{"content":[
            {"type":"tool_use","id":"toolu_…","name":"read","input":{…}},
            {"type":"input_json_delta","partial_json":"\\"path\\":\\"/a\\""}
        ]}}]}

    Without recognising these, the bridge's tool calls were generated by
    the model (counted in ``completion_tokens``) but NEVER accumulated →
    ``_drain_tool_calls`` saw an empty dict → the round looked
    "text-only", a no-tool-call ``finish_reason`` from the bridge then
    tripped the H-1 empty-completion guard and synthesized a useless
    retry. The handling below maps both block types into the SAME
    ``accumulated[index] = {"id","type":"function","function":{"name",
    "arguments"}}`` dict shape the OpenAI path produces, so
    ``_drain_tool_calls`` / ``_emit_args_progress`` consume it
    UNCHANGED.

    Indexing for list-form blocks: prefer the block's own ``index``
    field (Anthropic native SSE carries it on ``content_block_start`` /
    ``content_block_delta``). Absent that, fall back to the block's
    position within the event's ``delta.content`` list, shifted by a
    large ``_ANTHROPIC_INDEX_BASE`` offset to never collide with OpenAI
    integer indices a mixed proxy might also stream. A continuation
    ``input_json_delta`` whose owner index is not yet recorded is
    attached to the latest in-progress entry (so a corrupt-but-stable
    stream still accumulates).
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    first = choices[0]
    if not isinstance(first, dict):
        return
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return

    # --- 1. OpenAI form: delta.tool_calls -------------------------------
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc_delta in tool_calls:
            if not isinstance(tc_delta, dict):
                continue
            idx = tc_delta.get("index")
            if not isinstance(idx, int):
                continue

            if idx not in accumulated:
                accumulated[idx] = {
                    "id": tc_delta.get("id", ""),
                    "type": tc_delta.get("type", "function"),
                    "function": {
                        "name": "",
                        "arguments": "",
                    },
                }

            entry = accumulated[idx]

            # Update id/type if present (first chunk for this index).
            if tc_delta.get("id"):
                entry["id"] = tc_delta["id"]
            if tc_delta.get("type"):
                entry["type"] = tc_delta["type"]

            # Accumulate function name and arguments.
            fn_delta = tc_delta.get("function")
            if isinstance(fn_delta, dict):
                if fn_delta.get("name"):
                    entry["function"]["name"] = fn_delta["name"]
                if fn_delta.get("arguments"):
                    entry["function"]["arguments"] += fn_delta["arguments"]

            # PR-090 / S9 C-1 — Vertex AI thought_signature capture.
            # Persist the latest non-empty signature; some providers stream
            # it on the first chunk only, others send it repeatedly.
            sig = tc_delta.get("thought_signature")
            if sig:
                entry["thought_signature"] = sig

    # --- 2. Anthropic list-form: delta.content -------------------------
    content = delta.get("content")
    if not isinstance(content, list):
        return
    for pos, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            # Start-of-block: id + name (+ optional initial input dict).
            block_idx = block.get("index")
            if not isinstance(block_idx, int):
                block_idx = _ANTHROPIC_INDEX_BASE + pos
            if block_idx not in accumulated:
                accumulated[block_idx] = {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": "",
                    },
                }
            entry = accumulated[block_idx]
            if block.get("id"):
                entry["id"] = str(block["id"])
            if block.get("name"):
                entry["function"]["name"] = str(block["name"])
            # ``input`` (when present on the start block) is the model's
            # initial arguments object — serialize to the same JSON-string
            # shape the OpenAI path produces so downstream consumers
            # (json.loads of ``function.arguments``) stay identical.
            initial_input = block.get("input")
            if isinstance(initial_input, dict) and initial_input:
                try:
                    entry["function"]["arguments"] = json.dumps(
                        initial_input, ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    pass
            sig = block.get("thought_signature")
            if sig:
                entry["thought_signature"] = sig
        elif btype == "input_json_delta":
            # Continuation: append the partial JSON fragment to the owner's
            # ``function.arguments``. Owner is identified by the block's
            # ``index`` when present, else the position-derived key (same
            # offset rule as the start block), else the most recent
            # in-progress entry (last-resort, keeps a stable-but-untagged
            # stream accumulating instead of silently dropping).
            partial = block.get("partial_json")
            if not isinstance(partial, str) or not partial:
                continue
            block_idx = block.get("index")
            if not isinstance(block_idx, int):
                block_idx = _ANTHROPIC_INDEX_BASE + pos
            if block_idx not in accumulated:
                # Owner not known yet: attach to the latest Anthropic-keyed
                # entry (the start block usually arrived in an earlier event
                # in the same stream).
                anthropic_keys = [
                    k for k in accumulated
                    if isinstance(k, int) and k >= _ANTHROPIC_INDEX_BASE
                ]
                if not anthropic_keys:
                    continue
                block_idx = max(anthropic_keys)
            entry = accumulated[block_idx]
            entry.setdefault("function", {"name": "", "arguments": ""})
            entry["function"]["arguments"] += partial
        # Other block types (text / thinking / reasoning / image) are not
        # tool-call related and are handled by their own extractors.


# Alias matching the manifest contract name.
# :class:`HttpOpenAICompatibleLLMStream` is the production OpenAI-
# compatible single-provider implementation; the
# ``MultiProviderLLMStream`` name is the documented type that
# :class:`ChatServices.llm` exposes, so the alias keeps the public
# namespace stable while the provider-routing concern is layered at
# the configuration boundary (``Settings.llm_provider`` selects the
# concrete client constructed in :mod:`apps.api._chat_di`).
MultiProviderLLMStream = HttpOpenAICompatibleLLMStream
