# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Streaming tool-exec subprocess adapter — real-time stdout/stderr tee.

Port of v1 ``backend/tools/_exec.py::_tool_exec_stream`` into the new
architecture's infrastructure layer.

Design:

* An **async generator** yielding :class:`ExecStreamFrame` dataclasses.
* Internally spawns ``asyncio.create_subprocess_shell`` with separate
  ``stdout=PIPE`` and ``stderr=PIPE``.
* Two concurrent reader tasks (stdout + stderr) feed lines into an
  :class:`asyncio.Queue`; the main loop drains the queue and yields
  frames as they arrive — the SSE presenter sees output in real time.
* A ``timeout`` parameter enforces a maximum wall-clock for the child;
  on expiry the child is killed and a ``terminated(timed_out=True)``
  frame is emitted.
* The generator simultaneously **collects** all output so callers can
  obtain the full text after iteration completes (via
  :attr:`ExecStreamResult.full_output`).

Layering rules (§3.5 import-linter):
  - infrastructure may import domain / application / asyncio / stdlib
  - must NOT import backend.* / features.* / apps.* / interfaces.*
"""

from __future__ import annotations

import asyncio
import codecs
import enum
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from qai.platform.process import (
    best_effort_tree_kill,
    no_window_creationflags,
    terminate_process_tree,
)
from qai.platform.text import strip_ansi_escapes

__all__ = [
    "ExecStreamFrame",
    "ExecStreamFrameKind",
    "ExecStreamResult",
    "stream_exec",
]

_log = logging.getLogger("qai.tools.infrastructure.tool_exec_stream")

# --- Output frame coalescing (throughput) ---
# Reading a child's stdout line-by-line and emitting ONE frame per line makes a
# command that prints tens of thousands of lines pay the full per-frame cost
# (queue round-trip + generator yields + the SSE network write downstream) for
# every single line — throughput collapses to a crawl even though only a few MB
# of output is produced. We therefore COALESCE consecutive same-stream lines
# into one frame: the accumulated text is flushed as a single STDOUT/STDERR
# frame once it reaches ``_COALESCE_FLUSH_BYTES`` OR ``_COALESCE_FLUSH_SECONDS``
# has elapsed since the last flush (whichever first), and always when the stream
# tag switches / on EOF / timeout / cap. This collapses tens of thousands of
# frames into a few hundred WITHOUT changing the frame shape (a frame's ``data``
# simply carries several lines instead of one) — the model-visible full_output,
# the byte cap accounting, and the SSE frame format are all unchanged.
_COALESCE_FLUSH_BYTES = 16 * 1024
_COALESCE_FLUSH_SECONDS = 0.1
# Bounded reader→drain queue: the readers block on a full queue (back-pressure)
# instead of growing an unbounded backlog when the consumer is slower than the
# child's output rate. Generous enough that normal output never blocks.
_READER_QUEUE_MAXSIZE = 10000

# Bytes pulled per ``StreamReader.read`` call in the pipe readers. This is a
# CHUNK size, not a line cap: the readers deliberately do NOT use
# ``readline()``, because ``asyncio.create_subprocess_shell`` builds its
# StreamReaders with the default 64 KiB buffer limit and ``readline()`` on a
# line longer than that raises ``ValueError: Separator is not found, and chunk
# exceed the limit``. That exception was caught by the reader's broad
# ``except Exception`` and merely debug-logged, so a single long line
# (``python -c "print('Y' * 400000)"``) silently produced ZERO output with
# ``exit_code=0`` and ``truncated=False`` — the model saw "(no output)" and had
# no way to tell an empty command apart from a swallowed 400 KB body. Reading
# in fixed chunks has no separator requirement, so output length is irrelevant
# to correctness; line boundaries are preserved because the chunks are simply
# concatenated downstream (``collected`` / ``stdout_collected`` are joined with
# no added separator).
#
# ``readline()`` is only ever safe on a stream whose LINE LENGTH we control. A
# child's stdout line length is chosen by the invoked command, so every
# ``readline()`` on a subprocess pipe is a latent limit bomb. The sibling path
# ``ai_coding/infrastructure/tools/handlers/search.py`` (the ``rg`` reader) hit
# the same limit and guards it with an explicit
# ``except (ValueError, asyncio.LimitOverrunError)`` — the precedent existed but
# had not been carried over here. Prefer chunked ``read`` for any new pipe
# reader rather than adding a third variant of that guard.
_READER_CHUNK_BYTES = 64 * 1024

# ---------------------------------------------------------------------------
# Shell selection note (S1)
# ---------------------------------------------------------------------------
# This engine deliberately does NOT do its own cmd-vs-PowerShell detection.
# Its sole production caller — the chat agentic loop in ``apps/api/di.py::
# _exec_stream`` — already runs the SINGLE-source-of-truth selector
# (``qai.ai_coding...handlers.exec._select_shell``, which carries the full
# detection + PowerShell alias-removal prelude + ``-ExecutionPolicy Bypass``)
# and passes the resulting argv straight through as ``argv=``.  Adding
# detection here too would double-select the shell.
#
# 2026-08-09 — ``argv`` is the production path and there is NO outer shell.
# Previously the caller re-flattened that argv into one string via a
# ``subprocess_join`` helper and we spawned it with
# ``create_subprocess_shell``; on Windows that re-parsed the string through
# ``cmd.exe /c``, which treats newlines as command separators.  A multi-line
# POSIX payload (``python - <<'PY' ... PY``) therefore lost its heredoc body:
# bash warned ``here-document delimited by end-of-file``, ``python -`` got an
# empty stdin and dropped into its REPL, and the call hung with exit code 0
# until the foreground-wait threshold auto-parked it.  Passing argv to
# ``create_subprocess_exec`` removes that second shell entirely.  The
# ``command``-only form (``argv=None``) is retained for callers that really
# do want OS-shell semantics.
#
# Historical note (2026-07-21): a sibling ``ai_coding`` streaming engine at
# ``src/qai/ai_coding/infrastructure/tools/tool_exec_stream.py`` used to serve
# a legacy ``POST /api/tool_execute_stream`` route with its own inline
# ``_select_shell`` call.  Both the route and that sibling engine were
# retired in the 2026-07-21 cleanup (zero V2 SPA consumers), so this file
# is now the sole streaming exec engine.


# ---------------------------------------------------------------------------
# Frame types
# ---------------------------------------------------------------------------

class ExecStreamFrameKind(enum.Enum):
    """Discriminator for frames yielded by :func:`stream_exec`."""

    STARTED = "started"
    STDOUT = "stdout"
    STDERR = "stderr"
    CAP_REACHED = "cap_reached"
    TERMINATED = "terminated"
    # M5 (chat path): the outer bridge signalled soft-steer + threshold —
    # the running child has been (or is about to be) adopted by the
    # background-process manager.  The reader loop breaks WITHOUT killing
    # the child; ownership of ``proc`` transfers to the caller.
    ADOPTED = "adopted"


@dataclass(frozen=True, slots=True)
class ExecStreamFrame:
    """One frame of streaming tool-exec output.

    Attributes:
        kind: Frame discriminator.
        data: Text payload (stdout/stderr line, or diagnostic message).
        meta: Optional dict with extra info (pid, exit_code, timed_out, etc.).
    """

    kind: ExecStreamFrameKind
    data: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecStreamResult:
    """Accumulator filled during iteration of :func:`stream_exec`.

    After the async iterator is exhausted, ``stdout`` and ``stderr`` hold
    the child's two output streams **separately** (each carrying only its
    own bytes, never merged), ``full_output`` holds the time-ordered
    concatenation of both (kept as a legacy compatibility surface — the
    ordering matches the byte stream as observed by the FIFO reader), and
    ``exit_code`` holds the child's return code.

    The stdout/stderr separation is architecturally required by callers
    that must post-process one stream without touching the other — the
    non-streaming exec handler already relies on this (only the stderr
    text is fed to ``_strip_powershell_clixml``); the streaming path must
    honour the same contract so a PowerShell ``#< CLIXML`` blob on stderr
    never touches the real user stdout printed alongside it (PSHOST /
    Write-Host output on the stdout channel).
    """

    full_output: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    #: Bytes decoded from the child's stdout pipe only.  Never contains
    #: stderr text.  Populated from the reader-task tag == "stdout".
    stdout: str = ""
    #: Bytes decoded from the child's stderr pipe only.  Never contains
    #: stdout text.  Populated from the reader-task tag == "stderr".
    stderr: str = ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Maximum bytes to stream before emitting a CAP_REACHED frame.
#: Matches v1 EXEC_STREAM_CAP_BYTES (50 KB).
STREAM_CAP_BYTES: int = 50 * 1024


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _probe_ask_pending(
    probe: "Callable[[int], bool]", pid: int
) -> bool:
    """Safely call the native-ASK-pending probe; any error → ``False``.

    Never raises — a probe glitch must never STALL a timeout kill (orphan
    safety); on uncertainty we let the deadline fire.
    """
    try:
        return bool(probe(pid))
    except Exception:  # noqa: BLE001 — probe failure must not stall the kill
        return False


async def stream_exec(
    command: str,
    *,
    # 2026-08-09: when provided, the child is spawned via
    # ``create_subprocess_exec(*argv)`` — no intermediate OS shell, so a
    # multi-line payload keeps its real newlines (heredoc bodies survive).
    # ``command`` is then only the human/diagnostic form of the same request.
    argv: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    cap_bytes: int = STREAM_CAP_BYTES,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    ask_flush_for_pid: "Callable[[int], Awaitable[list[str]]] | None" = None,
    # M5 (chat path): optional adopt-signal.  When set (from within the outer
    # bridge, typically after ``TurnSoftSteerCtx.soft_steer_event`` fires AND
    # elapsed >= threshold), the reader loop breaks and yields an ``ADOPTED``
    # frame WITHOUT killing the child; the caller then transfers ownership
    # of ``proc`` (captured via ``on_started``) to the bg-process manager.
    soft_steer_event: "asyncio.Event | None" = None,
    # M5 (chat path): optional callback invoked once with the spawned
    # subprocess handle so the outer bridge can capture a reference for a
    # later ``bg_manager.adopt(proc=..., ...)`` call.  Never awaits; a
    # raising callback is caught + logged.
    on_started: "Callable[[asyncio.subprocess.Process], None] | None" = None,
) -> tuple[AsyncIterator[ExecStreamFrame], ExecStreamResult]:
    """Spawn *command* (or *argv*) and stream its output in real time.

    Returns a 2-tuple of ``(frame_iterator, result_accumulator)``.

    The caller iterates ``frame_iterator`` to drive I/O; as a side-effect
    ``result_accumulator`` is populated with the aggregated output and
    exit status. After the iterator is exhausted the accumulator is
    complete.
    """
    result = ExecStreamResult()
    return _stream_impl(
        command,
        argv=argv,
        cwd=cwd, env=env, timeout=timeout,
        cap_bytes=cap_bytes, result=result,
        ask_pending_probe=ask_pending_probe,
        ask_flush_for_pid=ask_flush_for_pid,
        soft_steer_event=soft_steer_event,
        on_started=on_started,
    ), result


async def _stream_impl(
    command: str,
    *,
    argv: list[str] | None = None,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float | None,
    cap_bytes: int,
    result: ExecStreamResult,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    ask_flush_for_pid: "Callable[[int], Awaitable[list[str]]] | None" = None,
    soft_steer_event: "asyncio.Event | None" = None,
    on_started: "Callable[[asyncio.subprocess.Process], None] | None" = None,
) -> AsyncIterator[ExecStreamFrame]:
    """Core async generator implementing the streaming tee logic."""
    effective_timeout = timeout if (timeout and timeout > 0) else None

    if argv is not None:
        # No intermediate OS shell: the argv came from ``_select_shell`` and
        # already names the interpreter explicitly, so a multi-line payload
        # reaches that interpreter with its real newlines intact.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            # Windows: don't flash a console window for the child (no-op on
            # POSIX). stdout/stderr are still captured via the pipes above.
            creationflags=no_window_creationflags(),
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            # Windows: don't flash a console window for the child (no-op on
            # POSIX). stdout/stderr are still captured via the pipes above.
            creationflags=no_window_creationflags(),
        )

    # M5 (chat path): notify the outer bridge about the fresh proc IMMEDIATELY
    # so it can capture a reference for a later ``bg_manager.adopt(proc=...)``.
    # Never awaits; a raising callback is logged + swallowed.
    if on_started is not None:
        try:
            on_started(proc)
        except Exception as exc:  # noqa: BLE001 — observability must not break the tool
            _log.debug(
                "tools.exec_stream.on_started_failed",
                exc_info=exc,
            )

    # M5 (chat path): set to True the instant we yield ADOPTED — the ``finally``
    # then skips the tree-kill (the bg_manager owns lifecycle now).
    adopted = False

    # Reader tasks are created inside the try so the finally can always tear
    # them down; declared here so the finally can reference them even if the
    # consumer stops right after the STARTED frame (before they are spawned).
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    try:
        pid = proc.pid or 0
        yield ExecStreamFrame(
            kind=ExecStreamFrameKind.STARTED,
            meta={"pid": pid, "command": command},
        )

        # --- Concurrent readers feeding a shared queue ---
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue(
            maxsize=_READER_QUEUE_MAXSIZE
        )
        # sentinel: ("eof", None)

        async def _read_stream(
            stream: asyncio.StreamReader | None, tag: str
        ) -> None:
            """Read *stream* in fixed chunks and enqueue them with *tag*.

            Chunked (``read``) rather than line-oriented (``readline``) ON
            PURPOSE — see :data:`_READER_CHUNK_BYTES`. ``readline()`` raises
            ``ValueError`` once a single line exceeds the StreamReader's 64 KiB
            default limit, which this function's ``except Exception`` then
            swallowed into a debug log: the command reported ``exit_code=0``
            with EMPTY output and ``truncated=False``, so a 400 KB single-line
            body vanished with no diagnostic. ``read(n)`` has no separator
            requirement, so line length can never lose data.

            Chunk boundaries do not disturb the output: every consumer
            concatenates the pieces (``"".join(collected)``), and the coalescing
            frame writer already batches arbitrary text spans, so a chunk that
            splits mid-line reassembles byte-for-byte.
            """
            if stream is None:
                return
            # Chunk boundaries fall at arbitrary BYTE offsets, so a multi-byte
            # UTF-8 sequence (CJK, emoji) can straddle two reads. Decoding each
            # chunk independently would emit U+FFFD for the split halves and
            # corrupt the text. An incremental decoder carries the partial
            # sequence across ``read`` calls and only emits complete
            # characters; ``errors="replace"`` still guards genuinely invalid
            # bytes (binary output), matching the previous behaviour. Measured
            # before this decoder existed: 360 KB of CJK came back with 10
            # U+FFFD replacements, one per chunk boundary. A pure-ASCII test can
            # NEVER catch that, so any change to the chunking here must be
            # verified with content that straddles a boundary mid-character.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while True:
                    chunk = await stream.read(_READER_CHUNK_BYTES)
                    if not chunk:
                        # Flush any trailing incomplete sequence as U+FFFD.
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            await queue.put((tag, strip_ansi_escapes(tail)))
                        break
                    decoded = decoder.decode(chunk)
                    if not decoded:
                        # Chunk held only the prefix of a multi-byte character;
                        # wait for the rest rather than enqueueing an empty span.
                        continue
                    # Strip ANSI/VT100 escape sequences (V1 parity:
                    # backend/tools/_exec.py:1488 — strip BEFORE the text enters
                    # ``collected`` (model-visible full_output) / is yielded
                    # (UI tool-card frame.data) / counts toward the byte cap, so
                    # neither the LLM nor the tool card sees raw ``\x1b[...m``).
                    await queue.put((tag, strip_ansi_escapes(decoded)))
            except Exception as exc:
                # WARNING, never debug: downstream receives an EMPTY string, and
                # "genuinely read 0 bytes" is INDISTINGUISHABLE from "the read
                # raised" once the exception is swallowed. This log is therefore
                # the ONLY signal separating the two — dropping it (the pre-fix
                # ``debug``) is what let a 400 KB body vanish behind
                # ``exit_code=0`` / ``truncated=False`` with zero diagnostic.
                # Corollary for any future edit here: success-semantics fields
                # must be derived from the REAL read outcome; never keep
                # reporting success on a path where reading already failed.
                _log.warning(
                    "tools.exec_stream.reader_failed",
                    extra={"stream": tag, "error": str(exc)},
                    exc_info=exc,
                )
            finally:
                await queue.put((f"eof_{tag}", None))

        stdout_task = asyncio.create_task(
            _read_stream(proc.stdout, "stdout")
        )
        stderr_task = asyncio.create_task(
            _read_stream(proc.stderr, "stderr")
        )

        # --- Wall-clock timeout watchdog ---
        # The in-loop deadline check below only runs when the loop reaches its
        # top, which requires each ``yield`` to return — i.e. it depends on the
        # downstream consumer pulling the next frame. A command that floods
        # output (or a slow/stalled consumer) can park the generator at a
        # ``yield`` indefinitely, starving the in-loop check so the timeout
        # never fires. This INDEPENDENT watchdog task sleeps until the deadline
        # and then force-kills the process tree regardless of where the main
        # loop is parked; killing the child makes its pipes hit EOF, so the
        # readers finish and the loop drains + ends naturally. ``watchdog_fired``
        # tells the post-loop code to report the timeout.
        watchdog_fired = False

        async def _timeout_watchdog(budget: float) -> None:
            nonlocal watchdog_fired
            try:
                await asyncio.sleep(budget)
            except asyncio.CancelledError:
                return
            # 2026-07-08 — pause the timeout while the child is BLOCKED on a
            # native FileGuard authorization dialog (State-Truth-First: probe
            # the pending-permission authority via the injected callable). If a
            # native ASK is pending on this child tree, re-sleep another budget
            # instead of killing, so the user's decision time is not counted
            # against the timeout. Orphan-safe: without a pending ASK (or no
            # probe / probe error) the child is still force-killed on time.
            while (
                ask_pending_probe is not None
                and proc.pid is not None
                and _probe_ask_pending(ask_pending_probe, proc.pid)
            ):
                try:
                    await asyncio.sleep(budget)
                except asyncio.CancelledError:
                    return
            watchdog_fired = True
            # Force-kill the whole tree (the shell spawned the real command, so
            # killing only the direct child would orphan it).
            best_effort_tree_kill(proc)

        if effective_timeout:
            watchdog_task = asyncio.create_task(
                _timeout_watchdog(effective_timeout)
            )

        # --- Main drain loop ---
        collected: list[str] = []
        # Per-stream aggregators — filled alongside ``collected`` so the final
        # result carries stdout and stderr SEPARATELY.  Architectural contract:
        # a caller that must post-process one stream (e.g. strip
        # ``#< CLIXML`` from PowerShell stderr) must never touch the other
        # (PSHOST / Write-Host output on stdout).  The non-streaming
        # ``handlers.exec.tool_exec`` path already honours this (only
        # ``err_text`` is stripped).  ``full_output`` remains the byte-ordered
        # concatenation for legacy callers that read the merged form.
        stdout_collected: list[str] = []
        stderr_collected: list[str] = []
        total_bytes = 0
        cap_noticed = False
        timed_out = False
        eof_count = 0  # expect 2 EOFs (stdout + stderr)

        # Coalescing buffer: accumulate consecutive same-stream text and flush
        # it as ONE frame on a byte / time boundary (see the constants above).
        pending_tag: str | None = None  # "stdout" / "stderr" of the buffer
        pending_buf: list[str] = []
        pending_bytes = 0
        last_flush = asyncio.get_event_loop().time()

        deadline = (
            asyncio.get_event_loop().time() + effective_timeout
            if effective_timeout
            else None
        )

        while eof_count < 2:
            # M5 (chat path): the outer bridge asks us to hand ownership of
            # ``proc`` over.  Flush any buffered output, release our stdout /
            # stderr readers so the bg-process manager's new pumps get
            # exclusive read access, yield ``ADOPTED``, and break the loop.
            # The ``finally`` sees ``adopted=True`` and skips the tree-kill so
            # the child keeps running under the bg-process manager.
            if soft_steer_event is not None and soft_steer_event.is_set():
                if pending_buf:
                    _kind = (
                        ExecStreamFrameKind.STDOUT
                        if pending_tag == "stdout"
                        else ExecStreamFrameKind.STDERR
                    )
                    yield ExecStreamFrame(
                        kind=_kind, data="".join(pending_buf)
                    )
                    pending_buf = []
                    pending_bytes = 0
                    pending_tag = None
                # Cancel the reader tasks NOW so ``proc.stdout`` /
                # ``proc.stderr`` are unlocked before the caller's
                # ``bg_manager.adopt`` spawns its own pumps.  Reap synchronously
                # via ``await`` so the readers are provably done before ADOPTED
                # is yielded.
                for _task in (stdout_task, stderr_task):
                    if _task is not None and not _task.done():
                        _task.cancel()
                        try:
                            await _task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                adopted = True
                yield ExecStreamFrame(
                    kind=ExecStreamFrameKind.ADOPTED,
                    meta={"pid": pid, "command": command},
                )
                break
            # Compute remaining time budget for this iteration
            wait_timeout: float | None = None
            if deadline is not None:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    # 2026-07-08 — pause the timeout while the child is BLOCKED
                    # on a native FileGuard authorization dialog: if a native
                    # ASK is pending on this child tree, push the deadline out
                    # another slice instead of killing (the user's decision
                    # time is not counted against the timeout). Orphan-safe:
                    # without a pending ASK the kill below fires on time.
                    if (
                        ask_pending_probe is not None
                        and proc.pid is not None
                        and _probe_ask_pending(ask_pending_probe, proc.pid)
                    ):
                        deadline = (
                            asyncio.get_event_loop().time() + effective_timeout
                        )
                        wait_timeout = _COALESCE_FLUSH_SECONDS
                        remaining = effective_timeout
                    else:
                        # Timeout expired — flush any buffered output first, then
                        # tree kill the child (the shell spawned the real command
                        # process, so ``proc.kill()`` alone would orphan it).
                        if pending_buf:
                            kind = (
                                ExecStreamFrameKind.STDOUT
                                if pending_tag == "stdout"
                                else ExecStreamFrameKind.STDERR
                            )
                            yield ExecStreamFrame(
                                kind=kind, data="".join(pending_buf)
                            )
                            pending_buf = []
                            pending_bytes = 0
                            pending_tag = None
                        best_effort_tree_kill(proc)
                        timed_out = True
                        timeout_msg = (
                            f"\n[process killed: timeout after "
                            f"{effective_timeout}s]\n"
                        )
                        collected.append(timeout_msg)
                        # Mirror onto stdout_collected: the synthetic marker is
                        # yielded as a STDOUT frame, so the per-stream view must
                        # match (keeps full_output == stdout + stderr invariant
                        # so a caller composing "stdout\n[stderr]\n<stderr>"
                        # still surfaces the timeout marker).
                        #
                        # Routing choice: stdout keeps the marker on a plain
                        # trailing line (as historically), rather than inside
                        # the ``[stderr]`` block ``di.py::_compose_streaming_
                        # exec_output`` renders — matches what users saw before
                        # the split-stream refactor (judgement 2: no visible
                        # regression).
                        stdout_collected.append(timeout_msg)
                        yield ExecStreamFrame(
                            kind=ExecStreamFrameKind.STDOUT,
                            data=timeout_msg,
                        )
                        break
                else:
                    wait_timeout = min(remaining, _COALESCE_FLUSH_SECONDS)
            else:
                wait_timeout = _COALESCE_FLUSH_SECONDS

            try:
                tag, text = await asyncio.wait_for(
                    queue.get(), timeout=wait_timeout
                )
            except asyncio.TimeoutError:
                # No new data within the flush window — flush any buffered
                # output so a slow trickle still surfaces promptly, then
                # re-check the deadline.
                if pending_buf:
                    kind = (
                        ExecStreamFrameKind.STDOUT
                        if pending_tag == "stdout"
                        else ExecStreamFrameKind.STDERR
                    )
                    yield ExecStreamFrame(kind=kind, data="".join(pending_buf))
                    pending_buf = []
                    pending_bytes = 0
                    pending_tag = None
                    last_flush = asyncio.get_event_loop().time()
                continue

            if tag.startswith("eof_"):
                eof_count += 1
                continue

            # Real data line
            assert text is not None
            line_bytes_len = len(text.encode("utf-8"))
            collected.append(text)
            # Fill the per-stream aggregator so the caller sees stdout and
            # stderr separately after the iterator drains (see the dataclass
            # docstring for the architectural rationale).
            if tag == "stdout":
                stdout_collected.append(text)
            else:  # tag == "stderr"
                stderr_collected.append(text)
            total_bytes += line_bytes_len

            if total_bytes > cap_bytes and not cap_noticed:
                cap_noticed = True
                # Flush buffered output BEFORE the cap-reached marker so the
                # frame ordering stays faithful to the byte stream.
                if pending_buf:
                    kind = (
                        ExecStreamFrameKind.STDOUT
                        if pending_tag == "stdout"
                        else ExecStreamFrameKind.STDERR
                    )
                    yield ExecStreamFrame(kind=kind, data="".join(pending_buf))
                    pending_buf = []
                    pending_bytes = 0
                    pending_tag = None
                yield ExecStreamFrame(
                    kind=ExecStreamFrameKind.CAP_REACHED,
                    meta={"bytes": total_bytes},
                )

            # Coalesce: flush the buffer first if this line is from a DIFFERENT
            # stream than what is buffered (so stdout/stderr never interleave
            # within one frame), then append.
            if pending_tag is not None and tag != pending_tag and pending_buf:
                kind = (
                    ExecStreamFrameKind.STDOUT
                    if pending_tag == "stdout"
                    else ExecStreamFrameKind.STDERR
                )
                yield ExecStreamFrame(kind=kind, data="".join(pending_buf))
                pending_buf = []
                pending_bytes = 0
            pending_tag = tag
            pending_buf.append(text)
            pending_bytes += line_bytes_len

            # Flush on the byte / time boundary.
            now = asyncio.get_event_loop().time()
            if (
                pending_bytes >= _COALESCE_FLUSH_BYTES
                or now - last_flush >= _COALESCE_FLUSH_SECONDS
            ):
                kind = (
                    ExecStreamFrameKind.STDOUT
                    if pending_tag == "stdout"
                    else ExecStreamFrameKind.STDERR
                )
                yield ExecStreamFrame(kind=kind, data="".join(pending_buf))
                pending_buf = []
                pending_bytes = 0
                pending_tag = None
                last_flush = now

        # Flush any residual buffered output after the loop (normal EOF path).
        if pending_buf:
            kind = (
                ExecStreamFrameKind.STDOUT
                if pending_tag == "stdout"
                else ExecStreamFrameKind.STDERR
            )
            yield ExecStreamFrame(kind=kind, data="".join(pending_buf))

        # The watchdog force-killed the child on the wall-clock deadline (the
        # loop then ended via EOF rather than the in-loop deadline branch).
        # Report the timeout the same way the in-loop branch does.
        if watchdog_fired and not timed_out:
            timed_out = True
            timeout_msg = (
                f"\n[process killed: timeout after {effective_timeout}s]\n"
            )
            collected.append(timeout_msg)
            # Mirror onto stdout_collected (see the in-loop branch above).
            stdout_collected.append(timeout_msg)
            yield ExecStreamFrame(
                kind=ExecStreamFrameKind.STDOUT,
                data=timeout_msg,
            )
    finally:
        # Runs on normal completion, timeout break, AND when the consumer stops
        # iterating (SSE disconnect / user "Stop" → the async generator's
        # ``aclose()`` raises GeneratorExit / CancelledError at the ``yield``):
        # the child + any subtree it spawned must not be left running, and the
        # two reader tasks must be cancelled + reaped so they do not leak.
        #
        # IMPORTANT (async-generator teardown): during ``aclose()`` the event
        # loop only drives the finally for a single await step, so a multi-step
        # awaited kill (shielded ``terminate_process_tree``) may NOT run to
        # completion before aclose returns. We therefore fire the SYNCHRONOUS
        # tree kill FIRST — ``best_effort_tree_kill`` sends the kill signal
        # (proc.kill + Windows taskkill subtree) WITHOUT awaiting — so the child
        # is provably signalled even on the aclose path. The awaited reap below
        # is then a best-effort zombie collection.
        # M5 (chat path): if we adopted the child (see the ``ADOPTED`` yield in
        # the reader loop) the bg-process manager owns lifecycle — do NOT kill.
        if not adopted and proc.returncode is None:
            best_effort_tree_kill(proc)
        # Problem ② (chat-Stop path) — this finally runs on the user "Stop"
        # ``aclose()`` (GeneratorExit / CancelledError at a ``yield``). The
        # tree is now force-killed above, but any native FileGuard ASK the
        # child queued is still live in the registry and its dialog would keep
        # popping until the 10s subprocess-gone backstop. Flush those ASKs NOW
        # (resolve DENY + push an SSE close frame) so the dialog closes the
        # instant Stop takes effect. Scheduled as a detached shielded task so
        # the teardown is not delayed by the fast SSE publish and the flush
        # (which NEVER raises) still completes even as this generator finishes
        # closing. ``proc.pid is None`` (spawn failed) → nothing to flush.
        if ask_flush_for_pid is not None and proc.pid is not None:
            try:
                asyncio.ensure_future(
                    asyncio.shield(ask_flush_for_pid(proc.pid))
                )
            except Exception:  # noqa: BLE001 — flush scheduling must not break teardown
                _log.debug(
                    "tools.exec_stream.ask_flush_schedule_failed",
                    exc_info=True,
                )
        for task in (stdout_task, stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    _log.warning(
                        "tools.exec_stream.reader_task_cleanup_failed",
                        exc_info=True,
                    )
        # Cancel the wall-clock watchdog (no-op if it already fired / never armed).
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - best-effort cleanup
                _log.warning(
                    "tools.exec_stream.watchdog_cleanup_failed", exc_info=True
                )
        # Best-effort reap (zombie collection); shielded so a re-cancel cannot
        # interrupt it. Swallow a cancel HERE so we never mask the GeneratorExit
        # ``aclose()`` is delivering — the generator re-raises it after finally.
        # M5 (chat path): skip the async reap when adopted — the bg-process
        # manager's own exit-watcher will do the final ``proc.wait()`` and
        # publish the terminal ``BackgroundProcessUpdated``.
        if not adopted:
            try:
                await asyncio.shield(terminate_process_tree(proc))
            except asyncio.CancelledError:
                pass

    exit_code = proc.returncode or 0


    # Populate result accumulator
    result.full_output = "".join(collected)
    result.stdout = "".join(stdout_collected)
    result.stderr = "".join(stderr_collected)
    result.exit_code = exit_code
    result.timed_out = timed_out
    result.truncated = cap_noticed

    yield ExecStreamFrame(
        kind=ExecStreamFrameKind.TERMINATED,
        meta={
            "exit_code": exit_code,
            "timed_out": timed_out,
            "truncated": cap_noticed,
            "total_bytes": total_bytes,
        },
    )
