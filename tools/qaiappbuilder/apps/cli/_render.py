# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``apps.cli._render`` — terminal renderers for streaming CLI sessions.

Shared by the Model Builder session (``qai build``, consumes
``StreamFrame``) and the App Builder inference surface (``qai app``,
consumes ``RunFrame`` payloads). All rendering is pure formatting glue:
no business logic, no use-case calls — frames in, terminal text out.

Design (cli-interactive-design.md §3.3 / §4.2 / §5)
---------------------------------------------------
* TTY vs non-TTY: when stdout is not a TTY (a pipe / file / CI), colour
  + emoji + box-drawing are suppressed so ``qai app ... | jq`` and log
  capture stay clean. ``rich`` already does most of this detection; we
  layer an explicit ``no_color`` switch on top for determinism in tests.
* StreamFrame (13 variants) → human-readable Agent transcript lines.
* RunFrame payloads (``status`` / ``progress`` / ``result`` / ``error``)
  → progress to *stderr*, structured result to *stdout* per outputSchema.

The renderers write through injected text sinks (default
``sys.stdout`` / ``sys.stderr``) so tests can capture output without
monkeypatching globals.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

__all__ = [
    "RenderOptions",
    "StreamFrameRenderer",
    "RunFrameRenderer",
    "render_run_output",
    "friendly_error",
]


# ---------------------------------------------------------------------------
# Terminal capability + options
# ---------------------------------------------------------------------------


def _stream_is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 — closed / fake stream
        return False


@dataclass(slots=True)
class RenderOptions:
    """Knobs controlling colour / emoji / fold behaviour.

    ``from_streams`` auto-detects TTY-ness so a piped invocation degrades
    to plain ASCII. Tests construct an explicit instance for determinism.
    """

    color: bool = True
    emoji: bool = True
    #: Fold ``tool_result`` bodies longer than this many lines.
    fold_lines: int = 20

    @classmethod
    def from_streams(
        cls, out: TextIO, err: TextIO, *, fold_lines: int = 20
    ) -> "RenderOptions":
        tty = _stream_is_tty(out) and _stream_is_tty(err)
        return cls(color=tty, emoji=tty, fold_lines=fold_lines)


# ANSI colour helpers (only emitted when ``color`` is on).
_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
}


def _c(opts: RenderOptions, color: str, text: str) -> str:
    if not opts.color or color not in _ANSI:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def _sym(opts: RenderOptions, emoji: str, plain: str) -> str:
    return emoji if opts.emoji else plain


# ---------------------------------------------------------------------------
# StreamFrame renderer (Model Builder, qai build)
# ---------------------------------------------------------------------------


class StreamFrameRenderer:
    """Render ``StreamFrame`` payloads to a chat-style terminal transcript.

    StreamFrame is consumed duck-typed (``frame.frame_type.value`` +
    ``frame.payload``) so this module never imports ``qai.chat`` — keeping
    the renderer free of a context dependency (and import-linter clean).
    The 13 frame-type strings are the contract
    (``stream_frame.py:StreamFrameType``).
    """

    __slots__ = ("_opts", "_out", "_err", "_mid_chunk")

    def __init__(
        self,
        opts: RenderOptions,
        *,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        self._opts = opts
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr
        #: True while a stream of ``chunk`` frames is being printed without
        #: a trailing newline, so the next non-chunk frame can break the line.
        self._mid_chunk = False

    def render(self, frame: Any) -> None:
        ftype = _frame_type_str(frame)
        payload = getattr(frame, "payload", {}) or {}
        handler = _STREAM_DISPATCH.get(ftype)
        if handler is None:
            # Unknown frame type: surface dimly so nothing is silently lost.
            self._break_chunk()
            self._line(_c(self._opts, "dim", f"· {ftype}: {payload}"))
            return
        handler(self, payload)

    # -- per-frame handlers -------------------------------------------------

    def _on_chunk(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        if not text:
            return
        if not self._mid_chunk:
            # Open an Agent block header on the first chunk of a turn.
            self._out.write(
                _c(self._opts, "magenta", _sym(self._opts, "🧞 ", "")) + ""
            )
        self._out.write(text)
        self._out.flush()
        self._mid_chunk = True

    def _on_tool_call(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        name = str(payload.get("tool_name", "tool"))
        args = payload.get("arguments", {})
        gear = _sym(self._opts, "⚙ ", "» ")
        summary = _summarize_tool_args(args)
        self._line(
            "  " + _c(self._opts, "cyan", f"{gear}{name}") + f" › {summary}"
        )

    def _on_tool_result(self, payload: dict[str, Any]) -> None:
        # Partial (streamed exec stdout) updates refresh in place; print the
        # delta inline without a new header.
        if payload.get("partial"):
            delta = str(payload.get("delta", ""))
            if delta:
                self._out.write(delta)
                self._out.flush()
                self._mid_chunk = True
            return
        self._break_chunk()
        result = payload.get("result", "")
        text = result if isinstance(result, str) else json.dumps(
            result, ensure_ascii=False
        )
        lines = text.splitlines() or [""]
        ok = _sym(self._opts, "✓", "[ok]")
        if len(lines) > self._opts.fold_lines:
            shown = "\n".join("     " + ln for ln in lines[: self._opts.fold_lines])
            more = len(lines) - self._opts.fold_lines
            self._line("     " + _c(self._opts, "green", ok))
            self._line(shown)
            self._line(
                _c(self._opts, "dim", f"     … {more} 行已折叠（/show 展开）")
            )
        else:
            body = "\n".join("     " + ln for ln in lines)
            self._line("     " + _c(self._opts, "green", ok))
            if body.strip():
                self._line(body)

    def _on_tool_mode_changed(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        mode = str(payload.get("mode", ""))
        self._line(
            _c(self._opts, "dim", f"· 已进入 {mode} 模式")
        )

    def _on_turn_warning(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        count = payload.get("turn_count")
        msg = payload.get("message") or f"已 {count} 轮工具调用，接近上限"
        self._line(_c(self._opts, "yellow", f"⚠ {msg}"))

    def _on_error(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        code = str(payload.get("code", "ERROR"))
        message = str(payload.get("message", ""))
        cross = _sym(self._opts, "✗", "[x]")
        self._line(_c(self._opts, "red", f"{cross} {code}: {message}"))

    def _on_end(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        usage = payload.get("usage") or {}
        bits: list[str] = []
        total = usage.get("total_tokens") or usage.get("total")
        if total:
            bits.append(f"{total} tok")
        if bits:
            self._line(_c(self._opts, "dim", "· " + " · ".join(bits)))

    def _on_subagent_start(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        idx = payload.get("index")
        total = payload.get("total")
        preview = str(payload.get("prompt_preview", ""))
        self._line(
            _c(self._opts, "dim", f"  └ 子任务 {idx}/{total}: {preview}")
        )

    def _on_subagent_output(self, payload: dict[str, Any]) -> None:
        content = str(payload.get("content", ""))
        if content:
            self._line(_c(self._opts, "dim", "    " + content))

    def _on_subagent_tool(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("tool_name", "tool"))
        self._line(_c(self._opts, "dim", f"    ⚙ {name}"))

    def _on_subagent_done(self, payload: dict[str, Any]) -> None:
        idx = payload.get("index")
        self._line(_c(self._opts, "dim", f"  └ 子任务 {idx} 完成"))

    def _on_subagent_error(self, payload: dict[str, Any]) -> None:
        idx = payload.get("index")
        msg = str(payload.get("message", ""))
        self._line(_c(self._opts, "red", f"  └ 子任务 {idx} 失败: {msg}"))

    def _on_agent_summary(self, payload: dict[str, Any]) -> None:
        n = payload.get("total_agents")
        self._line(_c(self._opts, "dim", f"· 共 {n} 个子 Agent"))

    # -- low-level ----------------------------------------------------------

    def _break_chunk(self) -> None:
        if self._mid_chunk:
            self._out.write("\n")
            self._out.flush()
            self._mid_chunk = False

    def _line(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()


_STREAM_DISPATCH: dict[str, Any] = {
    "chunk": StreamFrameRenderer._on_chunk,
    "tool_call": StreamFrameRenderer._on_tool_call,
    "tool_result": StreamFrameRenderer._on_tool_result,
    "tool_mode_changed": StreamFrameRenderer._on_tool_mode_changed,
    "turn_warning": StreamFrameRenderer._on_turn_warning,
    "error": StreamFrameRenderer._on_error,
    "end": StreamFrameRenderer._on_end,
    "subagent_start": StreamFrameRenderer._on_subagent_start,
    "subagent_output": StreamFrameRenderer._on_subagent_output,
    "subagent_tool": StreamFrameRenderer._on_subagent_tool,
    "subagent_done": StreamFrameRenderer._on_subagent_done,
    "subagent_error": StreamFrameRenderer._on_subagent_error,
    "agent_summary": StreamFrameRenderer._on_agent_summary,
}


def _frame_type_str(frame: Any) -> str:
    ft = getattr(frame, "frame_type", None)
    if ft is None:
        return ""
    return getattr(ft, "value", str(ft))


def _summarize_tool_args(args: Any, *, limit: int = 120) -> str:
    if isinstance(args, dict):
        # exec tool: show the command; otherwise compact JSON.
        cmd = args.get("command") or args.get("shell_command")
        text = str(cmd) if cmd else json.dumps(args, ensure_ascii=False)
    else:
        text = str(args)
    text = text.replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# RunFrame renderer (App Builder, qai app)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunResult:
    """Outcome of consuming a ``RunAppUseCase`` stream.

    ``output`` is the final result dict (if any); ``error_code`` /
    ``error_message`` are set when an error frame was seen.
    """

    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_payloads: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_code is None


class RunFrameRenderer:
    """Consume RunFrame payloads: progress→stderr, capture result/error.

    RunFrame payloads carry an ``event`` *or* ``type`` discriminator
    (``run_app.py`` checks ``event``, the runner subprocess emits
    ``type``) — both are tolerated. Progress goes to stderr so ``--json``
    stdout stays clean for piping.
    """

    __slots__ = ("_opts", "_err")

    def __init__(
        self, opts: RenderOptions, *, err: TextIO | None = None
    ) -> None:
        self._opts = opts
        self._err = err if err is not None else sys.stderr

    def consume(self, payload: dict[str, Any], result: RunResult) -> None:
        """Process one RunFrame payload, mutating ``result`` in place."""
        result.raw_payloads.append(payload)
        kind = _payload_kind(payload)
        if kind == "status":
            state = payload.get("state") or payload.get("status") or ""
            self._progress(f"… {state}")
        elif kind == "progress":
            pct = payload.get("percent")
            stage = payload.get("stage") or payload.get("phase") or ""
            if pct is not None:
                self._progress(f"… {stage} {pct}%")
            elif stage:
                self._progress(f"… {stage}")
        elif kind == "result":
            out = payload.get("output")
            if isinstance(out, dict):
                result.output = out
            else:
                # Some runners put result fields at the top level.
                result.output = {
                    k: v
                    for k, v in payload.items()
                    if k not in ("event", "type")
                }
        elif kind == "error":
            result.error_code = str(payload.get("code", "INFER_ERROR"))
            result.error_message = str(payload.get("message", ""))
        elif kind == "done":
            pass  # terminal marker; nothing to render

    def _progress(self, text: str) -> None:
        self._err.write(text + "\n")
        self._err.flush()


def _payload_kind(payload: dict[str, Any]) -> str:
    """Return the normalized discriminator for a RunFrame payload."""
    raw = payload.get("event") or payload.get("type") or ""
    return str(raw).lower()


# ---------------------------------------------------------------------------
# Output rendering by outputSchema.kind (qai app)
# ---------------------------------------------------------------------------


def render_run_output(
    output: dict[str, Any] | None,
    output_kind: str | None,
    opts: RenderOptions,
    *,
    out: TextIO | None = None,
) -> None:
    """Human-readable render of a run ``output`` dict by ``outputSchema.kind``.

    Falls back to pretty JSON for shapes we don't have a bespoke layout
    for. ``--json`` callers bypass this and dump ``output`` raw.
    """
    sink = out if out is not None else sys.stdout
    if output is None:
        sink.write(_c(opts, "dim", "(无输出)\n"))
        sink.flush()
        return

    kind = (output_kind or "").lower()
    if kind == "audio" or "audio_path" in output:
        path = output.get("audio_path") or output.get("path")
        sink.write(_sym(opts, "🔊 ", "") + f"已生成音频: {path}\n")
    elif kind == "image" or "image_path" in output:
        path = output.get("image_path") or output.get("path")
        sink.write(_sym(opts, "🖼 ", "") + f"已生成图片: {path}\n")
    elif "lines" in output and isinstance(output["lines"], list):
        _render_ocr(output, opts, sink)
    elif "segments" in output and isinstance(output["segments"], list):
        _render_asr(output, opts, sink)
    elif "predictions" in output and isinstance(output["predictions"], list):
        _render_predictions(output, opts, sink)
    elif "detections" in output and isinstance(output["detections"], list):
        _render_detections(output, opts, sink)
    else:
        sink.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    sink.flush()


def _render_ocr(output: dict[str, Any], opts: RenderOptions, sink: TextIO) -> None:
    lines = output["lines"]
    lang = output.get("language", "")
    sink.write(
        _sym(opts, "📄 ", "")
        + f"OCR 结果 (检测到 {len(lines)} 行, 语言: {lang})\n"
    )
    for item in lines:
        if isinstance(item, dict):
            text = item.get("text", "")
            conf = item.get("confidence") or item.get("score")
            conf_s = f"  {conf:.1%}" if isinstance(conf, (int, float)) else ""
            sink.write(f"  {text}{conf_s}\n")
        else:
            sink.write(f"  {item}\n")


def _render_asr(output: dict[str, Any], opts: RenderOptions, sink: TextIO) -> None:
    full = output.get("fullText") or output.get("full_text") or ""
    if full:
        sink.write(_sym(opts, "📝 ", "") + f"{full}\n")
    for seg in output["segments"]:
        if isinstance(seg, dict):
            start = seg.get("start")
            text = seg.get("text", "")
            ts = f"[{start:.1f}s] " if isinstance(start, (int, float)) else ""
            sink.write(f"  {ts}{text}\n")


def _render_predictions(
    output: dict[str, Any], opts: RenderOptions, sink: TextIO
) -> None:
    sink.write(_sym(opts, "🏷 ", "") + "分类结果 (top-k)\n")
    for pred in output["predictions"]:
        if isinstance(pred, dict):
            label = pred.get("label", "")
            prob = pred.get("probability") or pred.get("score")
            prob_s = f"  {prob:.1%}" if isinstance(prob, (int, float)) else ""
            sink.write(f"  {label}{prob_s}\n")


def _render_detections(
    output: dict[str, Any], opts: RenderOptions, sink: TextIO
) -> None:
    dets = output["detections"]
    sink.write(_sym(opts, "🎯 ", "") + f"检测结果 ({len(dets)} 个目标)\n")
    for det in dets:
        if isinstance(det, dict):
            label = det.get("label", "")
            bbox = det.get("bbox") or det.get("box")
            sink.write(f"  {label}  {bbox}\n")


# ---------------------------------------------------------------------------
# Error code → friendly message (qai app + qai build, design §5)
# ---------------------------------------------------------------------------

_ERROR_HINTS: dict[str, str] = {
    "WEIGHTS_NOT_INSTALLED": (
        "模型权重未安装。运行: qai pack deps-install <pack>"
    ),
    "INVALID_INPUT": "输入不符合要求。请检查格式 / 大小 / 必填项",
    "OUT_OF_MEMORY": "内存不足。尝试更小的输入或 fp16 变体",
    "INFER_ERROR": "推理失败",
    "TIMEOUT": "推理超时。模型加载或推理耗时过长",
    "INVALID_VARIANT_ID": "无效的变体 id。用 --help 查看可用变体",
}


def friendly_error(code: str | None, message: str | None) -> str:
    """Map a structured error ``code`` to a friendly Chinese hint."""
    code = code or "INFER_ERROR"
    hint = _ERROR_HINTS.get(code)
    base = hint if hint else code
    if message and message not in (base, ""):
        return f"{base}: {message}"
    return base
