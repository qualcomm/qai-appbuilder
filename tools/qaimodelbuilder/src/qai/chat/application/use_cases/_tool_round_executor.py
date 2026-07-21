# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared parallel tool-round execution skeleton (main + sub-agent).

Both the **main agent** loop (``streaming.py``) and the **sub-agent** loop
(``agent_tool.py``) execute one round's tool calls. Historically they had two
separate implementations:

* sub-agent: ``asyncio.gather`` over all calls (PARALLEL), final-only items;
* main agent: a serial ``for`` loop over non-``agent`` calls (SERIAL), with
  exec streaming partials.

This module hosts the SHARED skeleton both sides now call so the round-level
concurrency, per-slot wall-clock timing, abort racing, exception isolation,
original-order emission and ``call_id`` normalisation live in ONE place:

    execute_tools_in_parallel(
        tool_metas=[(name, args, call_id), ...],
        run_one=<per-call coroutine factory>,
        abort_event=<optional asyncio.Event>,       # whole-round Stop
        cancel_check=<optional (call_id) -> bool>,   # single-tool cancel_tool
    ) -> list[_SlotResult]   # in ORIGINAL order, one per call

The caller owns the per-call execution body (``run_one``) and how it turns the
raw result into a :class:`ToolExecutionItem` — that is where main (guardrail /
operator hooks / exec streaming partials / frame passthrough) and sub
(``agent`` recursion guard, final-only) differ. The skeleton stays neutral.

Per-call ordering contract (see ``parallel-tool-execution-design.md`` §1):
frames from different ``call_id`` MAY interleave, but within one ``call_id``
partials precede the single final. The skeleton itself only produces final
slot results (the caller streams partials around it via ``run_one`` when it
wants); it guarantees results are returned in ORIGINAL ``tool_metas`` order.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Per-call execution body injected by the caller: given (tool_name, arguments,
# call_id) returns the raw result (any type) or raises. The skeleton never
# inspects the value — the caller's result handler does (truncation / ok
# judgement / ToolExecutionItem shaping).
RunOne = Callable[[str, dict[str, Any], str], Awaitable[Any]]


@runtime_checkable
class ConcurrencySlot(Protocol):
    """Minimal protocol for the shared :class:`ToolConcurrencyManager`.

    Kept as a structural type so this application-layer module does not import
    the concrete platform class directly (DI injects the real one). ``slot``
    returns an async context manager acquiring the total + per-tool budget for
    the duration of one call.
    """

    def slot(self, tool_name: str) -> Any: ...


@dataclass(slots=True)
class SlotResult:
    """One tool call's outcome, carrying enough to rebuild a result item.

    ``raw`` is whatever ``run_one`` returned, OR a ``BaseException`` when the
    call raised (``asyncio.gather(return_exceptions=True)`` parity), OR an
    :class:`InterruptedError` instance when the abort event won the race
    before the tool finished. ``duration_ms`` is the per-call wall-clock time
    recorded in a ``finally`` so it is set on success / failure / interrupt
    alike.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    raw: Any
    duration_ms: int
    interrupted: bool = False


def normalize_call_id(
    *, raw_call_id: str | None, round_no: int, index: int
) -> str:
    """Return a stable, round-unique ``call_id`` for one tool call.

    Priority (design §3): upstream id (cloud ``tool_calls[].id``) → synthesised
    ``tc_r{round}_{index}`` when absent (local-model path emits no id). NEVER
    derived from tool name or argument hash (two identical calls are legal and
    must still get distinct ids so the UI binds partials to the right card and
    parallel same-named tools — e.g. two ``exec`` — never collide).
    """
    cid = (raw_call_id or "").strip()
    if cid:
        return cid
    return f"tc_r{round_no}_{index}"


async def execute_tools_in_parallel_stream(
    *,
    tool_metas: list[tuple[str, dict[str, Any], str]],
    run_one: RunOne,
    abort_event: asyncio.Event | None = None,
    concurrency: ConcurrencySlot | None = None,
    cancel_check: Callable[[str], bool] | None = None,
    cancel_poll_s: float = 0.05,
) -> AsyncIterator[SlotResult]:
    """Execute one round's tool calls in PARALLEL; yield each as it FINISHES.

    Identical concurrency / budget / abort-racing / single-tool-cancel /
    exception-isolation / outer-cancel-reap semantics to
    :func:`execute_tools_in_parallel` (which now merely COLLECTS this stream in
    original order) — the ONLY difference is emission timing: this generator
    yields each :class:`SlotResult` the instant that slot completes (completion
    order), instead of waiting for a whole-round ``gather`` barrier. That lets a
    per-slot caller flip a cancelled / fast tool card immediately rather than
    stalling every card until the slowest call finishes (父子统一 with the
    takeover path's per-slot dispatch). The kernel consuming these re-pairs
    results by ``call_id`` (``_single_agent_turn._execute_tool_round``), so the
    wire fed back to the model stays in issue order regardless of yield order.

    Contract highlights (see :func:`execute_tools_in_parallel` for the full
    prose): concurrent (latency ``max(t_i)``); per-call wall-clock timer in a
    ``finally``; ``abort_event`` losers return an ``InterruptedError`` instance;
    ``cancel_check`` picks off ONE call by ``call_id`` while the others keep
    running; a raising tool surfaces its exception as the slot ``raw``
    (``return_exceptions`` parity). On an OUTER cancel / ``GeneratorExit`` every
    still-running slot task is cancelled + reaped so ``exec.py`` tree-kills the
    subprocess before we unwind (no orphaned child).
    """
    n = len(tool_metas)
    durations_ms: list[int] = [0] * n

    async def _run_body(slot: int, name: str, args: dict[str, Any]) -> tuple[Any, bool]:
        call_id = tool_metas[slot][2]
        t0 = time.perf_counter()
        try:
            # Fast path: no abort event AND no per-call cancel check → run the
            # call directly (zero racing overhead, byte-identical to the old
            # non-abort path).
            if abort_event is None and cancel_check is None:
                return await run_one(name, args, call_id), False
            # Racing path: watch the call against the whole-round abort (when
            # given) AND poll the per-call cancel signal (when given). Both make
            # the loser return an ``InterruptedError`` (NOT raised) so a long
            # cancel-only tool like ``exec`` is torn down promptly. The
            # per-call cancel differs from the abort ONLY in that it targets
            # THIS single call — the caller's other slots (other tasks) are
            # untouched, so the round keeps running.
            exec_task = asyncio.ensure_future(run_one(name, args, call_id))
            abort_task = (
                asyncio.ensure_future(abort_event.wait())
                if abort_event is not None
                else None
            )
            watch: set[asyncio.Future[Any]] = {exec_task}
            if abort_task is not None:
                watch.add(abort_task)
            # Outcome decided by the loop below; ``None`` while still racing.
            #   ("done",  None)      → exec_task finished, surface its result
            #   ("abort", None)      → whole-round abort won → [interrupted]
            #   ("cancel", None)     → single-tool cancel hit → [cancelled]
            outcome_kind: str | None = None
            try:
                while True:
                    # ``timeout`` bounds each wait so the per-call cancel poll
                    # runs even when neither the tool nor the abort fires; when
                    # there is no ``cancel_check`` we simply block until a
                    # watched future completes (timeout=None → old behaviour).
                    _timeout = cancel_poll_s if cancel_check is not None else None
                    done, _pending = await asyncio.wait(
                        watch,
                        timeout=_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if exec_task in done:
                        outcome_kind = "done"
                        break
                    if abort_task is not None and abort_task in done:
                        outcome_kind = "abort"
                        break
                    # No watched future finished within the poll window: check
                    # the single-tool cancel signal. A hit stops watching (the
                    # ``finally`` below cancels + reaps the exec task → exec.py
                    # tree-kill) and marks this ONE call ``[cancelled]``.
                    if cancel_check is not None and cancel_check(call_id):
                        outcome_kind = "cancel"
                        break
            except asyncio.CancelledError:
                # OUTER task-level cancel (the main-agent Stop cascade —
                # ``streaming.py`` ``gather_task.cancel()``): a bare
                # ``asyncio.wait`` does NOT cancel the futures it was
                # watching, so ``exec_task`` (a long ``exec`` running a
                # PowerShell child) would be left ORPHANED — its
                # ``CancelledError`` handler in ``exec.py`` (which tree-kills
                # the subprocess) would never fire and the child would run
                # to completion with the tool card stuck "executing". We
                # therefore cancel + reap BOTH child tasks HERE so the
                # cancel propagates into ``exec.py`` and the process tree is
                # actually killed, then re-raise so the outer cancel
                # semantics stay correct (NEVER swallow the outer cancel).
                for t in (exec_task, abort_task):
                    if t is not None and not t.done():
                        t.cancel()
                for t in (exec_task, abort_task):
                    if t is None:
                        continue
                    try:
                        # ``exec.py`` shields its ``terminate_process_tree``
                        # reap, so awaiting the cancelled ``exec_task`` here
                        # blocks until the subprocess is actually killed
                        # (not merely signalled) before we unwind.
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        # Swallow ONLY this inner reap's exceptions (incl. a
                        # nested cancel delivered while we await) — the outer
                        # ``raise`` below preserves the real cancel.
                        pass
                raise
            finally:
                # Reap whatever is still pending on ANY loop exit (the abort or
                # per-call-cancel winner's still-running exec task, plus the
                # still-parked abort waiter on a normal completion). ``exec.py``
                # shields its tree-kill reap so awaiting a cancelled exec task
                # blocks until the child is provably dead before we unwind.
                for t in (exec_task, abort_task):
                    if t is None or t.done():
                        continue
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            if outcome_kind == "done":
                # Surface the real result (or its exception) — re-raising here
                # lets the ``return_exceptions=True`` capture below record it as
                # the slot value, identical to a non-abort run.
                return exec_task.result(), False
            if outcome_kind == "cancel":
                # Single-tool cancel_tool: ONLY this call is torn down; the
                # other slots keep running so the round continues.
                return (
                    InterruptedError(
                        f"[cancelled] tool '{name}' cancelled by user"
                    ),
                    True,
                )
            # ``abort`` (or defensive fall-through): whole-round abort winner.
            return (
                InterruptedError(f"[interrupted] tool '{name}' aborted by user"),
                True,
            )
        finally:
            durations_ms[slot] = int((time.perf_counter() - t0) * 1000)

    async def _timed(slot: int, name: str, args: dict[str, Any]) -> tuple[Any, bool]:
        # Acquire the shared budget slot OUTSIDE the timer (queueing for the
        # budget is not tool execution time). A short-circuited abort still
        # holds no slot. Unbounded manager / None → no-op (zero overhead).
        if concurrency is not None:
            async with concurrency.slot(name):
                return await _run_body(slot, name, args)
        return await _run_body(slot, name, args)

    if n == 0:
        return

    # One task per slot; a task→slot map lets us build the ORIGINAL-order
    # ``SlotResult`` as each finishes (``asyncio.wait(FIRST_COMPLETED)`` in a
    # loop = "yield as completed" without ``asyncio.as_completed``'s wrapper
    # futures, so the outer-cancel reap below can cancel the REAL slot tasks).
    slot_of: dict[asyncio.Task[tuple[Any, bool]], int] = {}
    for slot, (name, args, _cid) in enumerate(tool_metas):
        slot_of[asyncio.ensure_future(_timed(slot, name, args))] = slot
    pending: set[asyncio.Task[tuple[Any, bool]]] = set(slot_of)

    def _slot_result(slot: int, outcome: Any) -> SlotResult:
        name, args, cid = tool_metas[slot]
        # ``_timed`` either returns ``(value_or_exc, interrupted)`` or — if it
        # itself raised (should not happen, defensive) — the task captured the
        # exception directly.
        if isinstance(outcome, BaseException):
            raw: Any = outcome
            interrupted = False
        else:
            raw, interrupted = outcome
        return SlotResult(
            call_id=cid,
            tool_name=name,
            arguments=args,
            raw=raw,
            duration_ms=durations_ms[slot],
            interrupted=interrupted,
        )

    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                slot = slot_of[task]
                try:
                    outcome = task.result()
                except BaseException as exc:  # noqa: BLE001 — parity capture
                    outcome = exc
                yield _slot_result(slot, outcome)
    except (asyncio.CancelledError, GeneratorExit):
        # OUTER cancel / consumer aclose(): cancel + reap every still-running
        # slot task so each in-flight call's ``exec.py`` tree-kill fires (no
        # orphaned subprocess) before we unwind. Mirrors the per-call
        # ``CancelledError`` reap inside ``_run_body`` for the WHOLE batch.
        for task in pending:
            if not task.done():
                task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        raise


async def execute_tools_in_parallel(
    *,
    tool_metas: list[tuple[str, dict[str, Any], str]],
    run_one: RunOne,
    abort_event: asyncio.Event | None = None,
    concurrency: ConcurrencySlot | None = None,
    cancel_check: Callable[[str], bool] | None = None,
    cancel_poll_s: float = 0.05,
) -> list[SlotResult]:
    """Execute one round's tool calls in PARALLEL; return results in order.

    Mirrors the historical sub-agent ``_tool_executor`` skeleton
    (``agent_tool.py``: ``asyncio.gather(return_exceptions=True)`` + per-slot
    timing + abort racing) but is now caller-agnostic:

    * each call runs concurrently (latency ``max(t_i)`` not ``sum(t_i)``);
    * when ``concurrency`` is given, each call first acquires a budget slot
      (total + per-tool bucket) so a shared budget bounds the whole turn across
      main + sub agents (design §5) — the wall-clock timer starts AFTER the
      slot is acquired so queueing time is not billed as tool time;
    * a per-call wall-clock timer is recorded in ``finally`` (success / error /
      interrupt all carry a ``duration_ms``);
    * when ``abort_event`` is given, each call races the abort so a long tool
      (e.g. a 120 s shell ``exec``) is interrupted promptly on user Stop —
      losers return an ``InterruptedError`` instance (NOT raised), matching the
      old sub-agent behaviour;
    * when ``cancel_check`` is given, each call ALSO polls
      ``cancel_check(call_id)`` (the SAME per-tool cancel signal the main agent
      polls via ``handle.consume_cancel_tool`` — see ``streaming.py`` dispatch)
      on the ``cancel_poll_s`` cadence: a hit cancels ONLY that one call's task
      (propagating the ``CancelledError`` into ``exec.py`` → subprocess
      tree-kill) and returns an ``InterruptedError`` for THAT slot, while the
      OTHER slots keep running and the round (turn) continues — this is the
      single-tool ``cancel_tool`` semantics (NOT a whole-turn abort). Distinct
      from ``abort_event``: the abort tears the whole round down; the cancel
      check picks off one call by ``call_id`` and leaves the rest alone;
    * a raising tool yields its exception as the slot ``raw`` (gather
      ``return_exceptions=True`` parity) so one failure never aborts the batch;
    * results are returned in ORIGINAL ``tool_metas`` order regardless of
      completion order (the caller emits final items / frames from them).

    This is a thin COLLECTOR over :func:`execute_tools_in_parallel_stream`
    (the single source of truth for the execution semantics): it drains the
    per-slot stream and re-orders the results into ORIGINAL ``tool_metas``
    order (an ``InterruptedError`` outcome carries no ordering info, so we key
    by ``call_id`` slot). Existing callers that need the whole ordered batch
    (the main loop's dispatch, tests) keep this API unchanged.
    """
    by_call_id: dict[str, SlotResult] = {}
    async for slot_res in execute_tools_in_parallel_stream(
        tool_metas=tool_metas,
        run_one=run_one,
        abort_event=abort_event,
        concurrency=concurrency,
        cancel_check=cancel_check,
        cancel_poll_s=cancel_poll_s,
    ):
        by_call_id[slot_res.call_id] = slot_res
    # Re-order into ORIGINAL ``tool_metas`` order. Duplicate ``call_id`` (two
    # identical un-id'd calls) cannot happen here because ``normalize_call_id``
    # already gave each slot a distinct id upstream; a missing slot (impossible
    # unless the stream was torn down mid-flight, which re-raises) is simply
    # absent so ``strict`` iteration below would not KeyError in practice — we
    # index defensively by position through the metas.
    results: list[SlotResult] = []
    for _name, _args, cid in tool_metas:
        sr = by_call_id.get(cid)
        if sr is not None:
            results.append(sr)
    return results
