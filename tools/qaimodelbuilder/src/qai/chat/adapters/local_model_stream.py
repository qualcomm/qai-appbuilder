# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Local on-device model streaming adapter (PR-091, audit C-2 / H-8).

Migrates the local-model branch of the legacy
``backend/chat_handler.py:_stream_local`` (lines 2009-2025, 2096-2184,
2877-2922, 3351-3368) into a context-isolated
:class:`~qai.chat.application.ports.LLMStreamPort` adapter.

Two responsibilities the cloud OpenAI-compatible adapter
(:class:`qai.chat.infrastructure.llm_stream.HttpOpenAICompatibleLLMStream`)
does NOT carry:

* **C-2 — XML ``<tool_call>`` parsing.** On-device runtimes such as
  GenieAPIService (Qwen3-on-NPU, Llama-on-NPU, etc.) embed tool calls
  inside the assistant text channel using XML-style delimiters rather
  than the OpenAI ``tool_calls[]`` field.  We buffer the streamed
  content, split out safe-to-emit prefixes via
  :func:`qai.chat.adapters.openai_protocol.split_safe_content`, and
  emit complete ``<tool_call>{json}</tool_call>`` blocks as
  :data:`StreamFrameType.TOOL_CALL` frames.

* **H-8 — Service-unavailable friendly fallback.** When the local
  inference HTTP endpoint is unreachable (``httpx.ConnectError`` /
  ``ConnectTimeout`` on the configured local URL) the adapter yields
  a single explanatory chunk pointing the user to the Service panel,
  instead of leaking a raw connection error.  Error code:
  ``chat.local.service_unavailable``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from qai.chat.adapters.openai_protocol import (
    TOOL_CALL_OPEN,
    extract_xml_tool_call,
    parse_openai_tool_call,
    split_safe_content,
)
from qai.chat.adapters.error_classifier import (
    is_prompt_too_long_error,
    is_throttling_error,
)
from qai.chat.application.ports import LLMStreamRequest
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.infrastructure.notice_text import (
    make_content_filter_notice,
    make_truncation_notice,
)
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger


__all__ = ["LocalModelStreamAdapter"]


_log = get_logger(__name__)

# V1 ``backend/local_prompt_builder.py:93`` — the marker that makes
# GenieAPIService's PromptOptimizer.DetectAgentType() take the MAIN_AGENT
# optimisation path.  Defined here (not imported from the use case) to
# avoid a cross-layer import; the value is a stable protocol constant.
_LOCAL_AGENT_MAIN_MARKER = "agent=main"


_SERVICE_UNAVAILABLE_MESSAGE = (
    "**[本地模型服务未启动]**\n\n"
    "您选中了本地模型，但本地推理服务还未启动。\n\n"
    "**解决方法：**\n"
    "1. 打开顶部导航栏的 **Service** 面板\n"
    "2. 在模型列表中选择想使用的本地模型\n"
    "3. 点击 **启动服务** 并等待启动完成\n"
    "4. 回到此处重新发送您的问题\n\n"
    "> 如果想直接使用云端模型，请在顶部模型选择器中切换到云端模型。"
)


class LocalModelStreamAdapter:
    """OpenAI-compatible HTTP streaming adapter for on-device runtimes.

    Talks to a local OpenAI-compatible endpoint (typically
    GenieAPIService) at ``base_url`` and translates its streamed deltas
    into :class:`StreamFrame` objects.  Differs from the cloud adapter
    in two ways (see module docstring): inline XML ``<tool_call>``
    parsing and a friendly mock reply when the local service is down.
    """

    __slots__ = (
        "_api_key",
        "_base_url",
        "_client_factory",
        "_ids",
        "_model_name",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        base_url: str,
        ids: IdGenerator,
        api_key: str | None = None,
        model_name: str = "local",
        timeout_seconds: float = 1800.0,
        client_factory: Any | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._ids = ids
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    # ------------------------------------------------------------------
    # LLMStreamPort
    # ------------------------------------------------------------------
    def stream(
        self,
        request: LLMStreamRequest,
    ) -> AsyncIterator[StreamFrame]:
        return self._run(request)

    async def _run(
        self,
        request: LLMStreamRequest,
    ) -> AsyncIterator[StreamFrame]:
        if not self._base_url:
            # No endpoint configured — yield friendly fallback.
            async for frame in self._yield_service_unavailable_fallback():
                yield frame
            return

        url = f"{self._base_url}/chat/completions"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            # V1 parity (chat_handler.py:1846-1848): identify the client
            # source so GenieAPIService's DetectClientSource() routes
            # QAIModelBuilder requests through its OPTIMIZED PromptOptimizer
            # path. V1 always sent this header on every local request.
            "X-Genie-Client": "QAIModelBuilder",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = self._build_payload(request)

        sequence = 0
        content_buffer = ""
        # Track the terminal finish_reason so we can emit a V1-parity
        # truncation / content-filter notice before END (audit #5). The cloud
        # adapter already does this; the local path regressed it.
        last_finish_reason: str | None = None
        # ── OpenAI-native tool_calls accumulator (root-cause fix) ──
        # GenieAPIService converts the on-device model's tool call into the
        # OpenAI-native ``delta.tool_calls[]`` field (NOT a ``<tool_call>``
        # text block) and finalises it with ``finish_reason == "tool_calls"``.
        # V1 ``backend/chat_handler.py:1947-1983`` accumulates each chunk's
        # ``tool_calls`` by ``index`` (so streamed/fragmented ``arguments``
        # concatenate) and emits them only when the round ends. We mirror that
        # here keyed by ``index`` so both single-chunk (the captured local
        # shape) AND fragmented (cloud-routed) deliveries work.  ``int index``
        # → ``{"id", "type", "function": {"name", "arguments"}, ...}``.
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}

        try:
            async with self._make_client() as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body_text = await response.aread()
                        body_str = body_text.decode(
                            "utf-8", errors="replace"
                        )
                        # V1 parity (chat_handler.py:1908-1919): classify the
                        # error body so prompt-too-long triggers compression
                        # retry and throttling triggers backoff retry. The
                        # retry policy keys off the ERROR frame's ``code``
                        # (``_classify_error_frame``); a generic code would
                        # make the local path silently skip both retries.
                        sequence += 1
                        if is_prompt_too_long_error(body_str):
                            yield StreamFrame.error(
                                frame_id=self._next_frame_id("err"),
                                sequence=sequence,
                                code="prompt_too_long",
                                message=(
                                    f"Local model error "
                                    f"{response.status_code}: {body_str[:500]}"
                                ),
                            )
                        elif is_throttling_error(body_str):
                            yield StreamFrame.error(
                                frame_id=self._next_frame_id("err"),
                                sequence=sequence,
                                code="throttling",
                                message=(
                                    "请求过于频繁，请稍后再试。"
                                    "（Too many tokens / ThrottlingException）"
                                ),
                            )
                        else:
                            yield StreamFrame.error(
                                frame_id=self._next_frame_id("err"),
                                sequence=sequence,
                                code="chat.local.http_error",
                                message=(
                                    f"Local model HTTP {response.status_code}: "
                                    f"{body_str[:200]}"
                                ),
                            )
                        sequence += 1
                        yield StreamFrame.end(
                            frame_id=self._next_frame_id("end"),
                            sequence=sequence,
                            reason="failed",
                        )
                        return

                    async for raw_line in response.aiter_lines():
                        if not raw_line:
                            continue
                        if not raw_line.startswith("data:"):
                            continue
                        data_str = raw_line[len("data:"):].strip()
                        if not data_str or data_str == "[DONE]":
                            continue

                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            _log.debug(
                                "local_model_stream.bad_sse_json",
                                preview=data_str[:200],
                            )
                            continue

                        # ── GenieAPIService status / keep-alive frames ──
                        # On-device runtimes emit periodic *status* frames whose
                        # ``delta.content`` is empty and whose ``status`` field
                        # carries the lifecycle phase (``summarizing`` /
                        # ``preparing`` / ``inference`` / ``tool_call``) with a
                        # human-readable ``status_message`` ("Processing long
                        # text...", "Inferencing...", "Calling tool...").  V1
                        # (backend/chat_handler.py:1937-2025 ``_stream_local``)
                        # NEVER reads ``status`` / ``status_message``: they are
                        # pure SSE keep-alive frames (see the daemon's
                        # ``service_config.json: status_update_content_visible``,
                        # default ``false``), so V1 lets them fall through its
                        # ``if content:`` guard and silently drops them.
                        #
                        # We replicate that V1 behaviour explicitly here so the
                        # intent is unmistakable and so a status frame can NEVER
                        # be mis-handled: ``status: "tool_call"`` is NOT a tool
                        # invocation signal (the real tool call rides either a
                        # later ``delta.tool_calls`` or a ``<tool_call>`` text
                        # block in ``delta.content`` — see the XML extraction
                        # loop below); it is a progress marker only.  We log at
                        # debug for diagnosability and ``continue`` — exactly
                        # like V1's content-guard skip — WITHOUT touching the
                        # finish_reason / content path so an isolated status
                        # frame can never end the turn or be counted as the
                        # (empty) completion.
                        status = self._extract_status(event)
                        if status is not None and not self._extract_delta_text(
                            event
                        ):
                            _log.debug(
                                "local_model_stream.status_frame",
                                status=status,
                                message=self._extract_status_message(event),
                            )
                            continue

                        # ── Priority 1: OpenAI-native delta.tool_calls[] ──
                        # ROOT CAUSE FIX: GenieAPIService delivers the on-device
                        # model's tool call here (NOT as ``<tool_call>`` text in
                        # ``delta.content``).  The previous adapter only read
                        # ``delta.content`` and dropped these frames (empty
                        # content → ``continue``), so the tool-call signal was
                        # lost and the turn produced no output → ``empty_response``
                        # (the reported "端侧工具调用失败"). V1
                        # ``backend/chat_handler.py:1947-1968`` accumulates each
                        # chunk's tool_calls by ``index`` (concatenating any
                        # fragmented ``arguments``) and skips content processing
                        # for that chunk; we replicate that exactly.
                        chunk_tool_calls = self._extract_tool_calls(event)
                        if chunk_tool_calls:
                            for tc in chunk_tool_calls:
                                if not isinstance(tc, dict):
                                    continue
                                idx = tc.get("index", 0)
                                if not isinstance(idx, int):
                                    idx = 0
                                entry = accumulated_tool_calls.setdefault(
                                    idx,
                                    {
                                        "id": tc.get("id", ""),
                                        "type": tc.get("type", "function"),
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                func = tc.get("function") or {}
                                if isinstance(func, dict):
                                    if func.get("name"):
                                        entry["function"]["name"] = func["name"]
                                    if func.get("arguments"):
                                        entry["function"]["arguments"] += str(
                                            func["arguments"]
                                        )
                                if tc.get("id"):
                                    entry["id"] = tc["id"]
                                # Preserve Vertex AI thinking-model signatures
                                # (V1 chat_handler.py:1965-1967 parity).
                                if tc.get("thought_signature"):
                                    entry["thought_signature"] = tc[
                                        "thought_signature"
                                    ]
                            # Skip content processing for this chunk (V1:1968).
                            continue

                        # Capture a terminal finish_reason (V1 tracks the last
                        # non-empty one). GenieAPIService emits "" on content
                        # frames and the real reason ("stop"/"length"/
                        # "content_filter"/"tool_calls") on the final frame.
                        fr = self._extract_finish_reason(event)
                        if fr:
                            last_finish_reason = fr

                        # ── finish_reason == "tool_calls": emit accumulated ──
                        # V1 chat_handler.py:1974-1983: the round closes with a
                        # ``finish_reason == "tool_calls"`` frame (empty content,
                        # null tool_calls); flush every accumulated tool call as
                        # a TOOL_CALL frame.  This is the normal end of an
                        # on-device tool-call round — NOT an empty response.
                        if fr == "tool_calls" and accumulated_tool_calls:
                            for emitted in self._emit_accumulated_tool_calls(
                                accumulated_tool_calls,
                            ):
                                sequence += 1
                                yield StreamFrame.tool_call(
                                    frame_id=self._next_frame_id("tc"),
                                    sequence=sequence,
                                    tool_name=emitted["name"],
                                    arguments=emitted["arguments"],
                                    tool_call_id=emitted.get("id"),
                                    thought_signature=emitted.get(
                                        "thought_signature"
                                    ),
                                )
                            accumulated_tool_calls = {}
                            continue

                        delta_text = self._extract_delta_text(event)
                        if not delta_text:
                            continue

                        content_buffer += delta_text

                        # ── XML tool_call extraction loop ──
                        # Repeatedly try to extract complete
                        # <tool_call>...</tool_call> blocks; emit safe
                        # text in between.  See
                        # backend/chat_handler.py:2009-2025 for the
                        # legacy reference.
                        while True:
                            tool_event = extract_xml_tool_call(content_buffer)
                            if tool_event is None:
                                break
                            tag_idx = content_buffer.find(TOOL_CALL_OPEN)
                            pre_text = content_buffer[:tag_idx]
                            if pre_text:
                                sequence += 1
                                yield StreamFrame.chunk(
                                    frame_id=self._next_frame_id("ck"),
                                    sequence=sequence,
                                    text=pre_text,
                                )
                            sequence += 1
                            yield StreamFrame.tool_call(
                                frame_id=self._next_frame_id("tc"),
                                sequence=sequence,
                                tool_name=tool_event["name"],
                                arguments=tool_event["arguments"]
                                if isinstance(tool_event.get("arguments"), dict)
                                else {},
                                # Carry the round's lead-in text on the tool_call
                                # too, so the UI can commit it as a standalone
                                # visible message regardless of chunk-frame timing
                                # (it is also sent as the chunk above; the UI
                                # de-dupes by preferring the live buffer).
                                lead_in=pre_text or None,
                            )
                            close_idx = content_buffer.find(
                                "</tool_call>", tag_idx,
                            )
                            if close_idx == -1:
                                # Should not happen — extract_xml_tool_call
                                # only returns when both tags are present.
                                content_buffer = ""
                                break
                            content_buffer = content_buffer[
                                close_idx + len("</tool_call>"):
                            ]

                        # Flush only the safe prefix; hold back any tail
                        # that could still be the start of a new
                        # <tool_call> opener so the UI never sees a
                        # half-formed tag.
                        safe, content_buffer = split_safe_content(
                            content_buffer,
                        )
                        if safe:
                            sequence += 1
                            yield StreamFrame.chunk(
                                frame_id=self._next_frame_id("ck"),
                                sequence=sequence,
                                text=safe,
                            )

            # ── Stream ended with un-emitted tool_calls (no terminal
            # ``finish_reason == "tool_calls"`` frame) ──
            # V1 chat_handler.py:2027-2052: some upstreams (esp. cloud-routed)
            # close the SSE without a dedicated ``finish_reason == "tool_calls"``
            # frame; flush whatever was accumulated so the call still fires.
            # ``finish_reason == "length"`` means the arguments were truncated
            # mid-stream → discard to avoid an empty-argument execution and
            # surface the V1 truncation notice instead.
            if accumulated_tool_calls:
                if last_finish_reason == "length":
                    # V1 parity (chat_handler.py:1987-2000 / 2031-2044): name
                    # the tool(s) whose call was truncated so the user knows
                    # which side effect was cancelled.
                    _truncated_name = self._first_tool_name(
                        accumulated_tool_calls
                    )
                    accumulated_tool_calls = {}
                    sequence += 1
                    yield StreamFrame.chunk(
                        frame_id=self._next_frame_id("ck"),
                        sequence=sequence,
                        text=make_truncation_notice(_truncated_name),
                    )
                else:
                    for emitted in self._emit_accumulated_tool_calls(
                        accumulated_tool_calls,
                    ):
                        sequence += 1
                        yield StreamFrame.tool_call(
                            frame_id=self._next_frame_id("tc"),
                            sequence=sequence,
                            tool_name=emitted["name"],
                            arguments=emitted["arguments"],
                            tool_call_id=emitted.get("id"),
                            thought_signature=emitted.get("thought_signature"),
                        )
                    accumulated_tool_calls = {}

            # ── Stream completed: flush any remaining buffered content ──
            if content_buffer:
                tool_event = extract_xml_tool_call(content_buffer)
                if tool_event is not None:
                    tag_idx = content_buffer.find(TOOL_CALL_OPEN)
                    pre_text = content_buffer[:tag_idx]
                    if pre_text:
                        sequence += 1
                        yield StreamFrame.chunk(
                            frame_id=self._next_frame_id("ck"),
                            sequence=sequence,
                            text=pre_text,
                        )
                    sequence += 1
                    yield StreamFrame.tool_call(
                        frame_id=self._next_frame_id("tc"),
                        sequence=sequence,
                        tool_name=tool_event["name"],
                        arguments=tool_event["arguments"]
                        if isinstance(tool_event.get("arguments"), dict)
                        else {},
                        lead_in=pre_text or None,
                    )
                else:
                    sequence += 1
                    yield StreamFrame.chunk(
                        frame_id=self._next_frame_id("ck"),
                        sequence=sequence,
                        text=content_buffer,
                    )

            # V1 parity (chat_handler.py:2029-2074, audit #5): when the model
            # stopped because it hit the token ceiling (length) or the content
            # filter fired, append a user-facing notice so the user knows the
            # reply was cut off (and how to recover). A normal "stop" / empty
            # reason emits nothing extra.
            if last_finish_reason == "length":
                sequence += 1
                yield StreamFrame.chunk(
                    frame_id=self._next_frame_id("ck"),
                    sequence=sequence,
                    text=make_truncation_notice(),
                )
            elif last_finish_reason == "content_filter":
                sequence += 1
                yield StreamFrame.chunk(
                    frame_id=self._next_frame_id("ck"),
                    sequence=sequence,
                    text=make_content_filter_notice(),
                )

            sequence += 1
            yield StreamFrame.end(
                frame_id=self._next_frame_id("end"),
                sequence=sequence,
                reason="completed",
            )
            return

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # H-8: Local service down — yield friendly fallback.
            _log.warning(
                "local_model_stream.service_unavailable",
                base_url=self._base_url,
                error=str(exc),
            )
            async for frame in self._yield_service_unavailable_fallback(
                start_sequence=sequence,
            ):
                yield frame
            return
        except httpx.TimeoutException as exc:
            sequence += 1
            yield StreamFrame.error(
                frame_id=self._next_frame_id("err"),
                sequence=sequence,
                code="chat.local.timeout",
                message=f"本地模型服务响应超时：{exc}",
            )
            sequence += 1
            yield StreamFrame.end(
                frame_id=self._next_frame_id("end"),
                sequence=sequence,
                reason="failed",
            )
            return
        except Exception as exc:  # noqa: BLE001 — defensive last-resort branch
            _log.exception("local_model_stream.unexpected_error")
            sequence += 1
            # V1 parity (chat_handler.py:2118-2128): an exception surfaced
            # outside the >=400 path (e.g. a RuntimeError carrying the body)
            # is still classified so prompt-too-long / throttling drive the
            # retry policy instead of dead-ending as a generic error.
            exc_msg = str(exc)
            if is_prompt_too_long_error(exc_msg):
                yield StreamFrame.error(
                    frame_id=self._next_frame_id("err"),
                    sequence=sequence,
                    code="prompt_too_long",
                    message=exc_msg,
                )
            elif is_throttling_error(exc_msg):
                yield StreamFrame.error(
                    frame_id=self._next_frame_id("err"),
                    sequence=sequence,
                    code="throttling",
                    message=(
                        "请求过于频繁，请稍后再试。"
                        "（Too many tokens / ThrottlingException）"
                    ),
                )
            else:
                yield StreamFrame.error(
                    frame_id=self._next_frame_id("err"),
                    sequence=sequence,
                    code="chat.local.unexpected",
                    message=exc_msg,
                )
            sequence += 1
            yield StreamFrame.end(
                frame_id=self._next_frame_id("end"),
                sequence=sequence,
                reason="failed",
            )
            return

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _yield_service_unavailable_fallback(
        self,
        *,
        start_sequence: int = 0,
    ) -> AsyncIterator[StreamFrame]:
        """Emit a single chunk + END frame describing the down service.

        Uses the dedicated error code ``chat.local.service_unavailable``
        prefixed inside the chunk so frontends that wish to surface a
        button (e.g. "Open Service Panel") can detect the condition.
        Reference: legacy ``_mock_response`` at
        ``backend/chat_handler.py:2150-2184``.
        """
        seq = start_sequence + 1
        yield StreamFrame.error(
            frame_id=self._next_frame_id("err"),
            sequence=seq,
            code="chat.local.service_unavailable",
            message=_SERVICE_UNAVAILABLE_MESSAGE,
        )
        seq += 1
        yield StreamFrame.chunk(
            frame_id=self._next_frame_id("ck"),
            sequence=seq,
            text=_SERVICE_UNAVAILABLE_MESSAGE,
        )
        seq += 1
        yield StreamFrame.end(
            frame_id=self._next_frame_id("end"),
            sequence=seq,
            reason="completed",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    @staticmethod
    def _extract_delta_text(event: dict[str, Any]) -> str:
        """Return the textual delta (if any) from an SSE chunk."""
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        delta = first.get("delta") or {}
        if not isinstance(delta, dict):
            return ""
        content = delta.get("content")
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _extract_tool_calls(event: dict[str, Any]) -> list[Any]:
        """Return ``delta.tool_calls`` of an SSE chunk as a list.

        GenieAPIService (and cloud-routed models) deliver tool calls on the
        OpenAI-native ``choices[0].delta.tool_calls`` array.  Returns an empty
        list for content / status / terminal frames (where the field is absent
        or ``null``), so the caller can simply ``if chunk_tool_calls:``.
        Robust against malformed shapes so a stray frame can never crash the
        turn (V1 ``backend/chat_handler.py:1947`` ``delta.get("tool_calls", [])``
        parity).
        """
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        first = choices[0]
        if not isinstance(first, dict):
            return []
        delta = first.get("delta") or {}
        if not isinstance(delta, dict):
            return []
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            return tool_calls
        return []

    @staticmethod
    def _emit_accumulated_tool_calls(
        accumulated: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Parse accumulated tool-call entries into emit-ready dicts.

        Mirrors V1 ``backend/chat_handler.py:1976-1982`` (and reuses the shared
        :func:`qai.chat.adapters.openai_protocol.parse_openai_tool_call` so the
        local path stays byte-for-byte consistent with the cloud adapter):
        walk the entries in ascending ``index`` order, decode each via the
        shared parser (which JSON-decodes a string ``arguments`` and coerces
        bad/missing args to ``{}``), and drop any malformed entry (unknown
        type / missing name).  Returns ``{"name": str, "arguments": dict}``
        dicts ready to become :data:`StreamFrameType.TOOL_CALL` frames.
        """
        emitted: list[dict[str, Any]] = []
        for idx in sorted(accumulated.keys()):
            raw = accumulated[idx]
            parsed = parse_openai_tool_call(raw)
            if parsed is None:
                continue
            args = parsed.get("arguments")
            out: dict[str, Any] = {
                "name": parsed["name"],
                "arguments": args if isinstance(args, dict) else {},
            }
            # Carry the upstream tool_call id + Vertex AI thought_signature that
            # were captured onto the accumulated entry (lines ~318-325) through
            # to the emitted dict so the TOOL_CALL frame can carry them. Dropping
            # them here was a "captured then discarded" inconsistency: the id is
            # needed for strict result pairing and the signature for Vertex
            # thinking-model fidelity (parity with the cloud path; absent for
            # ordinary on-device models → keys simply omitted, no change).
            _id = raw.get("id")
            if isinstance(_id, str) and _id:
                out["id"] = _id
            _sig = raw.get("thought_signature")
            if _sig:
                out["thought_signature"] = _sig
            emitted.append(out)
        return emitted

    @staticmethod
    def _first_tool_name(
        accumulated: dict[int, dict[str, Any]],
    ) -> str | None:
        """Return the first accumulated tool call's name (or ``None``).

        Used to name the truncated tool in the ``finish_reason == "length"``
        notice (V1 chat_handler.py:1987-2000 ``tool_names`` parity). Walks the
        entries in ascending ``index`` order and returns the first non-empty
        ``function.name``; ``None`` when none is present.
        """
        for idx in sorted(accumulated.keys()):
            func = accumulated[idx].get("function")
            if isinstance(func, dict):
                name = func.get("name")
                if isinstance(name, str) and name:
                    return name
        return None

    @staticmethod
    def _extract_status(event: dict[str, Any]) -> str | None:
        """Return the ``status`` lifecycle marker of an SSE chunk, or ``None``.

        GenieAPIService keep-alive / progress frames carry the lifecycle
        phase on ``choices[0].status`` (``summarizing`` / ``preparing`` /
        ``inference`` / ``tool_call``).  Returns ``None`` for ordinary
        content frames (which have no ``status`` field).  Robust against
        malformed shapes so a stray frame can never crash the turn.
        """
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        status = first.get("status")
        if isinstance(status, str) and status:
            return status
        return None

    @staticmethod
    def _extract_status_message(event: dict[str, Any]) -> str:
        """Return the human-readable ``status_message`` of an SSE chunk.

        Companion of :meth:`_extract_status`; the on-device runtime puts a
        progress sentence ("Processing long text...", "Inferencing...",
        "Calling tool...") on ``choices[0].status_message``.  Used for debug
        logging only — V1 never surfaced it to the user (keep-alive only).
        """
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        msg = first.get("status_message")
        if isinstance(msg, str):
            return msg
        return ""

    @staticmethod
    def _extract_finish_reason(event: dict[str, Any]) -> str | None:
        """Return the finish_reason from an SSE chunk, or ``None``.

        GenieAPIService sends ``finish_reason: ""`` (empty string) on content
        frames and the real terminal reason ("stop" / "length" /
        "content_filter") on the final frame; an empty string is normalised to
        ``None`` so callers only see a meaningful terminal reason.
        """
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        fr = first.get("finish_reason")
        if isinstance(fr, str) and fr:
            return fr
        return None

    def _build_payload(self, request: LLMStreamRequest) -> dict[str, Any]:
        """Construct the OpenAI-compatible request body.

        Local runtimes accept the same shape as cloud OpenAI endpoints.

        Two history sources, in priority order:

        1. ``extra["messages"]`` — a FULLY-ASSEMBLED wire history handed down
           by the agentic follow-up loop (``streaming.py:3674-3683``, same
           contract the cloud adapter honours at ``llm_stream.py:808-810``).
           After a tool call this carries the per-round
           ``assistant{content, tool_calls}`` + paired ``role:tool`` blocks —
           i.e. the executed tool RESULTS. The local adapter MUST forward
           these verbatim; the earlier version rebuilt messages from
           ``request.history`` alone and silently dropped every tool_call /
           tool-result block, so the on-device model never saw its own tool
           output and re-issued the SAME ``read`` call forever (the reported
           "工具调用的结果没有发送给模型" infinite-loop bug).
        2. ``request.history`` — the plain role/content turns (first round,
           no tool blocks yet).

        P1-3 (V1 local_prompt_builder.py:10-14,93,109 parity): for local
        models, inject ``agent=main`` marker and ``<available_skills>``
        XML into the system prompt so GenieAPIService PromptOptimizer
        can walk the MAIN_AGENT optimization path.
        """
        extra = request.extra or {}
        messages: list[dict[str, Any]] = []

        # ── Path 1: fully-assembled wire history from the follow-up loop ──
        # The follow-up loop's ``_build_base_wire_messages`` builds
        # history + user + each round's assistant{tool_calls}/tool blocks
        # but deliberately OMITS the system message (the adapter owns
        # prepending it — same contract the cloud adapter honours at
        # llm_stream.py:990-997). So we forward the wire blocks verbatim
        # (preserving tool_calls/tool_call_id so the model sees its tool
        # RESULTS and continues the chain) AND prepend the local system
        # prompt when the wire history does not already open with one.
        wire_messages = extra.get("messages")
        if isinstance(wire_messages, list) and wire_messages:
            for m in wire_messages:
                if not isinstance(m, dict):
                    continue
                msg = self._normalize_wire_message(m)
                if msg["role"] == "system":
                    sp = msg.get("content") or ""
                    if isinstance(sp, str) and sp.strip():
                        msg["content"] = self._inject_local_protocol(sp, extra)
                messages.append(msg)
            # V1 parity (chat_handler.py:1870 — every local request carries the
            # ``agent=main`` + ``<available_skills>`` system message): if the
            # assembled wire history does NOT already open with a system turn,
            # prepend it from ``extra["system_prompt"]``. Without this the
            # follow-up rounds dropped the system message entirely (the
            # reported "工具调用时 V2 没把系统提示词发送给模型" bug — the model
            # lost agent=main + the skill catalog after the first tool call).
            first_role = messages[0].get("role") if messages else None
            if first_role != "system":
                sp_raw = extra.get("system_prompt")
                if isinstance(sp_raw, str) and sp_raw.strip():
                    messages.insert(
                        0,
                        {
                            "role": "system",
                            "content": self._inject_local_protocol(sp_raw, extra),
                        },
                    )
            payload = self._finalize_payload(messages, request, extra)
            return payload

        # ── Path 2: first round — rebuild from system_prompt + history ──
        if isinstance(extra.get("system_prompt"), str):
            sp = extra["system_prompt"]
            if sp.strip():
                # P1-3: inject local model protocol markers
                sp = self._inject_local_protocol(sp, extra)
                messages.append({"role": "system", "content": sp})

        for msg in request.history:
            # ``history`` is normally a tuple of domain ``Message`` objects,
            # but the agentic loop's context compressor
            # (``streaming.py:_compress_history``) returns plain dicts and
            # threads them back through ``history`` on a compressed round.
            # ``_normalize_local_history_message`` accepts both shapes and
            # preserves any OpenAI-shaped tool-call linkage (see its docstring).
            messages.append(self._normalize_local_history_message(msg))

        # Append the current turn's user prompt.
        prompt_text = ""
        if hasattr(request.prompt, "text") and request.prompt.text:
            prompt_text = request.prompt.text
        messages.append({"role": "user", "content": prompt_text})

        return self._finalize_payload(messages, request, extra)

    @staticmethod
    def _normalize_wire_message(m: dict[str, Any]) -> dict[str, Any]:
        """Copy a wire message dict, preserving tool_calls / tool_call_id.

        The follow-up loop's ``assistant`` messages carry ``tool_calls`` and
        the ``role:tool`` replies carry ``tool_call_id`` — GenieAPIService /
        OpenAI-compatible runtimes need BOTH to correlate a call with its
        result. The old per-field rebuild dropped them; we keep the whole
        OpenAI message shape (role + content + any tool_calls / tool_call_id /
        name) intact.

        ``content`` is passed through for BOTH the plain-string shape AND the
        OpenAI multimodal list shape (``[{type:text..},{type:image_url..}]``).
        The prior code coerced any non-``str`` content to ``""`` — which
        silently dropped a multimodal user turn (e.g. the vision blocks the
        agentic loop injects for a ``question``-answer image) on the way to the
        local daemon (AGENTS 🟡🟡 "发现缺陷必须修"). Whether the on-device model
        actually supports vision is the model's concern; the wire layer must
        not swallow the blocks. ``None`` still normalises to ``""`` (a
        contentless turn), and any other unexpected type is also coerced to
        ``""`` (defensive, unchanged).
        """
        content = m.get("content")
        if isinstance(content, (str, list)):
            wire_content: Any = content
        else:
            wire_content = ""
        out: dict[str, Any] = {
            "role": m.get("role") if isinstance(m.get("role"), str) else "user",
            "content": wire_content,
        }
        if isinstance(m.get("tool_calls"), list):
            out["tool_calls"] = m["tool_calls"]
        if isinstance(m.get("tool_call_id"), str):
            out["tool_call_id"] = m["tool_call_id"]
        if isinstance(m.get("name"), str):
            out["name"] = m["name"]
        return out

    @staticmethod
    def _normalize_local_history_message(msg: Any) -> dict[str, Any]:
        """Coerce one ``request.history`` element into an OpenAI wire dict.

        Accepts BOTH shapes threaded through ``LLMStreamRequest.history``:

        * compressed dicts (``streaming.py:_compress_history``) — passed
          through, defaulting ``role`` to ``user``, and preserving any
          ``tool_calls`` / ``tool_call_id`` / ``name`` linkage they carry;
        * domain :class:`Message` objects — ``role`` / ``content`` plus any
          OpenAI-shaped ``tool_calls`` (entries with ``id`` + ``function``)
          and, for a ``role:tool`` turn, the ``tool_call_id`` lifted from its
          first ``tool_results`` entry that records one.

        Mirrors the cloud adapter's ``llm_stream._normalize_history_message``
        fix: forwarding the linkage ONLY when already OpenAI-shaped (so a
        malformed block is never fabricated) keeps the assistant→tool pairing
        intact instead of degrading to bare role/content. The normal
        sub-agent / follow-up path uses ``extra["messages"]`` (Path 1) and is
        unaffected; this hardens the ``request.history`` fallback path.
        """
        if isinstance(msg, dict):
            role_d = msg.get("role")
            content_d = msg.get("content")
            wire: dict[str, Any] = {
                "role": role_d if isinstance(role_d, str) and role_d else "user",
                "content": content_d if isinstance(content_d, str) else "",
            }
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in msg:
                    wire[k] = msg[k]
            return wire
        role_obj = getattr(msg, "role", None)
        role = getattr(role_obj, "value", None) or "user"
        content_obj = getattr(msg, "content", None)
        content = content_obj.text if getattr(content_obj, "text", None) else ""
        wire = {"role": role, "content": content}
        msg_tool_calls = getattr(msg, "tool_calls", None)
        if isinstance(msg_tool_calls, (list, tuple)) and msg_tool_calls:
            wire_calls = [
                dict(tc)
                for tc in msg_tool_calls
                if isinstance(tc, dict) and tc.get("id") and tc.get("function")
            ]
            if wire_calls:
                wire["tool_calls"] = wire_calls
        if role == "tool":
            msg_tool_results = getattr(msg, "tool_results", None)
            if isinstance(msg_tool_results, (list, tuple)):
                for tr in msg_tool_results:
                    if isinstance(tr, dict) and isinstance(
                        tr.get("tool_call_id"), str
                    ):
                        wire["tool_call_id"] = tr["tool_call_id"]
                        _nm = tr.get("tool_name")
                        if isinstance(_nm, str) and _nm:
                            wire["name"] = _nm
                        break
        return wire

    def _finalize_payload(
        self,
        messages: list[dict[str, Any]],
        request: LLMStreamRequest,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Assemble the OpenAI-compatible body shared by both history paths."""
        # V1 parity (chat_handler.py:1861-1866): the daemon expects the BARE
        # model name. Strip any residual ``local::`` prefix (defensive final
        # guard — the resolver normally strips it, but some routing paths fall
        # back to the raw ``model_hint``). A bare ``local::`` (auto-select with
        # no default configured) collapses to an empty string so the daemon
        # uses its built-in default model rather than trying to load a model
        # literally named ``local::``.
        _model = request.model_hint or self._model_name
        if isinstance(_model, str) and _model.startswith("local::"):
            _model = _model[len("local::") :]
        payload: dict[str, Any] = {
            "model": _model,
            "messages": messages,
            "stream": True,
        }
        # V1 parity (chat_handler.py:1872): forward the conversation id as
        # ``session_id`` so GenieAPIService can key its per-session
        # PromptOptimizer state / cache to this chat. V1 always sent it;
        # dropping it made the daemon treat every turn as a brand-new
        # session. ``conversation_id`` is the V2 name for the same value; it
        # may be a value-object (``.value`` holds the string) or a raw str.
        _conv_id = getattr(request, "conversation_id", None)
        _session_id = getattr(_conv_id, "value", _conv_id)
        if _session_id is not None and str(_session_id):
            payload["session_id"] = str(_session_id)
        # V1 parity (local_prompt_builder.py:3,109 + chat_handler.py:1854-1873):
        # the local path ADVERTISES the canonical tool schemas on the payload
        # so GenieAPIService's PromptOptimizer.OptimizeToolsPrompt() can compress
        # them into the on-device-model-friendly format. V2 previously dropped
        # this entirely (neither payload.tools nor a <tools> XML block), so an
        # on-device model only saw the natural-language tools_intro and could
        # not reliably emit tool calls. The schemas ride in on
        # ``extra["tools_schemas"]`` (same list the cloud adapter advertises);
        # forward them verbatim. A local runtime that does not understand the
        # ``tools`` field simply ignores it, so this is safe even pre-SDK.
        tools_schemas = extra.get("tools_schemas")
        if isinstance(tools_schemas, (list, tuple)) and tools_schemas:
            advertised = [t for t in tools_schemas if isinstance(t, dict)]
            if advertised:
                payload["tools"] = advertised
        return payload

    @staticmethod
    def _inject_local_protocol(
        system_prompt: str, extra: dict[str, Any]
    ) -> str:
        """P1-3: ensure the local-model system prompt carries ``agent=main``.

        V1 ``local_prompt_builder.py:10-14`` prepends ``agent=main`` so the
        GenieAPIService PromptOptimizer takes the MAIN_AGENT path, and
        ``local_prompt_builder.py:93`` appends the ``<available_skills>``
        XML so PromptOptimizer parses the skill catalog.

        As of the local simplified-prompt path
        (``streaming.StreamChatUseCase._build_local_system_prompt``), the
        use case now builds the COMPLETE minimal prompt — base system (if
        any) + ``agent=main`` + ``<available_skills>`` metadata XML — and
        stores it on ``extra["system_prompt"]`` so the wire payload AND the
        debug snapshot share one source of truth.  This method is therefore
        IDEMPOTENT: when the prompt already starts with the ``agent=main``
        marker it is returned verbatim (no duplicate marker, no second
        ``<available_skills>`` block, no cloud catalog leakage).

        The legacy prepend remains as a defensive fallback for any caller
        that hands us a prompt WITHOUT the marker (e.g. a minimal test
        harness that sets ``extra["system_prompt"]`` directly).
        """
        # Already built by the use case (the normal path) — leave verbatim.
        if system_prompt.lstrip().startswith(_LOCAL_AGENT_MAIN_MARKER):
            return system_prompt

        # Defensive fallback: a caller supplied a raw system prompt without
        # the marker.  Prepend ``agent=main`` only (do NOT re-derive a
        # ``<available_skills>`` block here — the use case owns skill
        # discovery; the old ``extra["skill_catalog"]`` path was dead code
        # that emitted a wrong ``<skill path=.. use_for=../>`` shape).
        return f"{_LOCAL_AGENT_MAIN_MARKER}\n\n{system_prompt}"

    def _next_frame_id(self, prefix: str) -> str:
        return f"local-{prefix}-{self._ids.new_id()}"

    def _make_client(self) -> httpx.AsyncClient:
        """Build the HTTP client (test seam via ``client_factory``).

        ``client_factory`` mirrors the cloud
        :class:`HttpOpenAICompatibleLLMStream` convention
        (``(*, timeout) -> httpx.AsyncClient``) so tests can inject an
        :class:`httpx.MockTransport` without hitting the network.
        """
        if self._client_factory is not None:
            return self._client_factory(timeout=self._timeout_seconds)
        # V1 parity (chat_handler.py:1895-1900): split connect / read so the
        # SSE read can run long (default 1800s — covers long-text summarisation
        # / Phase -1 inference, which keep the connection alive with periodic
        # ``summarizing`` keep-alive frames) while connect fails fast at 30s.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=self._timeout_seconds)
        )
