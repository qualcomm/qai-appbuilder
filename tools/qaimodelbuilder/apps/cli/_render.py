"""``apps.cli._render`` — terminal renderers for streaming CLI sessions.

Shared by the Model Builder session (``qai build``, consumes
``StreamFrame``) and the App Builder inference surface (``qai app``,
consumes ``RunFrame`` payloads). All rendering is pure formatting glue:
no business logic, no use-case calls — frames in, terminal text out.

Design (cli-interactive-design.md §3.3 / §4.2 / §5; cli-render-redesign
plan, Technical Design decision 1-8)
---------------------------------------------------
* TTY vs non-TTY: when stdout is not a TTY (a pipe / file / CI), colour
  + emoji + the live progress bar are suppressed so ``qai app ... | jq``
  and log capture stay clean. ``RenderOptions.color`` doubles as the
  TTY/"rich terminal" switch feeding both the ``rich.console.Console``
  construction and :class:`RunFrameRenderer`'s progress-bar-vs-plain-line
  choice — there is no separate flag to keep in sync.
* StreamFrame (13 variants) → human-readable Agent transcript lines,
  rendered through a themed :class:`rich.console.Console` (see
  ``_render_theme.py``). Long ``tool_result`` bodies are folded and
  registered under an incrementing index so ``/show <n>`` can retrieve
  the full text later (decision 6).
* RunFrame payloads (``status`` / ``progress`` / ``result`` / ``error``)
  → progress to *stderr* (a live bar on a real terminal, plain lines
  otherwise), structured result to *stdout* per outputSchema.

The renderers write through injected text sinks (default
``sys.stdout`` / ``sys.stderr``) so tests can capture output without
monkeypatching globals. Model/tool text is always composed as
``rich.text.Text`` (never interpolated into markup strings) so stray
``[`` / ``]`` characters in LLM output or tool arguments can never be
misparsed as Rich markup.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

from rich.console import Console
from rich.padding import Padding
from rich.progress import Progress
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from apps.cli._render_theme import build_console, icon

__all__ = [
    "RenderOptions",
    "TranscriptSink",
    "ConsoleSink",
    "StreamFrameRenderer",
    "ProgressSink",
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


def _console(opts: RenderOptions, stream: TextIO) -> Console:
    """Build the shared themed console bound to *stream* for *opts*.

    Thin wrapper over :func:`apps.cli._render_theme.build_console` — the
    single construction site every CLI terminal surface shares (see that
    function's docstring for the ``no_color``/``legacy_windows`` rationale).
    """
    return build_console(color=opts.color, emoji=opts.emoji, stream=stream)


# ---------------------------------------------------------------------------
# StreamFrame renderer (Model Builder, qai build)
# ---------------------------------------------------------------------------


class TranscriptSink(Protocol):
    """Write target for :class:`StreamFrameRenderer` — a dumb sink, ignorant
    of frame types: raw incremental text, a "finalize the current line"
    signal, or one complete structured Rich renderable.
    """

    def write_chunk(self, text: str) -> None:
        """Append raw incremental text (no implied newline)."""

    def break_line(self) -> None:
        """Finalize the in-progress line (insert a line break)."""

    def print_block(self, renderable: Any) -> None:
        """Print one complete Rich renderable as a new block."""


class ConsoleSink:
    """Default :class:`TranscriptSink`: a plain ``TextIO`` + themed ``Console``.

    Reproduces exactly what :class:`StreamFrameRenderer` wrote directly
    before the sink indirection existed (see module docstring).
    """

    __slots__ = ("_out", "_console")

    def __init__(self, out: TextIO, console: Console) -> None:
        self._out = out
        self._console = console

    def write_chunk(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def break_line(self) -> None:
        self._out.write("\n")
        self._out.flush()

    def print_block(self, renderable: Any) -> None:
        self._console.print(renderable)


class StreamFrameRenderer:
    """Render ``StreamFrame`` payloads to a chat-style terminal transcript.

    StreamFrame is consumed duck-typed (``frame.frame_type.value`` +
    ``frame.payload``) so this module never imports ``qai.chat`` — keeping
    the renderer free of a context dependency (and import-linter clean).
    The 13 frame-type strings are the contract
    (``stream_frame.py:StreamFrameType``).

    All writes go through an injected :class:`TranscriptSink` (default a
    :class:`ConsoleSink` bound to ``out``/``err``, unchanged terminal
    behaviour) — e.g. the persistent Textual REPL shell (``_tui/app.py``)
    binds a widget-backed sink instead so the same turn-streaming logic
    lands in the chat transcript widget.
    """

    __slots__ = (
        "_opts",
        "_sink",
        "_mid_chunk",
        "_folded",
        "_fold_seq",
    )

    def __init__(
        self,
        opts: RenderOptions,
        *,
        out: TextIO | None = None,
        err: TextIO | None = None,
        sink: TranscriptSink | None = None,
    ) -> None:
        self._opts = opts
        if sink is None:
            out = out if out is not None else sys.stdout
            sink = ConsoleSink(out, _console(opts, out))
        self._sink = sink
        #: True while a stream of ``chunk`` frames is being printed without
        #: a trailing newline, so the next non-chunk frame can break the line.
        self._mid_chunk = False
        #: ``/show`` fold registry: index -> full (un-folded) text.
        self._folded: dict[int, str] = {}
        self._fold_seq = 0

    def render(self, frame: Any) -> None:
        ftype = _frame_type_str(frame)
        payload = getattr(frame, "payload", {}) or {}
        handler = _STREAM_DISPATCH.get(ftype)
        if handler is None:
            # Unknown frame type: surface dimly so nothing is silently lost.
            self._break_chunk()
            self._sink.print_block(Text(f"· {ftype}: {payload}", style="dim"))
            return
        handler(self, payload)

    def folded(self, index: int | None = None) -> str | None:
        """Return the full text registered for ``/show <index>``.

        ``index=None`` returns the most recently folded entry. Returns
        ``None`` when nothing has been folded yet, or *index* is unknown.
        """
        if index is None:
            index = self._fold_seq
        return self._folded.get(index)

    @property
    def last_fold_index(self) -> int:
        """Most recently assigned fold index (0 = nothing folded yet)."""
        return self._fold_seq

    # -- per-frame handlers -------------------------------------------------

    def _on_chunk(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        if not text:
            return
        if not self._mid_chunk:
            # Open an Agent block header on the first chunk of a turn.
            prefix = icon("agent", emoji=self._opts.emoji)
            if prefix:
                self._sink.write_chunk(prefix + " ")
        self._sink.write_chunk(text)
        self._mid_chunk = True

    def _on_tool_call(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        name = str(payload.get("tool_name", "tool"))
        args = payload.get("arguments", {})
        summary = _summarize_tool_args(args)
        gear = icon("tool", emoji=self._opts.emoji)
        line = Text("  ")
        line.append(f"{gear} {name}".strip(), style="tool")
        line.append(f" › {summary}", style="tool.arg")
        self._sink.print_block(line)

    def _on_tool_result(self, payload: dict[str, Any]) -> None:
        # Partial (streamed exec stdout) updates refresh in place; print the
        # delta inline without a new header.
        if payload.get("partial"):
            delta = str(payload.get("delta", ""))
            if delta:
                self._sink.write_chunk(delta)
                self._mid_chunk = True
            return
        self._break_chunk()
        result = payload.get("result", "")
        is_json = not isinstance(result, str)
        text = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, indent=2)
        )
        lines = text.splitlines() or [""]
        ok = icon("success", emoji=self._opts.emoji)
        self._sink.print_block(Text("     " + ok, style="success"))
        if len(lines) > self._opts.fold_lines:
            self._fold_seq += 1
            self._folded[self._fold_seq] = text
            shown = "\n".join(lines[: self._opts.fold_lines])
            self._print_result_body(shown, is_json)
            more = len(lines) - self._opts.fold_lines
            self._sink.print_block(
                Text(
                    f"     … {more} 行已折叠（/show {self._fold_seq} 展开）",
                    style="dim",
                )
            )
        else:
            if text.strip():
                self._print_result_body(text, is_json)

    def _print_result_body(self, text: str, is_json: bool) -> None:
        if is_json:
            self._sink.print_block(
                Padding(
                    Syntax(
                        text,
                        "json",
                        theme="ansi_dark",
                        word_wrap=True,
                        background_color="default",
                    ),
                    (0, 0, 0, 5),
                )
            )
        else:
            for ln in text.splitlines():
                self._sink.print_block(Text("     " + ln))

    def _on_tool_mode_changed(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        mode = str(payload.get("mode", ""))
        self._sink.print_block(Text(f"· 已进入 {mode} 模式", style="dim"))

    def _on_turn_warning(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        count = payload.get("turn_count")
        msg = payload.get("message") or f"已 {count} 轮工具调用，接近上限"
        warn = icon("warning", emoji=self._opts.emoji)
        self._sink.print_block(Text(f"{warn} {msg}".strip(), style="warning"))

    def _on_error(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        code = str(payload.get("code", "ERROR"))
        message = str(payload.get("message", ""))
        cross = icon("error", emoji=self._opts.emoji)
        self._sink.print_block(
            Text(f"{cross} {code}: {message}".strip(), style="error")
        )

    def _on_end(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        usage = payload.get("usage") or {}
        bits: list[str] = []
        total = usage.get("total_tokens") or usage.get("total")
        if total:
            bits.append(f"{total} tok")
        if bits:
            self._sink.print_block(Text("· " + " · ".join(bits), style="dim"))

    def _on_subagent_start(self, payload: dict[str, Any]) -> None:
        self._break_chunk()
        idx = payload.get("index")
        total = payload.get("total")
        preview = str(payload.get("prompt_preview", ""))
        self._sink.print_block(
            Text(f"  └ 子任务 {idx}/{total}: {preview}", style="dim")
        )

    def _on_subagent_output(self, payload: dict[str, Any]) -> None:
        content = str(payload.get("content", ""))
        if content:
            self._sink.print_block(Text("    " + content, style="dim"))

    def _on_subagent_tool(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("tool_name", "tool"))
        gear = icon("tool", emoji=self._opts.emoji)
        self._sink.print_block(Text(f"    {gear} {name}".strip(), style="dim"))

    def _on_subagent_done(self, payload: dict[str, Any]) -> None:
        idx = payload.get("index")
        self._sink.print_block(Text(f"  └ 子任务 {idx} 完成", style="dim"))

    def _on_subagent_error(self, payload: dict[str, Any]) -> None:
        idx = payload.get("index")
        msg = str(payload.get("message", ""))
        self._sink.print_block(Text(f"  └ 子任务 {idx} 失败: {msg}", style="error"))

    def _on_agent_summary(self, payload: dict[str, Any]) -> None:
        n = payload.get("total_agents")
        self._sink.print_block(Text(f"· 共 {n} 个子 Agent", style="dim"))

    # -- low-level ----------------------------------------------------------

    def _break_chunk(self) -> None:
        if self._mid_chunk:
            self._sink.break_line()
            self._mid_chunk = False


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


class ProgressSink(Protocol):
    """Write target for :class:`RunFrameRenderer`'s progress updates.

    Mirrors :class:`TranscriptSink`'s injection precedent: a dumb sink,
    ignorant of the RunFrame payload shape — just "show this status text",
    "update the bar to this stage/percent", "hide/reset the bar".
    """

    def set_status(self, text: str) -> None:
        """Show a status line with no percent yet (the ``status`` payload)."""

    def set_progress(self, stage: str, percent: float | None) -> None:
        """Update the bar (the ``progress`` payload)."""

    def stop(self) -> None:
        """Hide/reset the bar (run finished, errored, or force-stopped)."""


class RunFrameRenderer:
    """Consume RunFrame payloads: progress→stderr, capture result/error.

    RunFrame payloads carry an ``event`` *or* ``type`` discriminator
    (``run_app.py`` checks ``event``, the runner subprocess emits
    ``type``) — both are tolerated. Progress goes to stderr so ``--json``
    stdout stays clean for piping. ``opts.color`` (already TTY-derived by
    :meth:`RenderOptions.from_streams`) doubles as the "real terminal"
    switch: on a TTY the progress bar refreshes in place via
    ``rich.progress.Progress``; otherwise each update is a plain line
    (identical to the pre-Rich behaviour).

    ``progress_sink`` is an optional injection point (same precedent as
    :class:`StreamFrameRenderer`'s ``sink``) — e.g. the persistent Textual
    REPL shell binds a widget-backed sink instead, so progress lands in a
    panel rather than on stderr. Left unset, behaviour is byte-for-byte the
    pre-existing stderr/Rich-Progress path (kept inline below rather than
    factored into a default sink object, since existing tests assert
    directly on this class's ``_progress``/``_task_id`` internals).
    """

    __slots__ = (
        "_opts",
        "_err",
        "_console",
        "_progress_sink",
        "_progress",
        "_task_id",
        "_last_pct",
        "_last_stage",
    )

    def __init__(
        self,
        opts: RenderOptions,
        *,
        err: TextIO | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self._opts = opts
        self._err = err if err is not None else sys.stderr
        self._console = _console(opts, self._err)
        self._progress_sink = progress_sink
        self._progress: Progress | None = None
        self._task_id: Any = None
        self._last_pct: float = 0.0
        self._last_stage: str = ""

    def consume(self, payload: dict[str, Any], result: RunResult) -> None:
        """Process one RunFrame payload, mutating ``result`` in place."""
        result.raw_payloads.append(payload)
        kind = _payload_kind(payload)
        if kind == "status":
            state = payload.get("state") or payload.get("status") or ""
            text = f"… {state}"
            if self._progress_sink is not None:
                self._progress_sink.set_status(text)
            else:
                self._progress_line(text)
        elif kind == "progress":
            pct = payload.get("percent")
            stage = payload.get("stage") or payload.get("phase") or ""
            self._update_progress(stage, pct)
        elif kind == "result":
            self._stop_progress()
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
            self._stop_progress()
            result.error_code = str(payload.get("code", "INFER_ERROR"))
            result.error_message = str(payload.get("message", ""))
        elif kind == "done":
            self._stop_progress()  # terminal marker; just release the bar

    def stop(self) -> None:
        """Force-stop a live progress bar from outside (REPL exit cleanup).

        A no-op if no progress bar is currently running.
        """
        self._stop_progress()

    def _progress_line(self, text: str) -> None:
        self._console.print(Text(text, style="dim"))

    def _update_progress(self, stage: str, pct: Any) -> None:
        if isinstance(pct, (int, float)):
            self._last_pct = float(pct)
        if stage:
            self._last_stage = stage
        if self._progress_sink is not None:
            self._progress_sink.set_progress(
                self._last_stage,
                self._last_pct if isinstance(pct, (int, float)) else None,
            )
            return
        if self._opts.color:
            if self._progress is None:
                # ``auto_refresh=False`` + an explicit ``refresh()`` below
                # keeps rendering synchronous with ``update()`` instead of
                # relying on Rich's background refresh thread, so every
                # progress frame is reflected immediately (deterministic
                # for tests, and no lag for a real terminal either).
                self._progress = Progress(
                    console=self._console, transient=False, auto_refresh=False
                )
                self._progress.start()
                self._task_id = self._progress.add_task(
                    self._last_stage or "run", total=100
                )
            self._progress.update(
                self._task_id,
                completed=self._last_pct,
                description=self._last_stage or "run",
            )
            self._progress.refresh()
        elif isinstance(pct, (int, float)):
            self._progress_line(f"… {stage} {pct}%" if stage else f"… {pct}%")
        elif stage:
            self._progress_line(f"… {stage}")

    def _stop_progress(self) -> None:
        if self._progress_sink is not None:
            self._progress_sink.stop()
            return
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task_id = None


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

    Falls back to pretty JSON (syntax-highlighted) for shapes we don't
    have a bespoke layout for. ``--json`` callers bypass this and dump
    ``output`` raw.
    """
    sink = out if out is not None else sys.stdout
    console = _console(opts, sink)
    if output is None:
        console.print(Text("(无输出)", style="dim"))
        return

    kind = (output_kind or "").lower()
    if kind == "audio" or "audio_path" in output:
        path = output.get("audio_path") or output.get("path")
        prefix = icon("audio", emoji=opts.emoji)
        console.print(Text(f"{prefix} 已生成音频: {path}".strip()))
    elif kind == "image" or "image_path" in output:
        path = output.get("image_path") or output.get("path")
        prefix = icon("image", emoji=opts.emoji)
        console.print(Text(f"{prefix} 已生成图片: {path}".strip()))
    elif "lines" in output and isinstance(output["lines"], list):
        _render_ocr(output, opts, console)
    elif "segments" in output and isinstance(output["segments"], list):
        _render_asr(output, opts, console)
    elif "predictions" in output and isinstance(output["predictions"], list):
        _render_predictions(output, opts, console)
    elif "detections" in output and isinstance(output["detections"], list):
        _render_detections(output, opts, console)
    else:
        console.print(
            Syntax(
                json.dumps(output, ensure_ascii=False, indent=2),
                "json",
                theme="ansi_dark",
                word_wrap=True,
                background_color="default",
            )
        )


def _render_ocr(output: dict[str, Any], opts: RenderOptions, console: Console) -> None:
    lines = output["lines"]
    lang = output.get("language", "")
    prefix = icon("ocr", emoji=opts.emoji)
    console.print(
        Text(
            f"{prefix} OCR 结果 (检测到 {len(lines)} 行, 语言: {lang})".strip(),
            style="heading",
        )
    )
    table = Table(show_header=False, box=None, padding=(0, 1))
    for item in lines:
        if isinstance(item, dict):
            text = str(item.get("text", ""))
            conf = item.get("confidence") or item.get("score")
            conf_s = f"{conf:.1%}" if isinstance(conf, (int, float)) else ""
            table.add_row(text, conf_s)
        else:
            table.add_row(str(item))
    console.print(table)


def _render_asr(output: dict[str, Any], opts: RenderOptions, console: Console) -> None:
    full = output.get("fullText") or output.get("full_text") or ""
    prefix = icon("asr", emoji=opts.emoji)
    if full:
        console.print(Text(f"{prefix} {full}".strip()))
    table = Table(show_header=False, box=None, padding=(0, 1))
    for seg in output["segments"]:
        if isinstance(seg, dict):
            start = seg.get("start")
            text = str(seg.get("text", ""))
            ts = f"[{start:.1f}s]" if isinstance(start, (int, float)) else ""
            table.add_row(ts, text)
    console.print(table)


def _render_predictions(
    output: dict[str, Any], opts: RenderOptions, console: Console
) -> None:
    prefix = icon("predict", emoji=opts.emoji)
    console.print(Text(f"{prefix} 分类结果 (top-k)".strip(), style="heading"))
    table = Table(show_header=False, box=None, padding=(0, 1))
    for pred in output["predictions"]:
        if isinstance(pred, dict):
            label = str(pred.get("label", ""))
            prob = pred.get("probability") or pred.get("score")
            prob_s = f"{prob:.1%}" if isinstance(prob, (int, float)) else ""
            table.add_row(label, prob_s)
    console.print(table)


def _render_detections(
    output: dict[str, Any], opts: RenderOptions, console: Console
) -> None:
    dets = output["detections"]
    prefix = icon("detect", emoji=opts.emoji)
    console.print(
        Text(f"{prefix} 检测结果 ({len(dets)} 个目标)".strip(), style="heading")
    )
    table = Table(show_header=False, box=None, padding=(0, 1))
    for det in dets:
        if isinstance(det, dict):
            label = str(det.get("label", ""))
            bbox = det.get("bbox") or det.get("box")
            table.add_row(label, str(bbox))
    console.print(table)


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
