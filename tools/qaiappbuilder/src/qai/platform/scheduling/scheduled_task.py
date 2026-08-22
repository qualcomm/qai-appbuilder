# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for the agent-facing scheduled-task feature.

A *scheduled task* is a self-contained instruction (``prompt``) that the
scheduler fires on a schedule (interval / one-shot / cron). It runs as one
isolated agent turn with no live user, and its result is delivered back to a
target conversation out-of-band.

This module holds the pure, I/O-free domain shapes only:

* :class:`ScheduleKind` — the three supported schedule flavours.
* :class:`Schedule` — an immutable parsed schedule (one of the three kinds).
* :class:`TaskState` — the lifecycle state of a task.
* :class:`ScheduledTask` — the aggregate persisted by the store.

Parsing (text → :class:`Schedule`) and next-run computation live in
``schedule_parser.py`` so this module stays dependency-free and trivially
testable. Timezone rule (S1 PR-012): every datetime here is timezone-aware
UTC; naive datetimes are a programming error.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "ScheduleKind",
    "Schedule",
    "TaskState",
    "ScheduledTask",
    "TaskRunRecord",
]


class ScheduleKind(str, Enum):
    """The three schedule flavours a task can carry.

    * ``ONCE`` — fire a single time at :attr:`Schedule.run_at`.
    * ``INTERVAL`` — fire every :attr:`Schedule.interval_seconds`.
    * ``CRON`` — fire on a 5-field cron expression (:attr:`Schedule.cron_expr`).
    """

    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class TaskState(str, Enum):
    """Lifecycle state of a scheduled task (State-Truth-First: this is the
    stored truth; whether the task is *currently executing* is tracked
    separately by the scheduler from the live asyncio task, never from here).
    """

    SCHEDULED = "scheduled"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Schedule:
    """An immutable, already-parsed schedule.

    Exactly one of the payload fields is meaningful per :attr:`kind`:

    * ``ONCE``     → :attr:`run_at` (tz-aware UTC).
    * ``INTERVAL`` → :attr:`interval_seconds` (``> 0``).
    * ``CRON``     → :attr:`cron_expr` (a validated 5-field expression).

    :attr:`display` is the original human text (e.g. ``"every 2h"``) kept for
    UI / tool echoes so we never have to re-render a schedule from its parts.

    Two optional fields (appended; both default ``None`` so every existing
    construction stays valid) carry the user's *wall-clock intent*, which the
    UTC-only payload above cannot express on its own:

    * :attr:`start_at` — the FIRST fire time of a recurring schedule (tz-aware
      UTC). Without it an ``INTERVAL`` task's first run is one full interval
      after creation, so "every 2h starting at 09:00" was unexpressible. The
      scheduler waits for ``start_at``, then steps by the interval.
    * :attr:`tz_offset_minutes` — the offset (east-of-UTC minutes, e.g. ``480``
      for +08:00) of the zone whose wall clock the user meant. Cron fields
      ("0 7 * * *") name a wall clock, not a UTC instant; anchoring them in UTC
      makes a "07:00 daily" task fire at 15:00 local in +08:00. Carrying the
      offset as DATA (never read from the server's local zone) keeps
      ``next_run_at`` pure and deterministic under test while still honouring
      the user's intent. ``None`` ⇒ legacy UTC anchoring (unchanged).
    """

    kind: ScheduleKind
    display: str
    run_at: datetime | None = None
    interval_seconds: float | None = None
    cron_expr: str | None = None
    start_at: datetime | None = None
    tz_offset_minutes: int | None = None

    def __post_init__(self) -> None:
        if self.kind is ScheduleKind.ONCE:
            if self.run_at is None:
                raise ValueError("ONCE schedule requires run_at")
            if self.run_at.tzinfo is None:
                raise ValueError("run_at must be timezone-aware (UTC)")
        elif self.kind is ScheduleKind.INTERVAL:
            if self.interval_seconds is None or self.interval_seconds <= 0:
                raise ValueError("INTERVAL schedule requires interval_seconds > 0")
        elif self.kind is ScheduleKind.CRON:
            if not self.cron_expr:
                raise ValueError("CRON schedule requires cron_expr")
        else:  # pragma: no cover - exhaustive
            raise ValueError(f"unknown schedule kind: {self.kind!r}")
        if self.start_at is not None and self.start_at.tzinfo is None:
            raise ValueError("start_at must be timezone-aware (UTC)")
        if self.tz_offset_minutes is not None and not (
            -1440 < self.tz_offset_minutes < 1440
        ):
            raise ValueError("tz_offset_minutes must be within ±24h")

    @property
    def recurring(self) -> bool:
        """True for schedules that fire more than once (interval / cron)."""
        return self.kind is not ScheduleKind.ONCE


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """The persisted aggregate for one scheduled task.

    ``task_id`` is the stable identity (also used as the tool-facing handle).
    ``repeat_times`` caps how many times a recurring task fires (``None`` =
    unbounded); ``completed_runs`` counts *attempted* fires — the scheduler
    increments it just before running (at-most-once accounting), so a crash
    mid-run still consumes the count rather than replaying it. ``version``
    powers the store's optimistic-concurrency CAS. All datetimes are tz-aware
    UTC.

    ``enabled_tools`` / ``enabled_skills`` are per-task *whitelists* (tuples so
    the aggregate stays frozen-hashable): when non-empty, the session-level
    runner restricts the unattended turn to exactly those tools / skills
    (everything else is disabled for that run). An EMPTY tuple means "no
    restriction" — the run uses the full default tool set and skill catalog
    (preserving the pre-whitelist behaviour). Names are opaque here; the
    runner maps them onto the chat turn's disable sets.

    ``conversation_id`` / ``tab_id`` are the delivery binding. Both ``None``
    marks a GLOBAL task (not tied to any chat the user had): its run is not
    delivered into a user conversation by binding — the executor provisions a
    dedicated system conversation for it and the result surfaces via the
    notification center + run records. A non-null pair is a conversation-bound
    task whose result folds into that conversation (the original behaviour).
    """

    task_id: str
    prompt: str
    schedule: Schedule
    conversation_id: str | None = None
    tab_id: str | None = None
    name: str = ""
    model_id: str | None = None
    repeat_times: int | None = None
    completed_runs: int = 0
    enabled: bool = True
    state: TaskState = TaskState.SCHEDULED
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str = ""
    last_error: str = ""
    version: int = 0
    enabled_tools: tuple[str, ...] = ()
    enabled_skills: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        """Human label for echoes: the caller-supplied name, else the id."""
        return self.name or self.task_id

    @property
    def is_global(self) -> bool:
        """True when the task is not bound to a user conversation (global).

        A global task carries no ``conversation_id`` / ``tab_id``; the executor
        runs it in a dedicated system conversation and surfaces the result via
        the notification center + run records instead of folding it into a
        user chat.
        """
        return not self.conversation_id or not self.tab_id

    @property
    def repeat_exhausted(self) -> bool:
        """True once a bounded task has fired its full ``repeat_times``."""
        return (
            self.repeat_times is not None
            and self.completed_runs >= self.repeat_times
        )

    def with_changes(self, **changes: object) -> "ScheduledTask":
        """Return a copy with the given field overrides (frozen-safe update)."""
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskRunRecord:
    """One recorded execution of a scheduled task (the run-history row).

    Appended on every fire (see the scheduler's outcome path). Backs the
    notification center's "open -> full result" and the run-records view, and
    is the durable output of a GLOBAL task (which has no bound conversation to
    fold its result into). ``conversation_id`` is the conversation the run
    executed in (the bound one, or a global task's dedicated system
    conversation), empty when none.
    """

    id: str
    task_id: str
    conversation_id: str = ""
    ok: bool = False
    status: str = ""
    result_text: str = ""
    ran_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    #: Notification read state (migration 075): ``None`` = UNREAD (the row is
    #: an unread notification bell item); a tz-aware datetime = the moment the
    #: user dismissed it. Defaults to ``None`` because every fresh fire is
    #: unread until the user acts on it — the SQLite empty-string sentinel is
    #: an implementation detail of the store and never leaks into the domain
    #: (see :class:`SqliteScheduledTaskStore` for the row↔object bridge).
    notified_at: datetime | None = None
