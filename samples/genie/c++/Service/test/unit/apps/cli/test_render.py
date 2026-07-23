"""Unit tests for ``apps.cli._render`` (cli-render-redesign plan, Step 1).

Exercises every ``StreamFrameRenderer._on_*`` handler and the
``RunFrameRenderer``/``render_run_output``/``friendly_error`` surface
through injected ``io.StringIO`` sinks — no real TTY, no network, no
``qai.*`` context imports (the module under test has none).
"""

from __future__ import annotations

import io

import pytest

from apps.cli._render import (
    RenderOptions,
    RunFrameRenderer,
    RunResult,
    StreamFrameRenderer,
    friendly_error,
    render_run_output,
)


class _Frame:
    """Minimal duck-typed stand-in for a ``StreamFrame``."""

    __slots__ = ("frame_type", "payload")

    def __init__(self, frame_type: str, payload: dict) -> None:
        self.frame_type = frame_type
        self.payload = payload


def _renderer(*, color: bool, emoji: bool, fold_lines: int = 20):
    out, err = io.StringIO(), io.StringIO()
    opts = RenderOptions(color=color, emoji=emoji, fold_lines=fold_lines)
    return StreamFrameRenderer(opts, out=out, err=err), out, err


@pytest.mark.parametrize("color", [True, False])
@pytest.mark.parametrize("emoji", [True, False])
class TestStreamFrameRendererFrameTypes:
    """One assertion per frame type, across the color/emoji matrix."""

    def test_chunk(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("chunk", {"text": "hello world"}))
        assert "hello world" in out.getvalue()

    def test_tool_call(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("tool_call", {"tool_name": "exec", "arguments": {"command": "ls -la"}}))
        text = out.getvalue()
        assert "exec" in text
        assert "ls -la" in text

    def test_tool_result_short(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("tool_result", {"result": "line1\nline2"}))
        text = out.getvalue()
        assert "line1" in text and "line2" in text
        assert r.folded() is None

    def test_tool_mode_changed(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("tool_mode_changed", {"mode": "auto-approve"}))
        assert "auto-approve" in out.getvalue()

    def test_turn_warning(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("turn_warning", {"turn_count": 30}))
        assert "30" in out.getvalue()

    def test_error(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("error", {"code": "BOOM", "message": "went wrong"}))
        text = out.getvalue()
        assert "BOOM" in text and "went wrong" in text

    def test_end(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("end", {"usage": {"total_tokens": 42}}))
        assert "42" in out.getvalue()

    def test_subagent_start(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("subagent_start", {"index": 1, "total": 3, "prompt_preview": "do X"}))
        text = out.getvalue()
        assert "1/3" in text and "do X" in text

    def test_subagent_output(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("subagent_output", {"content": "sub says hi"}))
        assert "sub says hi" in out.getvalue()

    def test_subagent_tool(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("subagent_tool", {"tool_name": "grep"}))
        assert "grep" in out.getvalue()

    def test_subagent_done(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("subagent_done", {"index": 2}))
        assert "2" in out.getvalue()

    def test_subagent_error(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("subagent_error", {"index": 2, "message": "sub failed"}))
        assert "sub failed" in out.getvalue()

    def test_agent_summary(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("agent_summary", {"total_agents": 5}))
        assert "5" in out.getvalue()

    def test_unknown_frame_type(self, color, emoji):
        r, out, _ = _renderer(color=color, emoji=emoji)
        r.render(_Frame("mystery_frame", {"a": 1}))
        assert "mystery_frame" in out.getvalue()


def test_chunk_frame_breaks_before_next_line_frame():
    r, out, _ = _renderer(color=False, emoji=False)
    r.render(_Frame("chunk", {"text": "partial"}))
    r.render(_Frame("chunk", {"text": " reply"}))
    r.render(_Frame("tool_call", {"tool_name": "read", "arguments": {}}))
    text = out.getvalue()
    assert "partial reply" in text
    assert text.index("partial reply") < text.index("read")


def test_tool_result_partial_stream_writes_delta_inline():
    r, out, _ = _renderer(color=False, emoji=False)
    r.render(_Frame("tool_result", {"partial": True, "delta": "chunk-a"}))
    r.render(_Frame("tool_result", {"partial": True, "delta": "chunk-b"}))
    assert "chunk-achunk-b" in out.getvalue()


def test_tool_result_folding_registers_and_show_retrieves():
    r, out, _ = _renderer(color=False, emoji=False, fold_lines=3)
    long_text = "\n".join(f"line{i}" for i in range(10))
    r.render(_Frame("tool_result", {"result": long_text}))
    text = out.getvalue()
    assert "line0" in text and "line2" in text
    assert "line9" not in text  # folded away
    assert "/show 1" in text
    assert r.last_fold_index == 1
    assert r.folded(1) == long_text
    assert r.folded() == long_text  # default = most recent
    assert r.folded(2) is None


def test_tool_result_folding_json_payload_uses_syntax_highlight():
    r, out, _ = _renderer(color=False, emoji=False, fold_lines=2)
    payload = {"items": list(range(10))}
    r.render(_Frame("tool_result", {"result": payload}))
    text = out.getvalue()
    assert '"items"' in text
    assert "/show 1" in text
    assert r.folded(1) is not None
    assert '"items"' in r.folded(1) and "9" in r.folded(1)  # full, un-folded JSON


def test_run_frame_renderer_progress_percent_non_tty_plain_line():
    out = io.StringIO()
    opts = RenderOptions(color=False, emoji=False)
    r = RunFrameRenderer(opts, err=out)
    result = RunResult()
    r.consume({"event": "progress", "stage": "loading", "percent": 50}, result)
    text = out.getvalue()
    assert "loading" in text and "50" in text
    assert r._progress is None  # non-TTY path never creates a Live bar
    assert result.raw_payloads == [{"event": "progress", "stage": "loading", "percent": 50}]


def test_run_frame_renderer_progress_percent_tty_updates_live_bar():
    # A Live/Progress bar renders through Rich's own terminal-control timing,
    # which is not reliably scrapeable from a non-real-TTY sink; assert
    # against the Progress task state instead (the same pattern Rich's own
    # test suite uses), not the raw captured text.
    out = io.StringIO()
    opts = RenderOptions(color=True, emoji=True)
    r = RunFrameRenderer(opts, err=out)
    result = RunResult()
    r.consume({"event": "progress", "stage": "loading", "percent": 50}, result)
    assert r._progress is not None
    task = r._progress.tasks[0]
    assert task.completed == 50
    assert task.description == "loading"
    r._progress.stop()


def test_run_frame_renderer_status_and_result_and_error():
    out = io.StringIO()
    opts = RenderOptions(color=False, emoji=False)
    r = RunFrameRenderer(opts, err=out)
    result = RunResult()
    r.consume({"event": "status", "state": "starting"}, result)
    assert "starting" in out.getvalue()

    r.consume({"event": "result", "output": {"ok": True}}, result)
    assert result.output == {"ok": True}
    assert result.ok is True

    r2 = RunFrameRenderer(opts, err=io.StringIO())
    result2 = RunResult()
    r2.consume({"event": "error", "code": "INFER_ERROR", "message": "bad input"}, result2)
    assert result2.error_code == "INFER_ERROR"
    assert result2.error_message == "bad input"
    assert result2.ok is False


def test_run_frame_renderer_progress_bar_stops_on_result():
    out = io.StringIO()
    opts = RenderOptions(color=True, emoji=True)
    r = RunFrameRenderer(opts, err=out)
    result = RunResult()
    r.consume({"event": "progress", "stage": "loading", "percent": 10}, result)
    assert r._progress is not None
    r.consume({"event": "result", "output": {"ok": True}}, result)
    assert r._progress is None


@pytest.mark.parametrize("color", [True, False])
@pytest.mark.parametrize("emoji", [True, False])
class TestRenderRunOutput:
    def test_none_output(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        render_run_output(None, None, opts, out=out)
        assert "无输出" in out.getvalue()

    def test_audio(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        render_run_output({"audio_path": "/tmp/a.wav"}, "audio", opts, out=out)
        assert "/tmp/a.wav" in out.getvalue()

    def test_image(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        render_run_output({"image_path": "/tmp/b.png"}, "image", opts, out=out)
        assert "/tmp/b.png" in out.getvalue()

    def test_ocr(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        output = {
            "lines": [{"text": "hello", "confidence": 0.987}],
            "language": "en",
        }
        render_run_output(output, None, opts, out=out)
        text = out.getvalue()
        assert "hello" in text
        assert "98.7%" in text

    def test_asr(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        output = {
            "fullText": "full transcript",
            "segments": [{"start": 1.5, "text": "seg one"}],
        }
        render_run_output(output, None, opts, out=out)
        text = out.getvalue()
        assert "full transcript" in text
        assert "seg one" in text

    def test_predictions(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        output = {"predictions": [{"label": "cat", "probability": 0.5}]}
        render_run_output(output, None, opts, out=out)
        text = out.getvalue()
        assert "cat" in text
        assert "50.0%" in text

    def test_detections(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        output = {"detections": [{"label": "dog", "bbox": [1, 2, 3, 4]}]}
        render_run_output(output, None, opts, out=out)
        text = out.getvalue()
        assert "dog" in text
        assert "1" in text and "4" in text

    def test_fallback_json(self, color, emoji):
        out = io.StringIO()
        opts = RenderOptions(color=color, emoji=emoji)
        render_run_output({"weird": {"shape": True}}, None, opts, out=out)
        text = out.getvalue()
        assert "weird" in text
        assert "shape" in text


@pytest.mark.parametrize(
    ("code", "message", "expected_prefix"),
    [
        (None, None, "推理失败"),
        ("WEIGHTS_NOT_INSTALLED", None, "模型权重未安装"),
        ("OUT_OF_MEMORY", "cuda oom", "内存不足"),
        ("UNKNOWN_CODE", None, "UNKNOWN_CODE"),
        ("UNKNOWN_CODE", "extra detail", "UNKNOWN_CODE: extra detail"),
    ],
)
def test_friendly_error(code, message, expected_prefix):
    assert friendly_error(code, message).startswith(expected_prefix)
