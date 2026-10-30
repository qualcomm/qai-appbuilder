# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Local-model stream: daemon ``loading`` gate, usage passthrough, inline ``<think>``.

Ports (a) the ``TestModelLoadingGate`` / ``TestUsagePassthrough`` /
``TestExtractUsage`` classes from the internal ``test_local_model_stream.py``
(a63d4f26) and (b) coverage for the inline ``<think>`` splitter (0e5b1373).
"""

from __future__ import annotations

import httpx
import pytest

from qai.chat.adapters.local_model_stream import (
    _SERVICE_LOADING_MESSAGE,
    LocalModelStreamAdapter,
    _ThinkSplitter,
)
from qai.chat.application.ports import LLMStreamRequest
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrameType
from qai.platform.ids import UlidGenerator

pytestmark = pytest.mark.unit


def _factory_for(body: bytes):
    def _factory(*, timeout: float) -> httpx.AsyncClient:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            timeout=httpx.Timeout(timeout),
        )

    return _factory


def _make_request() -> LLMStreamRequest:
    return LLMStreamRequest(
        conversation_id=ConversationId.of("conv-1"),
        tab_id=TabId.of("tab-1"),
        prompt=MessageContent(text="hello"),
        history=(),
        model_hint="qwen3-8b-8380",
    )


_OK_SSE_BODY = (
    b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":""}]}\n\n'
    b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


def _gate_factory(status_handler, requests: list[httpx.Request]):
    def _factory(*, timeout: float) -> httpx.AsyncClient:
        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return status_handler(request)
            return httpx.Response(
                200,
                content=_OK_SSE_BODY,
                headers={"content-type": "text/event-stream"},
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            timeout=httpx.Timeout(timeout),
        )

    return _factory


async def _running() -> bool:
    return True


@pytest.mark.asyncio
class TestModelLoadingGate:
    """Request is held back while the daemon's ``GET /status`` says loading."""

    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "qai.chat.adapters.local_model_stream._MODEL_LOADING_POLL_SECONDS",
            0.001,
        )

    @staticmethod
    def _adapter(factory, *, with_provider: bool = True) -> LocalModelStreamAdapter:
        return LocalModelStreamAdapter(
            base_url="http://127.0.0.1:9999/v1",
            ids=UlidGenerator(),
            client_factory=factory,
            service_running_provider=_running if with_provider else None,
        )

    async def test_waits_while_loading_then_posts(self) -> None:
        requests: list[httpx.Request] = []
        answers = iter(["true", "true", "false"])

        def _status(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"loading": next(answers)})

        adapter = self._adapter(_gate_factory(_status, requests))
        frames = [f async for f in adapter.stream(_make_request())]

        types = [f.frame_type for f in frames]
        pulses = [f for f in frames if f.frame_type is StreamFrameType.LIVENESS_PULSE]
        assert len(pulses) == 2
        assert all(p.payload.get("status") == "model_loading" for p in pulses)
        first_chunk = types.index(StreamFrameType.CHUNK)
        assert max(i for i, t in enumerate(types) if t is StreamFrameType.LIVENESS_PULSE) < first_chunk
        assert types[-1] is StreamFrameType.END
        assert [(r.method, r.url.path) for r in requests] == [
            ("GET", "/status"),
            ("GET", "/status"),
            ("GET", "/status"),
            ("POST", "/v1/chat/completions"),
        ]

    async def test_status_404_skips_wait(self) -> None:
        requests: list[httpx.Request] = []
        adapter = self._adapter(
            _gate_factory(lambda r: httpx.Response(404), requests)
        )
        frames = [f async for f in adapter.stream(_make_request())]

        assert not any(
            f.frame_type is StreamFrameType.LIVENESS_PULSE for f in frames
        )
        assert frames[-1].frame_type is StreamFrameType.END
        assert [r.method for r in requests] == ["GET", "POST"]

    async def test_status_connect_error_treated_as_not_loading(self) -> None:
        requests: list[httpx.Request] = []

        def _status(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        adapter = self._adapter(_gate_factory(_status, requests))
        frames = [f async for f in adapter.stream(_make_request())]

        assert not any(
            f.frame_type is StreamFrameType.LIVENESS_PULSE for f in frames
        )
        assert "hi" in "".join(
            f.payload["text"]
            for f in frames
            if f.frame_type is StreamFrameType.CHUNK
        )
        assert [r.method for r in requests] == ["GET", "POST"]

    async def test_wait_timeout_yields_loading_fallback_without_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "qai.chat.adapters.local_model_stream._MODEL_LOADING_WAIT_SECONDS",
            0.05,
        )
        requests: list[httpx.Request] = []
        adapter = self._adapter(
            _gate_factory(
                lambda r: httpx.Response(200, json={"loading": "true"}), requests
            )
        )
        frames = [f async for f in adapter.stream(_make_request())]

        assert any(f.frame_type is StreamFrameType.LIVENESS_PULSE for f in frames)
        codes = [
            f.payload.get("code")
            for f in frames
            if f.frame_type is StreamFrameType.ERROR
        ]
        assert codes == ["chat.local.service_loading"]
        chunks = [
            f.payload["text"]
            for f in frames
            if f.frame_type is StreamFrameType.CHUNK
        ]
        assert chunks == [_SERVICE_LOADING_MESSAGE]
        assert frames[-1].frame_type is StreamFrameType.END
        assert all(r.method == "GET" for r in requests)

    async def test_no_provider_never_requests_status(self) -> None:
        requests: list[httpx.Request] = []
        adapter = self._adapter(
            _gate_factory(
                lambda r: httpx.Response(200, json={"loading": "true"}), requests
            ),
            with_provider=False,
        )
        frames = [f async for f in adapter.stream(_make_request())]

        assert frames[-1].frame_type is StreamFrameType.END
        assert not any(r.url.path.endswith("/status") for r in requests)
        assert [r.method for r in requests] == ["POST"]


_ZERO_USAGE = b'"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}'


@pytest.mark.asyncio
class TestUsagePassthrough:
    """GenieAPIService's real terminal-chunk usage is carried on the END frame."""

    @staticmethod
    async def _end_payload(body: bytes) -> dict:
        adapter = LocalModelStreamAdapter(
            base_url="http://127.0.0.1:9999/v1",
            ids=UlidGenerator(),
            client_factory=_factory_for(body),
        )
        frames = [f async for f in adapter.stream(_make_request())]
        ends = [f for f in frames if f.frame_type is StreamFrameType.END]
        assert len(ends) == 1
        return ends[0].payload

    async def test_terminal_chunk_usage_reaches_end_frame(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":""}],'
            + _ZERO_USAGE
            + b"}\n\n"
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1234,"completion_tokens":56,"total_tokens":1290}}\n\n'
            b"data: [DONE]\n\n"
        )
        payload = await self._end_payload(body)
        assert payload["reason"] == "completed"
        assert payload["usage"]["prompt_tokens"] == 1234
        assert payload["usage"]["completion_tokens"] == 56
        assert payload["usage"]["total_tokens"] == 1290

    async def test_all_zero_or_missing_usage_omits_usage_key(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":""}],'
            + _ZERO_USAGE
            + b"}\n\n"
            b'data: {"choices":[{"delta":{"content":" there"},"finish_reason":""}]}\n\n'
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}],'
            + _ZERO_USAGE
            + b"}\n\n"
            b"data: [DONE]\n\n"
        )
        payload = await self._end_payload(body)
        assert "usage" not in payload

    async def test_tool_calls_terminal_chunk_usage_reaches_end_frame(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"read","arguments":"{}"}}]},"finish_reason":""}],'
            + _ZERO_USAGE
            + b"}\n\n"
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"tool_calls"}],'
            b'"usage":{"prompt_tokens":800,"completion_tokens":12,"total_tokens":812}}\n\n'
            b"data: [DONE]\n\n"
        )
        adapter = LocalModelStreamAdapter(
            base_url="http://127.0.0.1:9999/v1",
            ids=UlidGenerator(),
            client_factory=_factory_for(body),
        )
        frames = [f async for f in adapter.stream(_make_request())]
        assert (
            sum(1 for f in frames if f.frame_type is StreamFrameType.TOOL_CALL) == 1
        )
        assert frames[-1].frame_type is StreamFrameType.END
        assert frames[-1].payload["usage"] == {
            "prompt_tokens": 800,
            "completion_tokens": 12,
            "total_tokens": 812,
        }

    async def test_total_tokens_recomputed_from_prompt_plus_completion(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":""}]}\n\n'
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":9999}}\n\n'
            b"data: [DONE]\n\n"
        )
        payload = await self._end_payload(body)
        assert payload["usage"]["total_tokens"] == 120

    async def test_non_numeric_prompt_tokens_does_not_crash_and_omits_usage(
        self,
    ) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":""}]}\n\n'
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":"abc","completion_tokens":5,"total_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        payload = await self._end_payload(body)
        assert payload["reason"] == "completed"
        assert "usage" not in payload


class TestExtractUsage:
    @pytest.mark.parametrize(
        "event",
        [
            None,
            "not-a-dict",
            [],
            {},
            {"choices": []},
            {"usage": None},
            {"usage": "oops"},
            {"usage": {}},
            {"usage": {"prompt_tokens": 0, "completion_tokens": 9}},
            {"usage": {"prompt_tokens": -3, "completion_tokens": 9}},
            {"usage": {"prompt_tokens": "x", "completion_tokens": 9}},
        ],
    )
    def test_extract_usage_returns_none_for_unusable_input(self, event) -> None:
        assert LocalModelStreamAdapter._extract_usage(event) is None

    def test_extract_usage_normalises_valid_input(self) -> None:
        assert LocalModelStreamAdapter._extract_usage(
            {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 1}}
        ) == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        assert LocalModelStreamAdapter._extract_usage(
            {"usage": {"prompt_tokens": 7}}
        ) == {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}


def _feed_all(chunks: list[str]) -> list[tuple[bool, str]]:
    sp = _ThinkSplitter()
    out: list[tuple[bool, str]] = []
    for c in chunks:
        out.extend(sp.feed(c))
    out.extend(sp.flush())
    return out


def _join(segs: list[tuple[bool, str]], reasoning: bool) -> str:
    return "".join(t for r, t in segs if r is reasoning)


class TestThinkSplitter:
    def test_plain_text_passes_through(self) -> None:
        assert _feed_all(["hello ", "world"]) == [(False, "hello "), (False, "world")]

    def test_inline_think_block_becomes_reasoning(self) -> None:
        segs = _feed_all(["<think>plan it</think>\n\nanswer"])
        assert _join(segs, True) == "plan it"
        assert _join(segs, False) == "answer"

    def test_tags_split_across_deltas(self) -> None:
        segs = _feed_all(["<thi", "nk>ab", "c</th", "ink>ok"])
        assert _join(segs, True) == "abc"
        assert _join(segs, False) == "ok"

    def test_stray_close_tag_without_open_is_dropped(self) -> None:
        # Chat templates that pre-open <think> only ever emit the closer.
        segs = _feed_all(["thinking...</think>answer"])
        assert _join(segs, False) == "thinking...answer"
        assert _join(segs, True) == ""

    def test_unterminated_think_is_flushed_as_reasoning(self) -> None:
        segs = _feed_all(["<think>never closed"])
        assert _join(segs, True) == "never closed"
        assert _join(segs, False) == ""

    def test_lone_angle_bracket_is_released_on_flush(self) -> None:
        segs = _feed_all(["a <"])
        assert _join(segs, False) == "a <"


@pytest.mark.asyncio
class TestThinkSplitInStream:
    async def test_reasoning_frames_and_visible_text(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"<think>hm"},"finish_reason":""}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"m</think>\\n\\nhi"},"finish_reason":""}]}\n\n'
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        adapter = LocalModelStreamAdapter(
            base_url="http://127.0.0.1:9999/v1",
            ids=UlidGenerator(),
            client_factory=_factory_for(body),
        )
        frames = [f async for f in adapter.stream(_make_request())]
        reasoning = "".join(
            f.payload["text"] for f in frames if f.frame_type is StreamFrameType.REASONING
        )
        visible = "".join(
            f.payload["text"] for f in frames if f.frame_type is StreamFrameType.CHUNK
        )
        assert reasoning == "hmm"
        assert visible == "hi"
        assert "<think>" not in visible and "</think>" not in visible
        assert frames[-1].frame_type is StreamFrameType.END
