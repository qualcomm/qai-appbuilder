# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory per-sub-agent cancellation registry (block 3).

Runtime-defect fix: a sub-agent used to have NO independent abort handle —
it ran inside its parent conversation's outer stream, so the only way to
stop it was to interrupt the WHOLE parent tab (which also killed the main
agent). ``POST /api/chat/subagents/{id}/interrupt`` therefore had to return
a truthful ``aborted=False`` ("fake interrupt").

This registry gives each running sub-agent its OWN cooperative-cancellation
flag, keyed by the sub-agent's persisted id (``SubAgentSessionId.value``).
It mirrors :class:`~qai.chat.adapters.stream_abort_registry.InMemoryStreamAbortRegistry`
(which is keyed by ``TabId``) — same in-memory, process-local model
(in-flight handles are inherently process-local; durability is not
required). The sub-agent loop registers its flag on start, checks it
between rounds (cooperative abort — it finishes the in-flight round then
stops), and unregisters on exit; the interrupt endpoint signals it so a
standalone sub-agent tab's "stop" button actually stops ONLY that sub-agent
without touching the main agent.

State-Truth-First (§7): the registry reflects the REAL set of in-flight
sub-agents — ``abort`` returns ``True`` only when a flag is actually
registered and signalled, so the endpoint reports the truthful outcome.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = ["ActiveSubAgentSnapshot", "InMemorySubAgentAbortRegistry"]


@dataclass(frozen=True, slots=True)
class ActiveSubAgentSnapshot:
    """Process-local snapshot of a running sub-agent cancellation flag."""

    subagent_id: str
    started_at: datetime
    aborted: bool
    reason: str | None = None


@dataclass(slots=True)
class _SubAgentAbortRecord:
    event: asyncio.Event
    started_at: datetime
    reason: str | None = None
    # Cascade-abort ownership (runtime-defect fix): the ``tab_id`` of the
    # parent conversation tab that SPAWNED this sub-agent, recorded at
    # ``register`` time so a Stop on that tab (which only signals the
    # ``stream_abort_registry`` handle keyed by ``tab_id``) can ALSO cascade
    # into every sub-agent it派生. ``None`` when the spawn context did not
    # supply an owner (legacy / standalone-only paths) — such a record is
    # simply never matched by :meth:`abort_by_owner_tab` (it can still be
    # aborted directly by id via the interrupt endpoint).
    owner_tab_id: str | None = None


class InMemorySubAgentAbortRegistry:
    """Process-local cancellation registry keyed by sub-agent id."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[str, _SubAgentAbortRecord] = {}

    def register(
        self, subagent_id: str, *, owner_tab_id: str | None = None
    ) -> asyncio.Event:
        """Allocate (or reuse) the cancellation event for ``subagent_id``.

        Returns the :class:`asyncio.Event` the sub-agent loop polls between
        rounds. A re-run / wake of the same id starts from a FRESH, cleared
        event so a stale prior signal does not abort the new run.

        ``owner_tab_id`` (additive — cascade-abort ownership) records the
        parent tab that spawned this sub-agent so a Stop on that tab can
        cascade-abort it via :meth:`abort_by_owner_tab`. Optional so the
        historical call ``register(id)`` (standalone / unit stubs) keeps
        working unchanged; when omitted the record is never matched by the
        owner-cascade (only a direct id abort reaches it). On a reused
        (still-live) event the owner is refreshed to the latest spawn context
        so a woken run re-attributes to the tab now driving it.
        """
        existing = self._records.get(subagent_id)
        if existing is not None and not existing.event.is_set():
            if owner_tab_id is not None:
                existing.owner_tab_id = owner_tab_id
            return existing.event
        event = asyncio.Event()
        self._records[subagent_id] = _SubAgentAbortRecord(
            event=event,
            started_at=datetime.now(UTC),
            owner_tab_id=owner_tab_id,
        )
        return event

    def unregister(self, subagent_id: str) -> None:
        """Drop the event for ``subagent_id``. Idempotent."""
        self._records.pop(subagent_id, None)

    def abort(self, subagent_id: str) -> bool:
        """Signal cancellation for ``subagent_id``.

        Returns ``True`` iff a live flag was found and set (the sub-agent is
        in-flight and will stop after its current round), ``False`` when no
        sub-agent with that id is currently running.
        """
        record = self._records.get(subagent_id)
        if record is None:
            return False
        record.reason = "user_requested"
        record.event.set()
        return True

    def abort_by_owner_tab(
        self, owner_tab_id: str, *, reason: str = "user_requested"
    ) -> tuple[str, ...]:
        """Signal cancellation for EVERY sub-agent spawned by ``owner_tab_id``.

        Runtime-defect fix (the reported "主 Agent 停止后子 Agent 一直跑不停"
        bug): a Stop on a parent tab only signals the ``stream_abort_registry``
        handle keyed by ``tab_id`` — it does NOT touch this per-sub-agent
        registry, so an autonomously-spawned sub-agent's cooperative-abort
        event was never set and its round loop ran forever. This method lets
        :class:`~qai.chat.application.use_cases.streaming.StopChatUseCase`
        cascade the tab Stop into every in-flight sub-agent that tab spawned.

        Sets the event on every live (registered) record whose
        ``owner_tab_id`` matches — the sub-agent loops poll it between rounds
        and stop cooperatively (the in-flight round finishes, then
        :class:`KernelAborted` fires at the next round top). Returns the ids
        that were actually signalled (already-set records are skipped so the
        result reflects the REAL newly-aborted set — State-Truth-First).
        A record with ``owner_tab_id is None`` is never matched (it was
        registered without an owner and can only be aborted directly by id).
        """
        aborted: list[str] = []
        for subagent_id, record in self._records.items():
            if record.owner_tab_id != owner_tab_id:
                continue
            if record.event.is_set():
                continue
            record.reason = reason
            record.event.set()
            aborted.append(subagent_id)
        return tuple(aborted)

    def is_aborted(self, subagent_id: str) -> bool:
        """Return ``True`` iff a registered flag for ``subagent_id`` is set."""
        record = self._records.get(subagent_id)
        return record is not None and record.event.is_set()

    def is_running(self, subagent_id: str) -> bool:
        """Return ``True`` iff a flag is currently registered for the id."""
        return subagent_id in self._records

    def list_active(self) -> tuple[ActiveSubAgentSnapshot, ...]:
        """Return process-local running sub-agent flags in start order."""
        items = [
            ActiveSubAgentSnapshot(
                subagent_id=subagent_id,
                started_at=record.started_at,
                aborted=record.event.is_set(),
                reason=record.reason,
            )
            for subagent_id, record in self._records.items()
        ]
        return tuple(sorted(items, key=lambda item: item.started_at))
