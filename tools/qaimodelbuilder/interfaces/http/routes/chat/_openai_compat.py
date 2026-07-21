# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""OpenAI-compatible HTTP routes — PR-033 stage D.

External contract per ``08-business-capabilities.md`` §8.2: these
3 routes are the **only** Clean-Cutover exception that must be
1:1-preserved (path, method, request body, response body) so that
third-party clients (e.g. the OpenAI Python SDK) keep working.

Routes (mounted under ``/v1`` — NOT under ``/api/chat``):

* ``GET  /v1/models``           — list of installed model identifiers
* ``GET  /v1/models/{id:path}`` — single model record
* ``POST /v1/chat/completions`` — non-streaming + streaming completion

Streaming response uses Server-Sent Events with the OpenAI shape
(``data: <json>\\n\\n`` then a final ``data: [DONE]\\n\\n``); this
deliberately differs from the QAI-native SSE frame contract in
``_sse.py`` because OpenAI clients hard-code their own parser.

Coordination notes (also captured in PR-033 manifest §11):

* ``/v1/embeddings`` is listed in the spec deliverable list but the
  inventory ``02-routes.md`` §3.7 only enumerates 3 routes
  (``/v1/models``, ``/v1/models/{id:path}``, ``/v1/chat/completions``).
  The chat application layer also has no embeddings use case, so an
  embeddings adapter would need a brand-new use case in another
  context. Recorded as a coordination request — NOT shipped here.

* ``/v1/models`` is served from the apps-layer
  :class:`apps.api.openai_compat_ports.OpenAIModelListerPort`
  (``container.chat.openai_model_lister``), which merges the
  ``model_catalog`` listing with the chat-context providers without a
  cross-context import (forbidden by the ``context-isolation``
  import-linter contract).  The old static ``_fake_model_listing`` stub
  was retired once that port was wired (R13 dealign cleanup).

* The non-streaming ``/v1/chat/completions`` path delegates the
  chunk-aggregation (collect every CHUNK frame's text into one
  completion body) to the application layer
  (:meth:`qai.chat.application.use_cases.streaming.StreamChatUseCase.collect_completion`)
  rather than looping over frames in the route — keeping the interface
  thin (R13 dealign). PR-D1 (F-10) extended ``collect_completion`` to
  also surface real token usage off the terminal END frame.

* PR-D1 (F-11) — sampling parameters (``top_p`` / ``n`` /
  ``temperature`` / ``max_tokens`` / ``frequency_penalty`` /
  ``presence_penalty`` / ``stop`` / ``seed``) and tool advertisement
  (``tools`` / ``tool_choice``) are forwarded to the LLM by stuffing
  them into ``StreamChatInput.extra`` — the chat use case → LLM stream
  pipeline already drains ``extra`` onto the wire payload
  (``llm_stream._build_payload`` L789 ``resolve_params`` + L851 generic
  forward loop). Multi-turn ``messages`` history is replayed onto a
  reused conversation; ``conversation_id`` (when supplied by the client
  via the OpenAI extension field) lets the same /v1 caller resume an
  existing chat conversation instead of always provisioning a fresh
  one.

* PR-D1 (F-12) — streaming bridge now folds ``ERROR`` frames into a
  ``data: {"error": ...}`` line + ``[DONE]`` (matching the open-stream
  exception path) and ``TOOL_CALL`` frames into OpenAI ``tool_calls``
  delta chunks so OpenAI SDK clients see the model's tool-use intent
  rather than silently swallowed frames.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from qai.chat.application.use_cases.streaming import StreamChatInput
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.domain.tab import ConversationTab
from qai.platform.errors import ForbiddenError, NotFoundError, QaiError
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


logger = get_logger(__name__)


# ---- /v1 schema (OpenAI-compatible) --------------------------------------


class _OpenAIChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    # §G field-type widening (1-H1): the OpenAI message ``content`` is
    # ``str`` for plain-text turns but a ``list[dict]`` of typed content
    # parts for multimodal turns (image_url / input_audio / text parts —
    # V1 ``openai_api_routes.py:283`` transparently proxied both). We
    # accept the union so multimodal requests no longer 422; existing
    # ``str`` callers are unaffected. The list form is forwarded verbatim
    # onto the wire payload (``extra["messages"]``) so providers that
    # understand multimodal parts keep working, and degraded to a flat
    # text prompt internally via :func:`_content_to_text` where a single
    # ``str`` prompt is required (the QAI-native chat path is text-first).
    content: str | list[dict[str, Any]]
    # ``name`` / ``tool_call_id`` are part of the OpenAI message schema
    # for tool-result turns; we accept them so multi-turn tool histories
    # round-trip cleanly. They are forwarded onto the LLM wire payload
    # via ``extra["messages"]`` (PR-D1 F-11).
    name: str | None = None
    tool_call_id: str | None = None


def _content_to_text(content: str | list[dict[str, Any]]) -> str:
    """Flatten an OpenAI message ``content`` into a plain-text string.

    1-H1: multimodal ``content`` is a list of typed parts. The QAI-native
    chat path needs a single text prompt, so we concatenate the ``text``
    of every ``{"type": "text", "text": ...}`` part (the standard OpenAI
    multimodal text-part shape). Non-text parts (image_url / input_audio)
    are dropped from the *internal* prompt — the full structured content
    is still forwarded verbatim to providers via ``extra["messages"]``.
    A plain ``str`` content is returned unchanged.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


class OpenAIChatCompletionRequest(BaseModel):
    """``POST /v1/chat/completions`` body — OpenAI-compatible subset.

    PR-D1 (F-11) extended the accepted-knob set to include sampling
    parameters (``top_p`` / ``n`` / ``frequency_penalty`` /
    ``presence_penalty`` / ``stop`` / ``seed``), tool advertisement
    (``tools`` / ``tool_choice``), ``response_format``, and the
    OpenAI-extension ``conversation_id`` that lets a client resume an
    existing QAI conversation across stateless ``/v1`` calls. Unknown
    future knobs are still silently ignored (``extra="ignore"``).
    """

    model: str = Field(..., min_length=1, max_length=256)
    messages: list[_OpenAIChatMessage] = Field(..., min_length=1)
    stream: bool = False
    # Sampling parameters (forwarded into ``StreamChatInput.extra`` —
    # the LLM stream resolver clamps / filters them per model profile).
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = None
    max_tokens: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: list[str] | str | None = None
    seed: int | None = None
    # Tool advertisement (forwarded as ``tools_schemas`` /
    # ``tool_choice`` — the LLM stream emits the OpenAI standard
    # ``payload["tools"]`` array on the cloud path; see
    # ``llm_stream._build_payload`` L803-845).
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    # OpenAI extension: client-provided conversation id lets the same
    # caller resume an existing QAI conversation. Absent / unknown id =>
    # provision a fresh one (current stateless behaviour).
    conversation_id: str | None = None

    model_config = {"extra": "ignore"}


class OpenAIModelEntry(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "qai"


class OpenAIModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[OpenAIModelEntry]


# ---- Helpers --------------------------------------------------------------


def _completion_chunk(
    *,
    model: str,
    completion_id: str,
    content: str,
    finish: bool,
    delta_overrides: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one OpenAI-style ``chat.completion.chunk`` dict.

    ``delta_overrides`` lets callers ship a ``tool_calls`` delta (PR-D1
    F-12) instead of the default ``{"content": ...}`` shape. ``usage``
    is appended to the terminal stop chunk (OpenAI streaming carries
    per-call usage in the final chunk when ``stream_options.include_usage``
    is on; the QAI-native pipeline already includes usage on the END
    frame — F-10).
    """
    delta: dict[str, Any] = {"content": content}
    if delta_overrides is not None:
        delta = delta_overrides
    chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": "stop" if finish else None,
            }
        ],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _normalize_usage_for_openai(usage: dict[str, Any]) -> dict[str, Any]:
    """Map QAIModelBuilder's internal usage dict to a standard OpenAI usage
    block for third-party clients (openai SDK / Cline / LangChain).

    Internal ``_extract_usage`` (llm_stream.py) zeroes the standard
    ``cache_read_tokens`` in its correction branch and stashes the real
    cached amount in PRIVATE observe-only fields (``cache_read_observed`` /
    ``cache_write_observed``) that third-party SDKs do not understand. Without
    re-surfacing them as the OpenAI-standard ``prompt_tokens_details.cached_tokens``,
    a client billing against our proxy cannot apply the cache-hit discount and
    over-counts cost. This maps them back to the standard shape WITHOUT touching
    internal billing (which keeps reading its own fields).

    Returns a NEW dict; does not mutate the input.
    """
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    out: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    # Re-surface cache read (prefer the observe-only real value, fall back to the
    # standard field when the correction branch did not fire / non-cache path).
    cache_read = int(
        usage.get("cache_read_observed")
        or usage.get("cache_read_tokens")
        or 0
    )
    cache_write = int(usage.get("cache_write_observed") or 0)
    if cache_read > 0 or cache_write > 0:
        details: dict[str, Any] = {}
        if cache_read > 0:
            details["cached_tokens"] = cache_read
        # Anthropic-style extra keys some clients read; harmless additions.
        if cache_read > 0:
            details["cache_read_input_tokens"] = cache_read
        if cache_write > 0:
            details["cache_creation_input_tokens"] = cache_write
        out["prompt_tokens_details"] = details
    # Preserve any other already-standard/optional fields (reasoning_tokens etc.),
    # but do NOT leak internal private keys to third-party clients.
    _PRIVATE_KEYS = {
        "cache_read_observed", "cache_write_observed", "provider_reported_cache",
        "cache_read_tokens", "cache_write_tokens",
        "last_round_prompt_tokens", "last_round_cache_read_tokens",
        "last_round_cache_read_display", "last_round_cache_write_display",
        "first_round_prompt_tokens", "first_round_cache_read_display",
        "first_round_cache_write_display",
    }
    for k, v in usage.items():
        if k in out or k in _PRIVATE_KEYS or k == "prompt_tokens_details":
            continue
        out[k] = v
    return out


def _final_completion(
    *,
    model: str,
    completion_id: str,
    content: str,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a non-streaming ``chat.completion`` dict.

    PR-D1 (F-10): when the underlying turn reported real token usage
    (captured off the terminal END frame by
    :meth:`StreamChatUseCase.collect_completion`), we surface those
    counts; otherwise we omit ``prompt_tokens`` (set to 0) and report
    only the assistant content's character length as a coarse fallback
    so downstream OpenAI SDK clients still see the expected envelope.
    """
    if usage is not None:
        # Normalize QAI's internal usage (private cache_*_observed / *_display
        # fields) into the OpenAI-standard shape (prompt_tokens_details.
        # cached_tokens) for third-party clients; internal billing is untouched
        # because it keeps reading its own fields upstream. Single source of
        # truth shared with the streaming exit.
        usage_block: dict[str, Any] = _normalize_usage_for_openai(usage)
    else:
        # Fallback (offline LLM / unknown adapter): coarse character
        # count so the envelope shape is stable and clients don't
        # explode. NOT a real token count.
        usage_block = {
            "prompt_tokens": 0,
            "completion_tokens": len(content),
            "total_tokens": len(content),
        }
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage_block,
    }


def _line(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _done_line() -> bytes:
    return b"data: [DONE]\n\n"


def _PASSTHROUGH_HEADER_PREFIXES() -> tuple[str, ...]:
    # 1-L1: provider-specific header prefixes V1 forwarded transparently
    # (``openai_api_routes.py:338``). ``anthropic-`` (e.g. anthropic-beta),
    # ``openai-`` (e.g. openai-organization), and the generic ``x-`` family
    # (e.g. x-api-version for Azure). Credential headers are NOT logged.
    return ("x-", "anthropic-", "openai-")


# Hop-by-hop / framing headers that must never be forwarded onto the
# upstream provider request (V1 ``openai_api_routes.py:339``).
_SKIP_PASSTHROUGH_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "authorization",
        "content-type",
        "accept",
        # QAI-internal auth / CSRF — never leak to the upstream provider.
        "cookie",
        "x-qai-csrf",
    }
)


def _collect_passthrough_headers(headers: Any) -> dict[str, str]:
    """Filter request headers down to the provider-passthrough allow-list.

    1-L1 parity (``openai_api_routes.py:338-346``): forward ``x-`` /
    ``anthropic-`` / ``openai-`` prefixed headers (minus hop-by-hop /
    credential / framing headers) so callers can pass provider-specific
    knobs (Azure ``api-version``, ``anthropic-beta``, etc.) through the
    OpenAI-compatible surface. Returns an empty dict when none match so
    callers keep ``extra`` minimal.
    """
    prefixes = _PASSTHROUGH_HEADER_PREFIXES()
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:
        return out
    for key, value in items:
        k_lower = str(key).lower()
        if k_lower in _SKIP_PASSTHROUGH_HEADERS:
            continue
        if any(k_lower.startswith(p) for p in prefixes):
            out[str(key)] = str(value)
    return out


def _build_extra_from_request(
    body: OpenAIChatCompletionRequest,
    *,
    passthrough_headers: dict[str, str] | None = None,
    unknown_fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Pack OpenAI sampling/tool fields into ``StreamChatInput.extra``.

    PR-D1 (F-11): the chat use case → LLM stream pipeline already
    forwards ``extra`` keys onto the wire payload (sampling tunables
    via ``resolve_params`` per :data:`TUNABLE_KEYS`; tool advertisement
    via ``extra["tools_schemas"]`` + a generic forward loop for any
    other recognised OpenAI top-level key like ``tool_choice`` /
    ``response_format``). This helper is the only translation point.

    Returns ``None`` when the request carries no forwardable knobs so
    callers can keep ``extra`` unset (matching previous behaviour for
    minimal /v1 calls).
    """
    extra: dict[str, Any] = {}
    if body.temperature is not None:
        extra["temperature"] = body.temperature
    if body.top_p is not None:
        extra["top_p"] = body.top_p
    if body.max_tokens is not None:
        extra["max_tokens"] = body.max_tokens
    if body.frequency_penalty is not None:
        extra["frequency_penalty"] = body.frequency_penalty
    if body.presence_penalty is not None:
        extra["presence_penalty"] = body.presence_penalty
    if body.stop is not None:
        extra["stop"] = body.stop
    if body.seed is not None:
        extra["seed"] = body.seed
    if body.n is not None:
        # ``n`` is not in TUNABLE_KEYS; the generic forward loop in
        # ``llm_stream._build_payload`` will pass it onto the wire (most
        # OpenAI-compatible providers honour it, others ignore unknown
        # fields silently).
        extra["n"] = body.n
    if body.tools is not None:
        # Mirror the chat use case's existing ``tools_schemas`` channel:
        # ``llm_stream._build_payload`` L825 reads it and emits
        # ``payload["tools"]`` + ``tool_choice="auto"`` on the cloud path.
        extra["tools_schemas"] = list(body.tools)
    if body.tool_choice is not None:
        extra["tool_choice"] = body.tool_choice
    if body.response_format is not None:
        extra["response_format"] = body.response_format
    # Multi-turn history (F-11): replay system/assistant/tool turns onto
    # the conversation as a fully-assembled ``messages`` array so the
    # LLM sees the full transcript even on the first turn after a
    # stateless /v1 call. ``llm_stream._build_payload`` L752 honours
    # ``extra["messages"]`` as a complete override.
    history = list(body.messages)
    if len(history) > 1:
        # Build the OpenAI message list verbatim (excluding the trailing
        # user message which is the prompt input). The LLM adapter's
        # message sanitisation pipeline (sanitize_tool_messages /
        # sanitize_messages_tool_call_names) will normalise tool turns.
        wire_messages: list[dict[str, Any]] = []
        for msg in history:
            entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.name is not None:
                entry["name"] = msg.name
            if msg.tool_call_id is not None:
                entry["tool_call_id"] = msg.tool_call_id
            wire_messages.append(entry)
        extra["messages"] = wire_messages
    # 1-L2: forward unknown OpenAI knobs (parallel_tool_calls / reasoning /
    # prediction / logit_bias / user / etc.) verbatim. §G locks the model
    # to ``extra="ignore"`` (must NOT become ``forbid``) — and ``ignore``
    # means Pydantic drops extras (``model_extra`` is empty), so we capture
    # them from the *raw request body* instead and re-attach here. Known
    # top-level fields already mapped above (and the structural ``model`` /
    # ``messages`` / ``stream``) take precedence — we never overwrite them.
    if unknown_fields:
        _known = set(OpenAIChatCompletionRequest.model_fields.keys())
        for key, value in unknown_fields.items():
            if key in _known or key in extra:
                continue
            extra[key] = value
    # 1-L1: forward provider-specific request headers (x- / anthropic- /
    # openai-) under a dedicated ``extra`` key so the LLM adapter can layer
    # them onto the upstream request. Credential values are NOT logged.
    if passthrough_headers:
        extra["passthrough_headers"] = dict(passthrough_headers)
    return extra or None


# ---- Streaming bridge: OpenAI-shape frames over StreamChatUseCase --------


async def _stream_openai_completions(
    *,
    container: "Container",
    body: OpenAIChatCompletionRequest,
    user_prompt: str,
    passthrough_headers: dict[str, str] | None = None,
    unknown_fields: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """Drive ``StreamChatUseCase`` and convert frames to OpenAI deltas.

    Strategy:
      1. Provision (or resume — PR-D1 F-11) a conversation + tab for
         this request. ``body.conversation_id`` lets the client resume
         an existing QAI conversation; absent / unknown id => fresh
         one-shot.
      2. Each ``StreamFrame.CHUNK`` becomes one
         ``chat.completion.chunk`` with the chunk text in ``delta.content``.
      3. ``TOOL_CALL`` frames (PR-D1 F-12) fold into OpenAI
         ``tool_calls`` delta chunks so SDK clients see the model's
         tool-use intent.
      4. The terminal ``StreamFrame.END`` becomes a chunk with empty
         delta + ``finish_reason="stop"`` + appended ``usage`` (F-10),
         followed by ``data: [DONE]``.
      5. ``ERROR`` frames mid-stream (PR-D1 F-12) become a single
         ``data: {"error": ...}`` line followed by ``data: [DONE]`` so
         clients learn the turn aborted; same shape as the open-stream
         exception path below.
    """
    completion_id = f"chatcmpl-{int(time.time() * 1000)}"
    model = body.model
    extra = _build_extra_from_request(
        body,
        passthrough_headers=passthrough_headers,
        unknown_fields=unknown_fields,
    )

    try:
        conv = await _resolve_compat_conversation(
            container, conversation_id=body.conversation_id
        )
        tab = await _provision_compat_tab(container, conversation=conv)
        agen = await container.chat.stream_chat_use_case.execute(
            StreamChatInput(
                tab_id=tab.id,
                conversation_id=conv.id,
                user_message=MessageContent(text=user_prompt),
                model_hint=body.model if body.model else None,
                extra=extra,
            ),
        )
    except QaiError as exc:
        yield _line({"error": exc.to_dict()})
        yield _done_line()
        return

    # PR-D1 (F-12) — fold tool-call frames into OpenAI delta chunks.
    # OpenAI streams a ``tool_calls`` array under ``delta``: each entry
    # carries ``index`` / ``id`` / ``type:"function"`` /
    # ``function:{name, arguments}``. The QAI-native TOOL_CALL frame
    # carries the full tool name + JSON arguments in one shot (no
    # incremental argument streaming), so we emit a single delta per
    # frame with the full arguments string.
    tool_call_index = 0
    captured_usage: dict[str, Any] | None = None

    try:
        async for frame in agen:
            if frame.frame_type is StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if not isinstance(text, str):
                    text = ""
                yield _line(
                    _completion_chunk(
                        model=model,
                        completion_id=completion_id,
                        content=text,
                        finish=False,
                    )
                )
            elif frame.frame_type is StreamFrameType.TOOL_CALL:
                # PR-D1 F-12: fold into OpenAI tool_calls delta. The
                # native frame_id doubles as the tool_call id (stable
                # across the call so the client can correlate).
                tool_name = frame.payload.get("tool_name", "")
                arguments = frame.payload.get("arguments", {})
                if isinstance(arguments, dict):
                    arguments_json = json.dumps(arguments, ensure_ascii=False)
                else:
                    arguments_json = str(arguments)
                _tc_entry: dict[str, Any] = {
                    "index": tool_call_index,
                    "id": frame.frame_id,
                    "type": "function",
                    "function": {
                        "name": str(tool_name),
                        "arguments": arguments_json,
                    },
                }
                # Forward the Vertex AI thought_signature on the outbound
                # tool_calls delta (optional, non-standard field; appended only
                # when present so ordinary responses are byte-unchanged). Lets a
                # client that drives a Vertex thinking model echo it back on the
                # next turn instead of hitting the missing-signature 400 /
                # flatten degradation.
                _sig = frame.payload.get("thought_signature")
                if _sig:
                    _tc_entry["thought_signature"] = _sig
                tool_delta = {"tool_calls": [_tc_entry]}
                yield _line(
                    _completion_chunk(
                        model=model,
                        completion_id=completion_id,
                        content="",
                        finish=False,
                        delta_overrides=tool_delta,
                    )
                )
                tool_call_index += 1
            elif frame.frame_type is StreamFrameType.ERROR:
                # PR-D1 F-12: surface mid-stream errors as the
                # OpenAI-standard ``{"error": ...}`` envelope. The
                # native ERROR frame payload is ``{code, message}`` —
                # forward it under the OpenAI-style shape clients expect.
                err = {
                    "message": frame.payload.get("message", "Stream error"),
                    "type": "stream_error",
                    "code": frame.payload.get("code", "chat.stream.error"),
                }
                yield _line({"error": err})
                yield _done_line()
                return
            elif frame.frame_type is StreamFrameType.END:
                # PR-D1 F-10: capture cumulative token usage off the
                # END frame (the snapshot end frame at
                # streaming.py:1396 has no usage and is correctly
                # skipped). Multiple END frames may appear; keep the
                # last non-empty one.
                _u = frame.payload.get("usage")
                if isinstance(_u, dict):
                    captured_usage = _u
                # We cannot emit the final stop chunk yet in case more
                # frames follow (e.g. snapshot END). Defer to after the
                # loop so usage reflects the latest captured value.
    except QaiError as exc:
        yield _line({"error": exc.to_dict()})
        yield _done_line()
        return

    # Emit the final stop chunk + usage (F-10) + [DONE]. Normalize the
    # captured internal usage into the OpenAI-standard shape (shared helper —
    # same one the non-streaming exit uses) so third-party clients see
    # prompt_tokens_details.cached_tokens without any internal private keys.
    _final_usage = (
        _normalize_usage_for_openai(captured_usage)
        if isinstance(captured_usage, dict)
        else None
    )
    yield _line(
        _completion_chunk(
            model=model,
            completion_id=completion_id,
            content="",
            finish=True,
            usage=_final_usage,
        )
    )
    yield _done_line()


async def _stream_buffered_completions(
    *,
    container: "Container",
    body: OpenAIChatCompletionRequest,
    user_prompt: str,
    passthrough_headers: dict[str, str] | None = None,
    unknown_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Non-streaming path: return one buffered completion body.

    R13 dealign: the chunk-aggregation (collect every CHUNK frame's text
    into one completion) is delegated to the application layer
    (:meth:`StreamChatUseCase.collect_completion`) instead of being
    looped in the route — the interface only provisions the
    conversation/tab and shapes the OpenAI response envelope.

    PR-D1 (F-10) upgraded the call from ``collect_completion_text``
    (text only) to ``collect_completion`` (text + cumulative token
    usage off the terminal END frame), so the response ``usage`` block
    reports real prompt/completion tokens instead of character counts.
    """
    extra = _build_extra_from_request(
        body,
        passthrough_headers=passthrough_headers,
        unknown_fields=unknown_fields,
    )
    conv = await _resolve_compat_conversation(
        container, conversation_id=body.conversation_id
    )
    tab = await _provision_compat_tab(container, conversation=conv)
    # QaiError raised here / inside the use case bubbles to the unified
    # error handler, which translates it; we do not swallow it.
    content, usage = await container.chat.stream_chat_use_case.collect_completion(
        StreamChatInput(
            tab_id=tab.id,
            conversation_id=conv.id,
            user_message=MessageContent(text=user_prompt),
            model_hint=body.model if body.model else None,
            extra=extra,
        ),
    )

    completion_id = f"chatcmpl-{int(time.time() * 1000)}"
    return _final_completion(
        model=body.model,
        completion_id=completion_id,
        content=content,
        usage=usage,
    )


# ---- Conversation / tab provisioning for stateless /v1 calls -------------


async def _resolve_compat_conversation(
    container: "Container",
    *,
    conversation_id: str | None,
):  # type: ignore[no-untyped-def]
    """Resume an existing conversation when ``conversation_id`` is set,
    otherwise mint a fresh one-shot.

    PR-D1 (F-11): the OpenAI-extension ``conversation_id`` field lets a
    /v1 caller resume an existing QAI conversation across stateless
    requests. Unknown id → fall back to fresh provision (no error,
    matching V1's tolerant transparent-proxy behaviour for unknown
    upstream knobs).
    """
    if conversation_id:
        try:
            return await container.chat.conversations.get(
                ConversationId(value=conversation_id)
            )
        except Exception:
            # Unknown / invalid id — silently fall back so OpenAI SDK
            # callers that pass a stale id keep working.
            logger.debug(
                "openai_compat: unknown conversation_id=%r — "
                "provisioning a fresh one",
                conversation_id,
            )
    return await _provision_compat_conversation(container)


async def _provision_compat_conversation(container: "Container"):  # type: ignore[no-untyped-def]
    """Create a one-shot conversation just for this /v1 call."""
    from qai.chat.application.use_cases.conversation_management import (
        CreateConversationInput,
    )

    return await container.chat.create_conversation_use_case.execute(
        CreateConversationInput(title="OpenAI-Compat Stateless Turn"),
    )


async def _provision_compat_tab(container: "Container", *, conversation):  # type: ignore[no-untyped-def]
    """Open + persist a fresh tab on the just-created conversation."""
    tab = ConversationTab.open(
        tab_id=TabId(value=container.ids.new_id()),
        conversation_id=conversation.id,
        now=container.clock.now(),
    )
    await container.chat.tabs.save(tab)
    return tab


# ---- Routes ---------------------------------------------------------------


def _last_user_prompt(messages: list[_OpenAIChatMessage]) -> str:
    """Pick the most recent user message body to use as the prompt.

    The remainder of the transcript (system / assistant / tool turns +
    earlier user turns) is forwarded onto the LLM wire payload via
    :func:`_build_extra_from_request` (PR-D1 F-11) so the model sees
    the full multi-turn context.
    """
    for m in reversed(messages):
        if m.role == "user":
            return _content_to_text(m.content)
    # Falls back to the very last message; OpenAI's reference behaviour
    # is to use the entire transcript so this is a small simplification.
    return _content_to_text(messages[-1].content)


def _make_openai_auth_dependency(api_key: str | None):  # type: ignore[no-untyped-def]
    """Build the optional bearer-token dependency for the ``/v1`` routes.

    S-3 (align D2). When ``api_key`` is ``None`` the returned dependency is
    a no-op — the OpenAI-compatible surface stays open, matching V1's
    transparent local-proxy behaviour (the routes are loopback-bound by
    default). When ``api_key`` is set, every ``/v1`` request must present
    ``Authorization: Bearer <key>``; otherwise a 403
    ``openai_compat.unauthorized`` :class:`ForbiddenError` is raised and
    rendered through the unified error envelope.

    The comparison uses :func:`secrets.compare_digest` so token validation
    is constant-time (no early-exit timing oracle on the key).
    """

    async def _require_bearer(
        authorization: str | None = Header(default=None),
    ) -> None:
        if api_key is None:
            return
        expected = f"Bearer {api_key}"
        if authorization is None or not secrets.compare_digest(
            authorization, expected
        ):
            raise ForbiddenError(
                "openai_compat.unauthorized",
                "Missing or invalid bearer token for the OpenAI-compatible API.",
            )

    return _require_bearer


def build_router(*, container: "Container") -> APIRouter:
    """Build the OpenAI-compatible router (mounted under ``/v1``)."""
    router = APIRouter(prefix="/v1", tags=["openai_compat"])

    # S-3 (align D2): optional bearer-token guard. When
    # ``server.openai_api_key`` is unset the dependency is a no-op (routes
    # stay open, matching V1's transparent local-proxy behaviour); when set
    # it enforces ``Authorization: Bearer <key>`` on all 3 /v1 routes. This
    # is layered purely as a ``dependencies=[...]`` pre-check — route paths,
    # methods, request/response shapes are untouched (§3.1 OpenAI-compat
    # contract preserved).
    auth_dependency = _make_openai_auth_dependency(
        container.settings.server.openai_api_key
    )

    @router.get(
        "/models",
        response_model=OpenAIModelList,
        dependencies=[Depends(auth_dependency)],
    )
    async def list_models() -> OpenAIModelList:
        infos = await container.chat.openai_model_lister.list_models()
        return OpenAIModelList(
            data=[
                OpenAIModelEntry(
                    id=info.id,
                    created=info.created,
                    owned_by=info.owned_by,
                )
                for info in infos
            ]
        )

    @router.get(
        "/models/{model_id:path}",
        response_model=OpenAIModelEntry,
        dependencies=[Depends(auth_dependency)],
    )
    async def get_model(
        model_id: str = Path(..., min_length=1, max_length=256),
    ) -> OpenAIModelEntry:
        info = await container.chat.openai_model_lister.get_model(model_id)
        if info is None:
            raise NotFoundError(
                "openai_compat.model_not_found",
                resource_type="model",
                resource_id=model_id,
            )
        return OpenAIModelEntry(
            id=info.id,
            created=info.created,
            owned_by=info.owned_by,
        )

    @router.post("/chat/completions", dependencies=[Depends(auth_dependency)])
    async def chat_completions(
        body: OpenAIChatCompletionRequest,
        request: Request,
    ) -> Any:
        prompt = _last_user_prompt(body.messages)
        # 1-L1: collect provider-passthrough headers (x- / anthropic- /
        # openai-) once; forwarded into StreamChatInput.extra so they reach
        # the upstream provider request without being logged here.
        passthrough_headers = _collect_passthrough_headers(request.headers)
        # 1-L2: re-read the raw JSON body to recover unknown top-level
        # fields dropped by ``extra="ignore"`` (parallel_tool_calls /
        # reasoning / prediction / etc.) so they can be forwarded verbatim.
        # Best-effort: a malformed body would already have failed model
        # validation above, so this re-parse is expected to succeed.
        unknown_fields: dict[str, Any] | None = None
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                unknown_fields = raw
        except Exception:  # noqa: BLE001 -- best-effort; body already validated
            unknown_fields = None
        if body.stream:
            stream_iter = _stream_openai_completions(
                container=container,
                body=body,
                user_prompt=prompt,
                passthrough_headers=passthrough_headers,
                unknown_fields=unknown_fields,
            )
            return StreamingResponse(
                stream_iter,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        return await _stream_buffered_completions(
            container=container,
            body=body,
            user_prompt=prompt,
            passthrough_headers=passthrough_headers,
            unknown_fields=unknown_fields,
        )

    return router


__all__ = ["build_router"]
