# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The exec 4-way race: "what ended this foreground wait, and why".

Purpose
-------
A long-running shell command occupies the agent's foreground turn.  Four
independent things can legitimately end that foreground wait, and the
caller must react DIFFERENTLY to each:

1. **completion** — the command finished on its own.  Return its real
   result synchronously; a 30ms ``git status`` must never be deferred.
2. **running** — the auto-background *threshold* elapsed while the
   command was still going.  Hand the child to the background-process
   manager and let the turn continue.
3. **aborted** — the user pressed Stop.  Kill the child.
4. **steer** — the user injected a fresh mid-turn instruction
   (Ctrl+Enter).  Same handoff as ``running``, but triggered by intent
   rather than by the clock: the user is waiting on an answer NOW, so
   the command must not keep holding the turn hostage.

Before this module each exec path hand-rolled its own 2-way wait, which
could only distinguish "done" from "park now" — a user injection had to
be laundered through the threshold budget, so an injection at t=2s with
a 15s threshold was silently deferred for 13 more seconds.  Making the
four signals first-class removes that conflation.

Why ``qai.platform``
--------------------
The two consumers live in DIFFERENT bounded contexts
(``qai.ai_coding...handlers.exec`` for the non-streaming path,
``apps.api.di`` for the streaming one), and ``.importlinter``'s
``context-isolation`` contract forbids a cross-context import.  Only
``qai.platform.**`` is whitelisted for every context, which is why this
shared coordination primitive lives here.

Contract (invariants)
---------------------
* Pure coordination: the function OWNS none of the four signals.  It
  never sets ``stop_signal`` / ``injection_signal``, never cancels
  ``job_future``, and never kills a process — deciding *what* to do is
  the caller's job, deciding *what happened* is ours.
* Every arm is independently skippable, and a skipped arm is skipped
  outright — never faked with a never-firing event, which would report
  a live arm that can never win:
    - ``threshold_ms`` of ``None`` or ``<= 0`` skips the threshold arm.
      That is the "auto-background disabled" setting: turning the
      feature off must not also disable Stop or mid-turn steering.
    - ``stop_signal`` / ``injection_signal`` of ``None`` mean the DI
      wiring did not thread that capability, so that outcome is simply
      unreachable for this call.
  Skipping every arm is a caller bug (the race would await a lone
  ``job_future`` while claiming to race) and is rejected up front.
* ``job_future`` is never renamed, never wrapped in a way that leaks
  back to the caller, and — when it is already a
  :class:`asyncio.Task` — never cancelled by this function.  Winner
  identification is by object **identity**, not by task name: a task
  name is caller-owned metadata that this function has no business
  reading or (worse) overwriting.
* Every arm this function created is cancelled AND reaped before
  returning, so no "Task exception was never retrieved" warning can
  surface after the winner is known.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from qai.platform.logging import get_logger

__all__ = ["ExecRaceOutcome", "wait_for_completion"]

_log = get_logger(__name__)

#: What ended the foreground wait.  ``"running"`` and ``"steer"`` both
#: mean "hand off to the background-process manager"; they are kept
#: distinct so the caller can log / message the user accurately (clock
#: vs. explicit user intent).
ExecRaceOutcome = Literal["completed", "running", "steer", "aborted"]


async def wait_for_completion(
    *,
    job_future: "asyncio.Future[object]",
    threshold_ms: int | None,
    stop_signal: asyncio.Event | None,
    injection_signal: asyncio.Event | None,
) -> tuple[ExecRaceOutcome, object]:
    """Race the command against threshold, Stop, and mid-turn injection.

    Args:
        job_future: The in-flight command.  Awaited, never cancelled;
            when it wins, its ``result()`` is returned as-is.  A raising
            future propagates its exception to the caller (a crashed
            command is the caller's error to shape, not an outcome).
        threshold_ms: Auto-background threshold in milliseconds.
            ``None`` or ``<= 0`` skips that arm — the others still race.
        stop_signal: Set when the user pressed Stop.  ``None`` skips the
            Stop arm (DI did not thread it), making ``"aborted"``
            unreachable for this call.
        injection_signal: Set when the user injected fresh input into
            this turn (per-tab; see
            :class:`qai.chat.adapters.injection_signal_bridge.InjectionSignalBridge`).
            ``None`` skips the injection arm, making ``"steer"``
            unreachable for this call.

    Returns:
        ``("completed", <job result>)`` — finished on its own;
        ``("running", None)`` — threshold elapsed, hand off;
        ``("steer", None)`` — user injected, hand off;
        ``("aborted", None)`` — user pressed Stop, kill it.

        When several signals land in the same event-loop pass the
        priority is completion > threshold > stop > injection: a command
        that genuinely finished should report its real output even if
        the user hit Stop in the same tick.

    Raises:
        ValueError: When every non-completion arm is skipped.  There is
            no race left to run, so the caller should simply await its
            job — silently degrading to that would hide broken wiring.
    """
    if (
        (threshold_ms is None or threshold_ms <= 0)
        and stop_signal is None
        and injection_signal is None
    ):
        raise ValueError(
            "exec race needs at least one of threshold_ms / stop_signal /"
            " injection_signal; all three are disabled"
        )

    # ``ensure_future`` on an already-Task returns THAT SAME object.  We
    # therefore keep a separate identity handle and never mutate it (no
    # ``set_name``: that would rewrite caller-owned metadata) and never
    # cancel it below.
    completion_task = asyncio.ensure_future(job_future)
    ours: list[asyncio.Task[object]] = []

    threshold_task: asyncio.Task[None] | None = None
    if threshold_ms is not None and threshold_ms > 0:
        threshold_task = asyncio.ensure_future(
            asyncio.sleep(threshold_ms / 1000.0)
        )
        ours.append(threshold_task)
    stop_task: asyncio.Task[bool] | None = None
    if stop_signal is not None:
        stop_task = asyncio.ensure_future(stop_signal.wait())
        ours.append(stop_task)
    injection_task: asyncio.Task[bool] | None = None
    if injection_signal is not None:
        injection_task = asyncio.ensure_future(injection_signal.wait())
        ours.append(injection_task)

    try:
        done, _pending = await asyncio.wait(
            [completion_task, *ours],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Reap ONLY the arms we minted.  ``completion_task`` belongs to
        # the caller: cancelling it here would kill a command that is
        # merely being backgrounded.
        for task in ours:
            task.cancel()
        if ours:
            await asyncio.gather(*ours, return_exceptions=True)

    outcome: ExecRaceOutcome
    if completion_task in done:
        outcome = "completed"
        # ``.result()`` re-raises a command-level exception on purpose —
        # a crash is not a race outcome.
        payload: object = completion_task.result()
    elif threshold_task is not None and threshold_task in done:
        outcome, payload = "running", None
    elif stop_task is not None and stop_task in done:
        outcome, payload = "aborted", None
    elif injection_task is not None and injection_task in done:
        outcome, payload = "steer", None
    else:  # pragma: no cover — asyncio.wait guarantees a non-empty done
        raise RuntimeError(
            "exec race woke with no recognised winner"
            f" (done={len(done)} tasks)"
        )

    _log.info(
        "exec_race.outcome",
        outcome=outcome,
        threshold_ms=threshold_ms,
        threshold_armed=threshold_task is not None,
        stop_armed=stop_task is not None,
        injection_armed=injection_task is not None,
    )
    return outcome, payload
