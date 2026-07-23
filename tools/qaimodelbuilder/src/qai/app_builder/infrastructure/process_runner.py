# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Process-backed :class:`RunnerPort` (PR-045 + PR-302).

Wraps :class:`qai.platform.process.ports.ProcessRunnerPort` (introduced
by PR-041) so the App Builder context can launch a per-model Pack
``runner.py`` subprocess without owning the subprocess plumbing.

PR-302 promotes the runner from a "raw stdout passthrough" to a
**runner_protocol v3.1 decoder**: stdout chunks are buffered into
NDJSON lines, lines are decoded into typed
:class:`qai.app_builder.infrastructure.runner_protocol.RunnerEvent`
envelopes, and emitted as :class:`RunFrame` payloads with the canonical
v3.1 wire shape (``status / progress / metrics / log / result / done /
error``).

A ``CommandResolver`` callable (built by
:func:`qai.app_builder.infrastructure.command_resolver.build_command_resolver`
from a runtime registry) maps ``(Run, AppModelDefinition)`` to a
:class:`ProcessExecutionRequest`. PR-303's manifest reader will
populate the registry from ``manifest.json``; PR-302 ships an in-memory
registry so DI / tests can wire concrete runner specs today.

Frame mapping contract (PR-302 wire shape)
------------------------------------------

The runner emits :class:`RunFrame` payloads in this order:

1. ``{"event": "started", "pid": <int>}`` — once per run.
2. Zero-or-more :class:`RunnerEvent`-derived payloads carrying
   ``"event": <type>`` (where ``<type>`` is the v3.1 discriminator),
   plus the original Pack-side fields verbatim:

   * ``status``: ``{event, state, ...}``
   * ``progress``: ``{event, phase, pct, ...}``
   * ``metrics``: ``{event, latencyMs?, memoryMB?, device?, ...}``
   * ``result``: ``{event, output, ...}``
   * ``done``: ``{event}``
   * ``error``: ``{event, code, message, ...}``
   * ``log``: ``{event, stream, line}``  (Pack-emitted structured log)

3. ``log`` payloads with ``stream="stderr"`` for Pack stderr lines —
   buffered and interleaved with the stdout event stream so the user
   sees them in roughly the right order.
4. ``log`` payloads with ``stream="stdout"`` for non-JSON / malformed
   stdout lines (Pack ``print()`` output that bypasses ``emit()``).
5. ``{"event": "terminated", "exit_code": <int|None>, "timed_out": bool,
   "truncated": bool}`` — once per run.

Resolving the worker command
----------------------------

The :class:`ProcessBackedAppRunner` accepts a ``command_resolver``
callable. The default is ``_default_resolver`` which returns ``None``
(PR-045 backwards-compat behaviour: emit a single ``no_command`` frame
and complete cleanly). DI swaps in the real resolver when the registry
is populated.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from qai.platform.process.ports import (
    ProcessExecutionRequest,
    ProcessFrame,
    ProcessRunnerPort,
    ProcessStartedFrame,
    ProcessStderrFrame,
    ProcessStdoutFrame,
    ProcessTerminatedFrame,
)

from qai.app_builder.application.ports import ArtifactStorePort
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.run import Run, RunFrame

from .runner_protocol import (
    DoneEvent,
    ErrorEvent,
    LogEvent,
    MetricsEvent,
    NdjsonDecoder,
    ProgressEvent,
    ResultEvent,
    StatusEvent,
    StdoutLogEvent,
    UnknownRunnerEvent,
    decode_event,
)

__all__ = ["ProcessBackedAppRunner", "CommandResolver"]

_log = logging.getLogger("qai.app_builder.infrastructure.process_runner")

# ---------------------------------------------------------------------------
# NPU hardware lock — only one run may occupy the NPU at a time.
# Uses asyncio.Lock (FIFO fairness in CPython) consistent with v1 design.
# Module-level singleton: all ProcessBackedAppRunner instances share it.
# ---------------------------------------------------------------------------
_npu_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# NPU wait-queue position tracker (G4 — queue-position SSE, V1 parity).
#
# V1 (``backend/app_builder/runner.py:73-110, 160-201``) keeps a FIFO list of
# run ids contending for the NPU lock so it can emit ``status:queued`` frames
# carrying a ``queuePosition``. The list is **purely for display** — actual
# ordering is still enforced by ``asyncio.Lock``'s internal FIFO fairness;
# this tracker only mirrors who is in line so the concurrent poll loop can
# surface "排队第 N 位" to the user.
#
# Position semantics (V2 refinement, more informative than V1 yet faithful to
# its intent): the run **holding** the lock stays enqueued at slot 0 for the
# duration of its run; the first run waiting behind it is therefore at
# position 1 ("前面还有 1 个"), the next at 2, and so on. ``position`` is the
# truthful "how many runs are ahead of me" count. The holder is removed via
# ``release_npu_lock_and_dequeue`` when it finishes, advancing the next
# waiter to slot 0 (which then acquires and emits no queued frame).
#
# Both the sticky and one-shot runner paths serialise on the SAME
# ``_npu_lock`` (sticky_runner imports the helpers from here), so they MUST
# share the same wait queue — otherwise a run waiting behind a sticky run
# would report position 0. Hence the tracker is a module-level singleton
# alongside the lock. ``asyncio.Lock`` guards the list mutations so concurrent
# enqueue/dequeue from different runs never corrupt it (并发纪律).
# ---------------------------------------------------------------------------


class _NpuWaitQueue:
    """Display-only FIFO tracker of run ids contending for :data:`_npu_lock`.

    Not a scheduler: ``asyncio.Lock`` owns the real ordering. This records
    the contention order (including the current holder at slot 0) so
    :func:`acquire_npu_lock_with_queue_frames` can emit ``queued`` frames
    with a truthful "N runs ahead" position.
    """

    def __init__(self) -> None:
        self._waiting: list[str] = []
        self._guard = asyncio.Lock()

    async def enqueue(self, run_id: str) -> int:
        """Append ``run_id`` (idempotent); return its 0-based position."""
        async with self._guard:
            if run_id in self._waiting:
                return self._waiting.index(run_id)
            self._waiting.append(run_id)
            return len(self._waiting) - 1

    async def dequeue(self, run_id: str) -> None:
        """Remove ``run_id`` from the wait list (idempotent)."""
        async with self._guard:
            try:
                self._waiting.remove(run_id)
            except ValueError:
                pass

    def position(self, run_id: str) -> int:
        """Current 0-based position; ``-1`` when not waiting.

        Lock-free read (``list.index`` is atomic under CPython); a momentary
        stale value only over/under-emits one ``queued`` frame, never a
        correctness issue (V1 ``get_queue_position`` rationale).
        """
        try:
            return self._waiting.index(run_id)
        except ValueError:
            return -1


#: Shared wait-queue tracker for the NPU lock (one per process, mirrors the
#: single ``_npu_lock``). Imported by ``sticky_runner`` so both runner paths
#: contribute to the same position view.
_npu_queue = _NpuWaitQueue()

#: Poll interval (seconds) for re-emitting the queue position while waiting
#: for the NPU lock. Matches V1 ``runner.py:183`` (``asyncio.sleep(0.5)``).
_QUEUE_POLL_INTERVAL_S: float = 0.5


def _make_queued_frame(run_id: str, sequence: int, position: int) -> RunFrame:
    """Build a ``status:queued`` RunFrame carrying ``queuePosition``.

    Wire shape (V1 ``runner.py:164-169`` parity, contract §3.1 append-only):
    ``{"event":"status","state":"queued","queuePosition":<int>,"runId":<id>}``.
    The frontend ``frames.ts`` projects ``queuePosition`` onto
    ``run.queuePosition`` and ``DynamicOutput.vue`` renders "排队第 N 位".
    """
    return RunFrame(
        sequence=sequence,
        payload={
            "event": "status",
            "state": "queued",
            "queuePosition": position,
            "runId": run_id,
        },
    )


async def acquire_npu_lock_with_queue_frames(
    run_id: str, *, sequence_start: int
) -> AsyncIterator[RunFrame]:
    """Acquire :data:`_npu_lock`, yielding ``queued`` frames while waiting.

    Mirrors V1 ``runner.py:160-243``: enqueue the run, then race the lock
    acquisition against a 0.5s poll that emits a ``status:queued`` frame
    carrying the run's 0-based wait ``queuePosition`` whenever the run is
    genuinely waiting behind another run. When the lock is acquired the
    generator returns **holding the lock**; the run STAYS in the wait queue
    (it is the current holder) so a run that started behind it correctly
    reports a non-zero position. The caller MUST therefore release the lock
    via :func:`release_npu_lock_and_dequeue` (NOT a bare
    ``_npu_lock.release()``) so the holder is removed from the queue when it
    finishes — otherwise later runs would over-count.

    Position semantics: the run holding the lock occupies slot 0 of the
    wait queue; the first run waiting behind it is at position 1 ("前面还有
    1 个"), the next at 2, and so on. This is the truthful "how many runs
    are ahead of me" count the user sees as "排队第 N 位".

    Uncontended fast path: when the lock is free and the run lands at
    position 0 (no run ahead), **no** ``queued`` frame is emitted — the
    no-contention frame stream stays byte-for-byte identical to the pre-G4
    behaviour, and the frontend only renders "N ahead" for ``pos > 0``
    anyway. The ``queued`` frame appears only when a second concurrent run
    is genuinely waiting.

    Cancellation / generator-close safety: if the caller closes this
    generator before the lock is acquired (e.g. the run is cancelled while
    queued), the pending acquire is cancelled, the run is dequeued, and the
    lock is never leaked. If close races an already-acquired lock, the lock
    is released AND the run dequeued so neither is orphaned.

    The ``RunFrame.sequence`` continues from ``sequence_start`` so the
    caller's own frame numbering stays monotonic.
    """
    sequence = sequence_start
    initial_pos = await _npu_queue.enqueue(run_id)
    # Only surface the wait when there is real contention (pos > 0). A
    # position of 0 means we are up-next / the lock is free — emitting a
    # frame there would only add noise (the UI ignores pos==0) and would
    # perturb the no-contention frame stream every existing run depends on.
    last_pos = initial_pos
    if initial_pos > 0:
        yield _make_queued_frame(run_id, sequence, initial_pos)
        sequence += 1

    acquire_task: asyncio.Task[None] = asyncio.ensure_future(_npu_lock.acquire())
    got_lock = False
    try:
        while not got_lock:
            done, _pending = await asyncio.wait(
                {acquire_task}, timeout=_QUEUE_POLL_INTERVAL_S
            )
            if acquire_task in done:
                # Propagate any acquire error (shouldn't happen for a plain
                # asyncio.Lock, but keep it surfaced rather than swallowed).
                acquire_task.result()
                got_lock = True
                break
            # Still waiting — re-emit the position if it advanced (and is
            # still a real wait, pos > 0).
            pos = _npu_queue.position(run_id)
            if pos > 0 and pos != last_pos:
                yield _make_queued_frame(run_id, sequence, pos)
                sequence += 1
                last_pos = pos
        # Lock acquired. Intentionally DO NOT dequeue here: the holder stays
        # at slot 0 of the wait queue for the duration of its run so a run
        # behind it counts it as "1 ahead". The caller removes us via
        # release_npu_lock_and_dequeue() when the run finishes.
    finally:
        if not got_lock:
            # We were closed/cancelled before acquiring. Cancel the pending
            # acquire and make sure we don't leave a half-acquired lock.
            if not acquire_task.done():
                acquire_task.cancel()
                try:
                    await acquire_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            elif not acquire_task.cancelled():
                # Race: acquire completed just as we were closing — release
                # so the lock is not orphaned (V1 runner.py:258-263 parity).
                try:
                    acquire_task.result()
                    _npu_lock.release()
                except (RuntimeError, Exception):  # noqa: BLE001
                    pass
            await _npu_queue.dequeue(run_id)


async def release_npu_lock_and_dequeue(run_id: str) -> None:
    """Release :data:`_npu_lock` and remove ``run_id`` from the wait queue.

    The counterpart to :func:`acquire_npu_lock_with_queue_frames` — the
    holder stays enqueued (slot 0) during its run so waiters count it as
    "1 ahead"; this removes it on completion so the next waiter advances to
    position 0 and acquires. Both steps are best-effort / idempotent so a
    double-release or a not-held lock never raises.
    """
    await _npu_queue.dequeue(run_id)
    try:
        _npu_lock.release()
    except RuntimeError:
        # Not held / already released — defensive (the helper only returns
        # with the lock held, so this should not normally fire).
        pass


#: Per-run timeout (seconds). Prevents hung runner subprocesses from
#: blocking the NPU indefinitely. Matches v1 _MAX_TOTAL_TIMEOUT_S = 30 min.
RUN_TIMEOUT_S: float = 30 * 60

#: Max stderr lines retained for the synthesized PROCESS_EXITED ``detail``
#: (V1 ``stderr_tail`` cap, python_script.py uses a 500-line deque).
_STDERR_TAIL_MAXLEN: int = 500

#: Diagnostic hints for typical Windows native crash exit codes (V1
#: ``_CRASH_HINTS``, python_script.py:391-402). Keyed by the unsigned
#: 32-bit exit code so e.g. ``-1073741819`` (Python's signed view of
#: ``0xC0000005``) and ``3221225477`` both resolve.
_CRASH_HINTS: dict[int, str] = {
    0xC0000005: (
        "Access violation (segfault). Usually means a native DLL loaded by "
        "Python crashed. Try: (1) re-run the same command in a terminal to "
        "get the Windows fault dialog; (2) check qai_appbuilder / numpy / "
        "Pillow ABI compatibility with the configured ARM64 venv; (3) verify "
        "the .bin context binary matches the QAIRT runtime version."
    ),
    0xC0000409: "Stack buffer overrun (security cookie check failed).",
    0xC000013A: "Process terminated by Ctrl+C / Ctrl+Break.",
    0xC0000142: "DLL initialization failed. A dependent DLL could not load.",
    0x40010005: "Process exited via DBG_CONTROL_C.",
}


def _build_process_exited_error(
    *,
    run: Run,
    model: AppModelDefinition,
    request: ProcessExecutionRequest,
    exit_code: int,
    stderr_tail: "deque[str]",
) -> dict[str, Any]:
    """Assemble a V1-parity ``PROCESS_EXITED`` error payload.

    Mirrors ``backend/app_builder/runners/python_script.py:420-446``:
    the runner subprocess exited non-zero without a structured ``error``
    frame, so we surface the failure context (exit code + hex, stderr
    tail, spawn context, optional Windows crash hint) under ``detail`` —
    an *append-only* extension of the existing ``error`` frame shape
    (AGENTS §3.1:既有 ``event`` / ``code`` / ``message`` 不变, ``detail``
    尾部追加). The frontend ``frames.ts`` reads ``payload.detail`` and the
    diagnostics panel renders each present section, staying empty when a
    section is absent.
    """
    tail_lines = list(stderr_tail)
    tail_text = "\n".join(tail_lines).strip()
    base_msg = f"runner exited with code {exit_code} before emitting 'done'"

    # Windows exit codes are an unsigned 32-bit view; normalise for hex +
    # crash-hint lookup (V1 ``rc & 0xFFFFFFFF``).
    unsigned = exit_code & 0xFFFFFFFF
    exit_code_hex = f"0x{unsigned:08X}"
    crash_hint = _CRASH_HINTS.get(unsigned)

    if tail_text:
        # Keep the message tail bounded (V1 ~4 KB) so legacy consumers
        # reading only ``message`` still see the most recent output.
        msg_tail = tail_text[-4000:] if len(tail_text) > 4000 else tail_text
        base_msg = f"{base_msg}\n--- stderr (last lines) ---\n{msg_tail}"
    else:
        base_msg = (
            f"{base_msg}\n(stderr was empty; the child process likely failed "
            "to launch or was killed before writing anything.)"
        )

    detail: dict[str, Any] = {
        "exit_code": exit_code,
        "exit_code_hex": exit_code_hex,
        "stderr_lines": tail_lines,
        "stderr_truncated": len(stderr_tail) >= stderr_tail.maxlen,
        "spawn_context": {
            "argv": list(request.argv),
            "cwd": request.cwd,
            "model_id": str(model.id),
            "run_id": str(run.id),
        },
    }
    if crash_hint:
        detail["crash_hint"] = crash_hint

    return {
        "event": "error",
        "code": "PROCESS_EXITED",
        "message": base_msg,
        "detail": detail,
    }



CommandResolver = Callable[
    [Run, AppModelDefinition],
    ProcessExecutionRequest | None,
]


def _default_resolver(
    run: Run, model: AppModelDefinition
) -> ProcessExecutionRequest | None:
    """Backwards-compat fallback (PR-045): no command bound.

    Returns ``None`` so the runner emits a single ``no_command`` frame
    and exits cleanly. Production DI swaps this out with a real
    resolver built by
    :func:`qai.app_builder.infrastructure.command_resolver.build_command_resolver`
    once :class:`InMemoryRunnerCommandRegistry` is populated (PR-302
    test fixtures inject it directly; PR-303 wires the manifest
    reader).
    """
    del run, model  # accepted for signature-compatibility
    return None


class ProcessBackedAppRunner:
    """Runner that delegates execution to a :class:`ProcessRunnerPort`.

    The :meth:`execute` method matches :class:`RunnerPort.execute` —
    it is a regular method that *returns* an async iterator of
    :class:`RunFrame`; the iterator drives the underlying
    :class:`ProcessRunnerPort.run` call lazily.

    PR-302: stdout bytes are buffered through an :class:`NdjsonDecoder`
    and decoded into typed runner_protocol v3.1 envelopes. The runner
    yields one :class:`RunFrame` per logical event (started / typed
    runner event / stderr log / terminated).

    NPU serialisation (v1 parity):
        All runs are serialised behind :data:`_npu_lock` — only one
        runner subprocess occupies the NPU hardware at any time. Each
        run is also subject to :data:`RUN_TIMEOUT_S` (30 min); on expiry
        the iterator yields a synthetic ``error`` frame and returns.
    """

    __slots__ = ("_runner", "_resolver", "_timeout")

    def __init__(
        self,
        *,
        runner: ProcessRunnerPort,
        command_resolver: CommandResolver | None = None,
        timeout: float = RUN_TIMEOUT_S,
    ) -> None:
        self._runner = runner
        self._resolver = command_resolver or _default_resolver
        self._timeout = timeout

    def execute(
        self,
        run: Run,
        model: AppModelDefinition,
        *,
        artifact_store: ArtifactStorePort,
    ) -> AsyncIterator[RunFrame]:
        del artifact_store  # not needed today; kept on the Port signature
        return self._stream(run, model)

    async def _stream(
        self, run: Run, model: AppModelDefinition
    ) -> AsyncIterator[RunFrame]:
        try:
            request = self._resolver(run, model)
        except Exception as exc:  # noqa: BLE001
            # Phase D: surface UnsupportedBackendError (and any future
            # resolver errors) as a structured error frame rather than
            # letting the exception propagate and crash the run with an
            # opaque traceback.
            from qai.app_builder.infrastructure.command_resolver.registry import (
                UnsupportedBackendError,
            )
            if isinstance(exc, UnsupportedBackendError):
                yield RunFrame(
                    sequence=0,
                    payload={
                        "event": "error",
                        "code": "UNSUPPORTED_BACKEND",
                        "message": str(exc),
                        "model_id": str(model.id),
                        "run_id": str(run.id),
                    },
                )
                return
            raise
        if request is None:
            # No command bound — emit a single informational frame and
            # let the caller's RunAppUseCase complete the run cleanly.
            yield RunFrame(
                sequence=0,
                payload={
                    "event": "no_command",
                    "model_id": str(model.id),
                    "run_id": str(run.id),
                },
            )
            return

        # Acquire NPU lock — serialises all runner invocations. While
        # waiting, emit ``queued`` frames carrying the wait position so a
        # second concurrent run shows "排队第 N 位" instead of silently
        # hanging in preparing (G4 / V1 ``runner.py:160-243`` parity). The
        # helper returns holding the lock; we release it in ``finally``.
        _log.debug(
            "run %s model %s: waiting for NPU lock", run.id, model.id
        )
        run_id = str(run.id)
        queued_count = 0
        queue_gen = acquire_npu_lock_with_queue_frames(
            run_id, sequence_start=0
        )
        try:
            async for queued_frame in queue_gen:
                yield queued_frame
                queued_count += 1
        finally:
            await queue_gen.aclose()
        # Lock is held now (the helper only returns normally after acquiring).
        _log.debug(
            "run %s model %s: NPU lock acquired", run.id, model.id
        )
        try:
            async for frame in self._stream_with_timeout(
                request, run, model, sequence_start=queued_count
            ):
                yield frame
        finally:
            _log.debug(
                "run %s model %s: NPU lock released",
                run.id,
                model.id,
            )
            # Release the lock AND remove ourselves from the wait queue so
            # the next waiter advances to position 0 (G4). The holder stays
            # enqueued during the run so waiters count it as "1 ahead".
            await release_npu_lock_and_dequeue(run_id)

    async def _stream_with_timeout(
        self,
        request: ProcessExecutionRequest,
        run: Run,
        model: AppModelDefinition,
        *,
        sequence_start: int = 0,
    ) -> AsyncIterator[RunFrame]:
        """Drive the subprocess runner with a wall-clock timeout.

        If the timeout fires before the runner finishes, a synthetic
        ``error`` frame is emitted and the iterator terminates. The
        runner's internal process cleanup is left to the
        :class:`ProcessRunnerPort` implementation (which should kill on
        generator close / cancellation).

        Structured failure diagnostics (V1 parity, AGENTS.md 判据 2):
        if the runner subprocess exits with a non-zero code WITHOUT
        having emitted its own ``error`` / ``done`` event (a hard crash —
        segfault / DLL-init failure / bare ``sys.exit``), we synthesize a
        ``PROCESS_EXITED`` ``error`` frame whose ``detail`` mirrors V1
        ``backend/app_builder/runners/python_script.py:420-446`` —
        ``exit_code`` / ``exit_code_hex`` / ``stderr_lines`` /
        ``stderr_truncated`` / ``spawn_context`` / ``crash_hint``. Without
        it the user only saw the bare ``terminated`` frame and an empty
        diagnostics panel (the缺口 #2 this fixes). The accumulated stderr
        tail + a Windows native crash-code hint are V1's exact fields.

        ``sequence_start`` continues the RunFrame numbering after any
        ``queued`` frames the caller already emitted while waiting for the
        NPU lock (G4), keeping ``RunFrame.sequence`` monotonic.
        """
        sequence = sequence_start
        decoder = NdjsonDecoder()
        timed_out = False
        # V1 parity: keep the last N stderr lines so a crash without a
        # structured ``error`` event still surfaces the failure context.
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_MAXLEN)
        saw_terminal = False  # runner emitted its own ``error`` / ``done``
        terminated_exit_code: int | None = None
        saw_terminated = False

        def _track(payload: dict[str, Any]) -> None:
            """Observe a yielded payload to accumulate failure context."""
            nonlocal saw_terminal, terminated_exit_code, saw_terminated
            ev = payload.get("event")
            if ev in ("error", "done"):
                saw_terminal = True
            elif ev == "log" and payload.get("stream") == "stderr":
                raw = payload.get("data")
                if not isinstance(raw, str):
                    raw = payload.get("line")
                if isinstance(raw, str) and raw != "":
                    for ln in raw.splitlines():
                        if ln != "":
                            stderr_tail.append(ln)
            elif ev == "terminated":
                saw_terminated = True
                code = payload.get("exit_code")
                terminated_exit_code = code if isinstance(code, int) else None

        try:
            async with asyncio.timeout(self._timeout):
                async for frame in self._runner.run(request):
                    for payload in _frame_to_payloads(frame, decoder):
                        _track(payload)
                        yield RunFrame(sequence=sequence, payload=payload)
                        sequence += 1
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            _log.warning(
                "run %s model %s: timed out after %.0fs",
                run.id,
                model.id,
                self._timeout,
            )
            yield RunFrame(
                sequence=sequence,
                payload={
                    "event": "error",
                    "code": "TIMEOUT",
                    "message": (
                        f"Run timed out after {int(self._timeout)}s. "
                        f"The NPU runner subprocess was killed."
                    ),
                },
            )
            sequence += 1

        if not timed_out:
            # Stream ended normally; flush any buffered trailing line.
            for line in decoder.flush():
                payload = _decode_stdout_line(line)
                _track(payload)
                yield RunFrame(sequence=sequence, payload=payload)
                sequence += 1

            # V1 parity: the subprocess exited non-zero but never emitted
            # a structured ``error`` / ``done`` — synthesize PROCESS_EXITED
            # so the diagnostics panel renders stderr / exit code / spawn
            # context instead of staying empty.
            if (
                not saw_terminal
                and saw_terminated
                and terminated_exit_code is not None
                and terminated_exit_code != 0
            ):
                yield RunFrame(
                    sequence=sequence,
                    payload=_build_process_exited_error(
                        run=run,
                        model=model,
                        request=request,
                        exit_code=terminated_exit_code,
                        stderr_tail=stderr_tail,
                    ),
                )
                sequence += 1


# ---------------------------------------------------------------------------
# Frame → payload mapping
# ---------------------------------------------------------------------------
def _frame_to_payloads(
    frame: ProcessFrame, decoder: NdjsonDecoder
) -> list[dict[str, Any]]:
    """Map a :class:`ProcessFrame` onto zero-or-more RunFrame payloads.

    Stdout frames are *expanded*: a single 64 KB chunk may contain many
    NDJSON event lines, so we yield one payload per decoded line plus
    one synthetic ``stdout_log`` payload per malformed line.

    Stderr / started / terminated frames map 1:1.
    """
    if isinstance(frame, ProcessStartedFrame):
        return [{"event": "started", "pid": frame.pid}]
    if isinstance(frame, ProcessStdoutFrame):
        out: list[dict[str, Any]] = []
        for line in decoder.feed(frame.data):
            out.append(_decode_stdout_line(line))
        return out
    if isinstance(frame, ProcessStderrFrame):
        text = frame.data.decode("utf-8", errors="replace")
        return [
            {
                "event": "log",
                "stream": "stderr",
                "data": text,
                "size": len(frame.data),
            }
        ]
    if isinstance(frame, ProcessTerminatedFrame):
        status = frame.status
        return [
            {
                "event": "terminated",
                "exit_code": status.exit_code,
                "timed_out": status.timed_out,
                "truncated": status.truncated,
            }
        ]
    # Defensive: future frame kinds surface as-is.
    return [{"event": "unknown", "kind": getattr(frame, "kind", None)}]


def _decode_stdout_line(line: str) -> dict[str, Any]:
    """Decode one stdout text line into a RunFrame payload dict."""
    event = decode_event(line)
    payload: dict[str, Any]
    if isinstance(event, StatusEvent):
        payload = {"event": "status", "state": event.state}
        _merge_payload(payload, event.payload, exclude={"type", "state"})
        return payload
    if isinstance(event, ProgressEvent):
        payload = {
            "event": "progress",
            "phase": event.phase,
            "pct": event.pct,
        }
        _merge_payload(payload, event.payload, exclude={"type", "phase", "pct"})
        return payload
    if isinstance(event, MetricsEvent):
        payload = {"event": "metrics"}
        if event.latency_ms is not None:
            payload["latencyMs"] = event.latency_ms
        if event.memory_mb is not None:
            payload["memoryMB"] = event.memory_mb
        if event.device is not None:
            payload["device"] = event.device
        _merge_payload(
            payload,
            event.payload,
            exclude={"type", "latencyMs", "memoryMB", "device"},
        )
        return payload
    if isinstance(event, LogEvent):
        payload = {
            "event": "log",
            "stream": event.stream,
            "line": event.line,
        }
        _merge_payload(
            payload, event.payload, exclude={"type", "stream", "line"}
        )
        return payload
    if isinstance(event, ResultEvent):
        payload = {"event": "result", "output": dict(event.output)}
        _merge_payload(payload, event.payload, exclude={"type", "output"})
        return payload
    if isinstance(event, DoneEvent):
        payload = {"event": "done"}
        _merge_payload(payload, event.payload, exclude={"type"})
        return payload
    if isinstance(event, ErrorEvent):
        payload = {
            "event": "error",
            "code": event.code,
            "message": event.message,
        }
        _merge_payload(
            payload, event.payload, exclude={"type", "code", "message"}
        )
        return payload
    if isinstance(event, UnknownRunnerEvent):
        # Forward-compat: pass through the original payload but tag it
        # so consumers can route on ``event="unknown_runner"``.
        out: dict[str, Any] = {
            "event": "unknown_runner",
            "declared_type": event.declared_type,
        }
        _merge_payload(out, event.payload, exclude={"type"})
        return out
    if isinstance(event, StdoutLogEvent):
        return {
            "event": "log",
            "stream": "stdout",
            "line": event.line,
        }
    # Shouldn't be reachable.
    return {"event": "log", "stream": "stdout", "line": event.raw}  # pragma: no cover


def _merge_payload(
    target: dict[str, Any],
    source: Any,
    *,
    exclude: set[str],
) -> None:
    """Copy the unrecognised fields from ``source`` into ``target``.

    Forward compatibility: if a Pack runner emits extra fields we don't
    yet model, surface them verbatim so a presenter can consume them.
    Skip ``type`` (we already mapped that to ``event``) and any keys
    we already wrote.
    """
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if key in exclude:
            continue
        if key in target:
            continue
        target[key] = value
