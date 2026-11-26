# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-user-task token / wall-clock ledger (main agent + every sub-agent).

A *task* is one user request: it starts when the user sends a message and
ends when the request is fully handled — including background sub-agents
(any depth) it spawned and the headless follow-up turn(s) that integrate
their results.

Accounting is strictly **per LLM round, incremental**: every round that
actually ran (main agent round 0, follow-up rounds, suppressed truncation
retries, every sub-agent round) adds its OWN billed figures exactly once.
Nothing is ever read back from cumulative aggregates such as
``SubAgentSession.usage``, so a resumed / woken sub-agent contributes only the
rounds it runs for THIS task — never its historical totals.

Per-round billed口径 (input from :func:`_extract_usage`'s normalised dict):

* ``input``  — the FULL prompt actually sent that round, cached parts
  included. Claude/Anthropic split cache reads/writes OUT of
  ``prompt_tokens`` (unless ``_extract_usage`` already corrected
  ``prompt_tokens`` to ``total - completion``, in which case the cache split
  fields are absent/zeroed), so they are added back; OpenAI / Gemini already
  fold cached tokens into ``prompt_tokens``.
* ``cache_read`` / ``cache_write`` — portions of ``input`` served from /
  written to the prompt cache (display breakdown, already inside ``input``).
* ``output`` — ``completion_tokens`` (reasoning tokens are inside it).
* ``reasoning`` — portion of ``output`` spent on hidden reasoning
  (``_extract_usage``'s ``reasoning_tokens``; display breakdown only).

In-memory and process-scoped: a restart forgets running tasks (the summaries
already stamped onto persisted messages survive). Pure bookkeeping — every
method is synchronous, never raises and never blocks.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TaskUsageLedger", "billed_round_usage"]

#: Keep at most this many tasks in memory (oldest evicted first).
_MAX_TASKS = 256


def _int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return max(0, int(v))
    return 0


def billed_round_usage(
    usage: dict[str, Any] | None, model_id: str | None
) -> dict[str, int] | None:
    """Billed figures of ONE LLM round; ``None`` when no usable usage."""
    if not isinstance(usage, dict):
        return None
    pt = _int(usage.get("prompt_tokens"))
    out = _int(usage.get("completion_tokens"))
    cr_split = _int(usage.get("cache_read_tokens"))
    details = usage.get("prompt_tokens_details")
    cw_details = (
        _int(details.get("cache_write_tokens")) if isinstance(details, dict) else 0
    )
    # ``cache_write_observed`` covers BOTH the nested details key and the
    # top-level ``cache_creation_input_tokens`` / ``cache_write_tokens``.
    cache_write = _int(usage.get("cache_write_observed")) or cw_details
    cache_read = _int(usage.get("cache_read_observed")) or cr_split
    is_anthropic = isinstance(model_id, str) and "claude" in model_id.lower()
    # ``prompt_includes_cache`` (set by ``_extract_usage`` when it corrected
    # prompt to total-completion) ⇒ prompt is already the full wire. Otherwise
    # Anthropic reports cache read + cache write OUTSIDE prompt_tokens.
    split = is_anthropic and not bool(usage.get("prompt_includes_cache"))
    inp = pt + (cr_split + cache_write if split else 0)
    # Reasoning (hidden chain-of-thought) tokens: OpenAI-style gateways count
    # them INSIDE ``completion_tokens`` (display breakdown only).
    reasoning = _int(usage.get("reasoning_tokens"))
    if inp == 0 and out == 0:
        return None
    return {
        "input": inp,
        "output": out,
        "cache_read": min(cache_read, inp),
        "cache_write": min(cache_write, inp),
        "reasoning": min(reasoning, out),
    }


def _zero() -> dict[str, int]:
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "reasoning": 0, "rounds": 0,
    }


@dataclass
class _Task:
    task_id: str
    conversation_id: str
    started_ms: int
    model: str | None
    main: dict[str, int] = field(default_factory=_zero)
    #: subagent_id → {"name", totals...} for THIS task only.
    subs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: sub-agent runs of this task still in flight.
    active: set[str] = field(default_factory=set)


class TaskUsageLedger:
    """Process-scoped ledger: ``task_id`` → per-round incremental totals."""

    def __init__(self, max_tasks: int = _MAX_TASKS) -> None:
        self._tasks: OrderedDict[str, _Task] = OrderedDict()
        self._latest_by_conv: dict[str, str] = {}
        self._task_by_subagent: dict[str, str] = {}
        self._max = max(1, int(max_tasks))

    # ── lifecycle ─────────────────────────────────────────────────────────
    def open_task(
        self,
        task_id: str,
        *,
        conversation_id: str,
        started_ms: int,
        model: str | None,
    ) -> None:
        if not task_id or task_id in self._tasks:
            return
        self._tasks[task_id] = _Task(
            task_id=task_id,
            conversation_id=conversation_id,
            started_ms=int(started_ms),
            model=model,
        )
        self._latest_by_conv[conversation_id] = task_id
        while len(self._tasks) > self._max:
            old_id, old = self._tasks.popitem(last=False)
            if self._latest_by_conv.get(old.conversation_id) == old_id:
                self._latest_by_conv.pop(old.conversation_id, None)
            for sid in [s for s, t in self._task_by_subagent.items() if t == old_id]:
                self._task_by_subagent.pop(sid, None)

    def has_task(self, task_id: str | None) -> bool:
        return bool(task_id) and task_id in self._tasks

    def latest_task(self, conversation_id: str) -> str | None:
        return self._latest_by_conv.get(conversation_id)

    def task_for_subagent(self, subagent_id: str | None) -> str | None:
        return self._task_by_subagent.get(subagent_id) if subagent_id else None

    def begin_subagent(
        self, task_id: str | None, subagent_id: str, name: str | None
    ) -> None:
        task = self._tasks.get(task_id) if task_id else None
        if task is None or not subagent_id:
            return
        self._task_by_subagent[subagent_id] = task.task_id
        entry = task.subs.setdefault(subagent_id, {"name": name or "agent", **_zero()})
        if name:
            entry["name"] = name
        task.active.add(subagent_id)

    def end_subagent(self, task_id: str | None, subagent_id: str | None) -> None:
        task = self._tasks.get(task_id) if task_id else None
        if task is not None and subagent_id:
            task.active.discard(subagent_id)

    # ── accounting ────────────────────────────────────────────────────────
    def add_round(
        self,
        task_id: str | None,
        usage: dict[str, Any] | None,
        *,
        model_id: str | None,
        subagent_id: str | None = None,
    ) -> None:
        """Fold ONE round's usage (exactly once per round) into the task."""
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            return
        billed = billed_round_usage(usage, model_id or task.model)
        if billed is None:
            return
        if subagent_id:
            bucket = task.subs.setdefault(subagent_id, {"name": "agent", **_zero()})
        else:
            bucket = task.main
        for k, v in billed.items():
            bucket[k] = int(bucket.get(k, 0)) + v
        bucket["rounds"] = int(bucket.get("rounds", 0)) + 1

    # ── read ──────────────────────────────────────────────────────────────
    def snapshot(self, task_id: str | None, *, now_ms: int) -> dict[str, Any] | None:
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            return None
        total = {
            k: int(task.main.get(k, 0))
            for k in ("input", "output", "cache_read", "cache_write", "reasoning")
        }
        subs_out: list[dict[str, Any]] = []
        for sid, s in task.subs.items():
            for k in total:
                total[k] += int(s.get(k, 0))
            subs_out.append(
                {
                    "id": sid,
                    "name": s.get("name") or "agent",
                    "tokens": int(s.get("input", 0)) + int(s.get("output", 0)),
                    "input": int(s.get("input", 0)),
                    "output": int(s.get("output", 0)),
                    "rounds": int(s.get("rounds", 0)),
                    "running": sid in task.active,
                }
            )
        if not any(total.values()):
            return None
        summary: dict[str, Any] = {
            "task_id": task.task_id,
            "total_tokens": total["input"] + total["output"],
            "input_tokens": total["input"],
            "output_tokens": total["output"],
            "cache_read_tokens": total["cache_read"],
            "cache_write_tokens": total["cache_write"],
            "reasoning_tokens": total["reasoning"],
            "main_tokens": task.main["input"] + task.main["output"],
            "main_rounds": task.main["rounds"],
            "duration_ms": max(0, int(now_ms) - task.started_ms),
            "complete": not task.active,
        }
        if task.model:
            summary["model"] = task.model
        if subs_out:
            summary["sub_agents"] = subs_out
        return summary
