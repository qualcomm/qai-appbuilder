# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``local_model_stream.end``: one summary line per local-model stream.

The line must tell a reply the model finished from one the service cut off,
a length-capped reply, an HTTP error and a stream the caller closed.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from qai.chat.adapters.local_model_stream import LocalModelStreamAdapter
from qai.chat.application.ports import LLMStreamRequest
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.platform.ids import UlidGenerator
from qai.platform.logging import configure_logging

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _sse(*events: dict | str) -> bytes:
    out = []
    for ev in events:
        data = ev if isinstance(ev, str) else json.dumps(ev)
        out.append(f"data: {data}\n\n")
    return "".join(out).encode("utf-8")


def _delta(text: str, finish: str | None = "") -> dict:
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


def _factory(body: bytes, status: int = 200):
    def _f(*, timeout):
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, content=body, headers={"content-type": "text/event-stream"},
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), timeout=httpx.Timeout(timeout),
        )

    return _f


def _adapter(body: bytes, status: int = 200) -> LocalModelStreamAdapter:
    return LocalModelStreamAdapter(
        base_url="http://127.0.0.1:9999/v1",
        ids=UlidGenerator(),
        client_factory=_factory(body, status),
    )


def _request() -> LLMStreamRequest:
    return LLMStreamRequest(
        conversation_id=ConversationId.of("conv-1"),
        tab_id=TabId.of("tab-1"),
        prompt=MessageContent(text="hello"),
        history=(),
        model_hint="qwen3-8b-8380",
    )


@pytest.fixture
def log_stream():
    stream = io.StringIO()
    configure_logging(stream=stream, fmt="json")
    yield stream
    configure_logging(stream=io.StringIO())


def _end_records(stream: io.StringIO) -> list[dict]:
    out = []
    for line in stream.getvalue().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "local_model_stream.end":
            out.append(rec)
    return out


async def _drain(adapter: LocalModelStreamAdapter) -> list:
    return [f async for f in adapter.stream(_request())]


async def test_finished_reply(log_stream) -> None:
    body = _sse(
        _delta("Hello "),
        _delta("world", "stop"),
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}},
        "[DONE]",
    )
    await _drain(_adapter(body))

    (rec,) = _end_records(log_stream)
    assert rec["outcome"] == "ended"
    assert rec["level"] == "INFO"
    assert rec["finish_reason"] == "stop"
    assert rec["done_seen"] is True
    assert rec["http_status"] == 200
    assert rec["chars"] == len("Hello world")
    assert rec["chunks"] >= 1
    assert rec["error_code"] is None
    assert rec["end_reason"] == "completed"
    assert isinstance(rec["duration_ms"], int)


async def test_stream_cut_off_by_service(log_stream) -> None:
    """No finish_reason and no [DONE]: the HTTP stream just ended."""
    await _drain(_adapter(_sse(_delta("Half a sen"))))

    (rec,) = _end_records(log_stream)
    assert rec["outcome"] == "cut_off_by_service"
    assert rec["level"] == "WARNING"
    assert rec["finish_reason"] is None
    assert rec["done_seen"] is False
    assert rec["chars"] == len("Half a sen")


async def test_length_capped_reply_is_a_warning(log_stream) -> None:
    await _drain(_adapter(_sse(_delta("long text", "length"), "[DONE]")))

    (rec,) = _end_records(log_stream)
    assert rec["finish_reason"] == "length"
    assert rec["truncated"] is True
    assert rec["level"] == "WARNING"


async def test_http_error(log_stream) -> None:
    await _drain(_adapter(b'{"error": "model crashed"}', status=500))

    (rec,) = _end_records(log_stream)
    assert rec["http_status"] == 500
    assert rec["error_code"] == "chat.local.http_error"
    assert rec["end_reason"] == "failed"
    assert rec["level"] == "WARNING"
    errors = [
        json.loads(line)
        for line in log_stream.getvalue().splitlines()
        if '"local_model_stream.http_error"' in line
    ]
    assert errors and errors[0]["status"] == 500
    assert "model crashed" in errors[0]["body"]


async def test_closed_by_caller(log_stream) -> None:
    body = _sse(_delta("a"), _delta("b"), _delta("c", "stop"), "[DONE]")
    agen = _adapter(body).stream(_request())
    await agen.__anext__()
    await agen.aclose()

    (rec,) = _end_records(log_stream)
    assert rec["outcome"] == "closed_by_caller"
    assert rec["level"] == "INFO"


async def test_close_after_end_frame_is_a_normal_end(log_stream) -> None:
    """The use case stops reading at END and closes the stream: not 'closed_by_caller'."""
    agen = _adapter(b'{"error": "insufficient_memory"}', status=500).stream(_request())
    async for frame in agen:
        if frame.frame_type.value == "end":
            break
    await agen.aclose()

    (rec,) = _end_records(log_stream)
    assert rec["outcome"] == "ended"
    assert rec["http_status"] == 500
    assert rec["error_code"] == "chat.local.http_error"
    assert rec["level"] == "WARNING"


async def test_frames_are_unchanged(log_stream) -> None:
    """The summary wrapper must not alter what the caller receives."""
    body = _sse(_delta("Hi", "stop"), "[DONE]")
    adapter = _adapter(body)
    wrapped = [(f.frame_type, dict(f.payload)) for f in await _drain(adapter)]
    raw = [(f.frame_type, dict(f.payload)) async for f in adapter._run(_request())]
    assert wrapped == raw


# ── empty replies / service errors inside a 200 stream ───────────────────


def _status(msg: str = "Inferencing...") -> dict:
    return {"choices": [{"delta": {"content": ""}, "status": "inference", "status_message": msg}]}


async def test_empty_reply_is_a_warning_with_preview_and_request_size(log_stream) -> None:
    """HTTP 200, a status frame, [DONE] and nothing else: the reply was empty."""
    frames = await _drain(_adapter(_sse(_status(), "[DONE]")))

    (rec,) = _end_records(log_stream)
    assert rec["outcome"] == "empty_reply"
    assert rec["level"] == "WARNING"
    assert rec["request_messages"] >= 1
    assert rec["request_est_tokens"] > 0
    assert rec["request_bytes"] >= rec["request_est_tokens"] * 4
    assert any("inference" in line for line in rec["sse_preview"])
    end = [f for f in frames if f.frame_type.value == "end"][-1]
    assert end.payload["reason"] == "completed"
    assert end.payload["request_est_tokens"] == rec["request_est_tokens"]


async def test_finished_reply_has_no_preview(log_stream) -> None:
    await _drain(_adapter(_sse(_delta("ok", "stop"), "[DONE]")))
    (rec,) = _end_records(log_stream)
    assert rec["outcome"] == "ended"
    assert "sse_preview" not in rec
    assert rec["request_est_tokens"] > 0


async def test_service_context_overflow_becomes_an_error_frame(log_stream) -> None:
    """GenieAPIService: pre-filter cannot fit the request -> error object + [DONE]."""
    err = {
        "error": {
            "message": "Failed to process request: Protected messages (last user message and "
            "all subsequent messages) exceed context size (16384 tokens). Current total: "
            "22593 tokens. Cannot compress further.",
            "type": "internal_error",
            "code": "build_failed",
        }
    }
    frames = await _drain(_adapter(_sse(_status("Preparing"), err, "[DONE]")))

    errs = [f for f in frames if f.frame_type.value == "error"]
    assert len(errs) == 1
    assert errs[0].payload["code"] == "chat.local.context_overflow"
    assert "Cannot compress further" in errs[0].payload["message"]
    assert frames[-1].frame_type.value == "end"
    assert frames[-1].payload["reason"] == "failed"

    (rec,) = _end_records(log_stream)
    assert rec["error_code"] == "chat.local.context_overflow"
    assert rec["level"] == "WARNING"
    assert "Cannot compress further" in rec["service_error"]
    svc = [
        json.loads(line)
        for line in log_stream.getvalue().splitlines()
        if '"local_model_stream.service_error"' in line
    ]
    assert svc and svc[0]["context_overflow"] is True


async def test_other_service_error_becomes_an_error_frame(log_stream) -> None:
    err = {"error": {"message": "model not loaded", "code": "build_failed"}}
    frames = await _drain(_adapter(_sse(err, "[DONE]")))
    errs = [f for f in frames if f.frame_type.value == "error"]
    assert errs and errs[0].payload["code"] == "chat.local.service_error"
    assert "model not loaded" in errs[0].payload["message"]
    (rec,) = _end_records(log_stream)
    assert rec["error_code"] == "chat.local.service_error"
