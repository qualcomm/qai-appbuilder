# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""One-shot retry when a tool call is cut off by ``finish_reason=length``.

External-skill bug (2026-11): a model asked to ``write`` a large file hit
``max_tokens`` mid-argument; the incomplete tool call was discarded and the
user only got a "truncated" notice. ``HttpOpenAICompatibleLLMStream`` now
re-issues the request ONCE with a larger ``max_tokens`` (2x, capped at the
family ceiling passed by the routing layer as ``__max_tokens_ceiling__``).

A mock ``httpx`` transport stands in for the provider: the first response is a
truncated tool call, the second is a complete one. Plain asserts +
``asyncio.run`` (no async pytest plugin needed).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from qai.chat.application.ports import LLMStreamRequest
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrameType
from qai.chat.infrastructure.llm_stream import HttpOpenAICompatibleLLMStream
from tests.unit.qai.chat.fakes import FakeIdGenerator

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "write a file",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _sse(*events: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def _tool_call_event(args: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "write", "arguments": args},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }


def _finish_event(reason: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


_USAGE = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

_TRUNCATED = _sse(_tool_call_event('{"path": "a.md", "content": "par'), _finish_event("length"), _USAGE)
_COMPLETE = _sse(_tool_call_event('{"path": "a.md", "content": "full"}'), _finish_event("tool_calls"), _USAGE)


class _Recorder:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)
        self.payloads: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        body = self._responses.pop(0)
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )


def _run(
    responses: list[bytes], extra: dict[str, Any]
) -> tuple[list[Any], list[dict[str, Any]]]:
    rec = _Recorder(responses)

    def client_factory(*, timeout: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))

    stream = HttpOpenAICompatibleLLMStream(
        base_url="http://llm.test/v1",
        api_key="k",
        model="qwen3.7-max",
        ids=FakeIdGenerator(),
        client_factory=client_factory,
    )
    request = LLMStreamRequest(
        conversation_id=ConversationId("c-1"),
        tab_id=TabId("t-1"),
        prompt=MessageContent(text="write the report"),
        history=(),
        model_hint="qwen3.7-max",
        extra={"tools_schemas": _TOOLS, **extra},
    )

    async def collect() -> list[Any]:
        return [f async for f in stream.stream(request)]

    return asyncio.run(collect()), rec.payloads


def _types(frames: list[Any]) -> list[StreamFrameType]:
    return [f.frame_type for f in frames]


def test_truncated_tool_call_is_retried_once_with_doubled_max_tokens() -> None:
    frames, payloads = _run(
        [_TRUNCATED, _COMPLETE],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 131072},
    )
    assert [p["max_tokens"] for p in payloads] == [4096, 8192]
    # The complete call from the retry is delivered; no truncation notice.
    types = _types(frames)
    assert StreamFrameType.TOOL_CALL in types
    assert types[-1] == StreamFrameType.END
    assert types.count(StreamFrameType.END) == 1
    assert not any(
        f.frame_type == StreamFrameType.CHUNK and "截断" in str(getattr(f, "payload", ""))
        for f in frames
    )


def test_retry_is_capped_at_the_family_ceiling() -> None:
    _, payloads = _run(
        [_TRUNCATED, _COMPLETE],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 6000},
    )
    assert [p["max_tokens"] for p in payloads] == [4096, 6000]


def test_frame_sequences_continue_across_the_retry() -> None:
    frames, _ = _run(
        [_TRUNCATED, _COMPLETE],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 131072},
    )
    seqs = [f.sequence for f in frames]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_second_truncation_is_not_retried_again() -> None:
    frames, payloads = _run(
        [_TRUNCATED, _TRUNCATED],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 131072},
    )
    assert len(payloads) == 2  # exactly one retry, never a third request
    assert StreamFrameType.TOOL_CALL not in _types(frames)
    assert _types(frames)[-1] == StreamFrameType.END


def test_no_retry_without_a_known_ceiling() -> None:
    # Unknown family / no ceiling => behaviour is exactly the pre-change one:
    # a single request, the truncated call discarded, notice + END emitted.
    frames, payloads = _run([_TRUNCATED], {"max_tokens": 4096})
    assert len(payloads) == 1
    assert StreamFrameType.TOOL_CALL not in _types(frames)
    assert _types(frames)[-1] == StreamFrameType.END


def test_no_retry_when_already_at_the_ceiling() -> None:
    frames, payloads = _run(
        [_TRUNCATED],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 4096},
    )
    assert len(payloads) == 1
    assert _types(frames)[-1] == StreamFrameType.END


def test_no_retry_when_no_max_tokens_was_sent() -> None:
    _, payloads = _run([_TRUNCATED], {"__max_tokens_ceiling__": 131072})
    assert len(payloads) == 1
    assert "max_tokens" not in payloads[0]


def test_a_complete_tool_call_makes_a_single_request() -> None:
    frames, payloads = _run(
        [_COMPLETE],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 131072},
    )
    assert len(payloads) == 1
    assert StreamFrameType.TOOL_CALL in _types(frames)


def test_control_keys_never_reach_the_wire() -> None:
    _, payloads = _run(
        [_TRUNCATED, _COMPLETE],
        {"max_tokens": 4096, "__max_tokens_ceiling__": 131072},
    )
    for p in payloads:
        assert not any(k.startswith("__") for k in p)


# ── routing layer: where the ceiling comes from ─────────────────────────────


def test_routing_layer_publishes_the_family_ceiling_for_the_retry() -> None:
    from qai.chat.infrastructure.provider_routing_stream import (
        _inject_family_sampling_defaults,
    )

    extra: dict[str, Any] = {}
    _inject_family_sampling_defaults(extra, model_id="qwen3.7-max", is_local=False)
    assert extra["max_tokens"] == 32768
    assert extra["__max_tokens_ceiling__"] == 131072

    # A family override moves the ceiling too.
    extra = {}
    _inject_family_sampling_defaults(
        extra,
        model_id="qwen3.7-max",
        is_local=False,
        family_overrides={"qwen": {"max_tokens_max": 20000}},
    )
    assert extra["__max_tokens_ceiling__"] == 20000


def test_unknown_family_publishes_no_ceiling_so_no_retry_happens() -> None:
    from qai.chat.infrastructure.provider_routing_stream import (
        _inject_family_sampling_defaults,
    )

    extra: dict[str, Any] = {}
    _inject_family_sampling_defaults(extra, model_id="e2e-model", is_local=False)
    assert "max_tokens" not in extra
    assert "__max_tokens_ceiling__" not in extra


# ── deferred "generation truncated" notice (plain-text length truncation) ───

_TEXT_EVENT = {"choices": [{"index": 0, "delta": {"content": "partial answer"}, "finish_reason": None}]}
_TEXT_TRUNCATED = _sse(_TEXT_EVENT, _finish_event("length"), _USAGE)


def _chunk_texts(frames: list[Any]) -> str:
    return "".join(
        f.payload.get("text", "") for f in frames if f.frame_type == StreamFrameType.CHUNK
    )


def test_plain_text_truncation_keeps_the_in_stream_notice_by_default() -> None:
    # Every non-main-agent caller (sub-agents, discussion, summaries ...) relies on
    # this notice as its ONLY truncation signal -> default must not change.
    frames, _ = _run([_TEXT_TRUNCATED], {"max_tokens": 4096})
    assert "生成被截断" in _chunk_texts(frames)
    assert frames[-1].payload.get("truncated") is True


def test_plain_text_truncation_notice_is_withheld_when_the_caller_defers_it() -> None:
    frames, _ = _run(
        [_TEXT_TRUNCATED], {"max_tokens": 4096, "__defer_length_notice__": True}
    )
    text = _chunk_texts(frames)
    assert text == "partial answer"
    # END still tells the caller it was truncated (that is what triggers auto-continue).
    assert frames[-1].frame_type == StreamFrameType.END
    assert frames[-1].payload.get("truncated") is True
    # ... and a deferred notice must not look like an empty-response failure.
    assert not any(f.frame_type == StreamFrameType.ERROR for f in frames)


def test_deferred_notice_with_no_text_is_not_reported_as_an_empty_response() -> None:
    only_finish = _sse(_finish_event("length"), _USAGE)
    frames, _ = _run(
        [only_finish], {"max_tokens": 4096, "__defer_length_notice__": True}
    )
    assert not any(f.frame_type == StreamFrameType.ERROR for f in frames)


def test_defer_flag_never_reaches_the_wire() -> None:
    _, payloads = _run(
        [_TEXT_TRUNCATED], {"max_tokens": 4096, "__defer_length_notice__": True}
    )
    assert not any(k.startswith("__") for k in payloads[0])


def test_the_application_layer_notice_matches_the_infrastructure_wording() -> None:
    # streaming.py cannot import infrastructure, so it carries a literal copy of the
    # plain-text notice; guard against the two drifting apart.
    from qai.chat.application.use_cases.streaming import _LENGTH_TRUNCATION_NOTICE_ZH
    from qai.chat.infrastructure.notice_text import make_truncation_notice

    assert _LENGTH_TRUNCATION_NOTICE_ZH == make_truncation_notice(
        tool_name=None, language="zh"
    )


def test_only_ordinary_cloud_turns_defer_the_notice() -> None:
    from qai.chat.application.use_cases.streaming import _defers_length_notice

    assert _defers_length_notice("qai-service::qwen3.7-max") is True
    assert _defers_length_notice("deepseek-v4.1-flash") is True
    assert _defers_length_notice("local::some-model") is False
    assert _defers_length_notice("query::cebot") is False
