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

# ---------------------------------------------------------------------------
# Shell selection note (S1)
# ---------------------------------------------------------------------------
# This engine deliberately does NOT do its own cmd-vs-PowerShell detection.
# Its sole production caller — the chat agentic loop in ``apps/api/di.py::
# _exec_stream`` — already runs the SINGLE-source-of-truth selector
# (``qai.ai_coding...handlers.exec._select_shell``, which carries the full
# 6-rule detection + PowerShell alias-removal prelude + ``-ExecutionPolicy
# Bypass``) and rejoins the resulting argv via ``subprocess_join`` BEFORE
# handing the command string here.  ``create_subprocess_shell`` then runs it
# through the OS shell (cmd.exe on Windows).  Adding detection here too would
# double-select the shell.
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
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    cap_bytes: int = STREAM_CAP_BYTES,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    ask_flush_for_pid: "Callable[[int], Awaitable[list[str]]] | None" = None,
) -> tuple[AsyncIterator[ExecStreamFrame], ExecStreamResult]:
    """Spawn *command* in a shell and stream output in real time.

    Returns a 2-tuple of ``(frame_iterator, result_accumulator)``.

    The caller iterates ``frame_iterator`` to drive I/O; as a side-effect
    ``result_accumulator`` is populated with the aggregated output and
    exit status. After the iterator is exhausted the accumulator is
    complete.

    Args:
        command: Shell command string (passed to ``asyncio.create_subprocess_shell``).
        cwd: Working directory for the child process.
        env: Environment variable mapping; ``None`` inherits the current env.
        timeout: Maximum seconds to wait before killing the child. ``None`` or
            ``0`` means no timeout.
        cap_bytes: After this many bytes have been streamed, emit a
            :attr:`ExecStreamFrameKind.CAP_REACHED` frame (informational —
            output still keeps flowing).

    Returns:
        ``(async_iterator, result)`` — iterate the first; read the second
        after iteration for the aggregated text.
    """
    result = ExecStreamResult()
    return _stream_impl(command, cwd=cwd, env=env, timeout=timeout,
                        cap_bytes=cap_bytes, result=result,
                        ask_pending_probe=ask_pending_probe,
                        ask_flush_for_pid=ask_flush_for_pid), result


async def _stream_impl(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float | None,
    cap_bytes: int,
    result: ExecStreamResult,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    ask_flush_for_pid: "Callable[[int], Awaitable[list[str]]] | None" = None,
) -> AsyncIterator[ExecStreamFrame]:
    """Core async generator implementing the streaming tee logic."""
    effective_timeout = timeout if (timeout and timeout > 0) else None

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        # Windows: don't flash a console window for the child (no-op on POSIX).
        # stdout/stderr are still captured via the pipes above.
        creationflags=no_window_creationflags(),
    )

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
            """Read lines from *stream* and enqueue them with *tag*."""
            if stream is None:
                return
            try:
                while True:
                    line_bytes = await stream.readline()
                    if not line_bytes:
                        break
                    # Strip ANSI/VT100 escape sequences per line (V1 parity:
                    # backend/tools/_exec.py:1488 — strip BEFORE the line enters
                    # ``collected`` (model-visible full_output) / is yielded
                    # (UI tool-card frame.data) / counts toward the byte cap, so
                    # neither the LLM nor the tool card sees raw ``\x1b[...m``).
                    text = strip_ansi_escapes(
                        line_bytes.decode("utf-8", errors="replace")
                    )
                    await queue.put((tag, text))
            except Exception as exc:
                _log.debug("reader %s error: %s", tag, exc)
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
        if proc.returncode is None:
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
