# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Schedule-text parsing and next-run computation (pure, I/O-free).

Two responsibilities, both side-effect free so they unit-test in isolation:

* :func:`parse_schedule` — turn a human schedule string into a
  :class:`~qai.platform.scheduling.scheduled_task.Schedule`. Recognised, in
  order:

  1. ``daily HH:MM[±HH:MM]``        → CRON    (e.g. ``"daily 07:30+08:00"``)
  2. ``weekly <dow> HH:MM[±HH:MM]`` → CRON    (e.g. ``"weekly mon 09:00+08:00"``)
  3. ``every <duration>``           → INTERVAL (e.g. ``"every 2h"``)
  4. a 5-field cron expression      → CRON    (e.g. ``"0 9 * * *"``)
  5. an ISO-8601 timestamp          → ONCE    (e.g. ``"2026-08-01T09:00:00Z"``)
  6. a bare duration                → ONCE at ``now + duration`` (``"30m"``)

  Forms 1-2 exist because a cron expression names a WALL CLOCK, not a UTC
  instant: a user asking for "07:30 every day" in +08:00 means 23:30 UTC. They
  carry the trailing offset into ``Schedule.tz_offset_minutes`` so
  :func:`next_run_at` can anchor the recurrence in the user's zone instead of
  silently firing 8 hours off. A bare offset-less ``daily 07:30`` (or a raw
  5-field cron) keeps the legacy UTC anchoring.

* :func:`next_run_at` — compute the next fire time for a schedule given the
  last successful run (``None`` if it has never run). A recurring schedule with
  ``Schedule.start_at`` in the future fires FIRST at ``start_at`` (so "every 2h
  starting 09:00" is expressible); afterwards it steps normally.

Timezone rule (S1 PR-012): every datetime crossing this boundary is tz-aware
UTC. A ``now`` is always injected by the caller (from the platform
:class:`~qai.platform.time.Clock`) so tests are deterministic and the due-check
and storage share one clock. The user's zone travels as DATA
(``tz_offset_minutes``) — never read from the server's local zone — so the same
schedule computes identically on any host.

Cron support depends on the third-party ``croniter`` package. It is imported
lazily and guarded: interval / one-shot schedules work with or without it; a
cron expression raises a clear, actionable error when it is absent, so a
deployment that never uses cron is never forced to install it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from qai.platform.time import ensure_aware_utc, from_iso8601

from .scheduled_task import Schedule, ScheduleKind

__all__ = [
    "ScheduleParseError",
    "CronUnavailableError",
    "parse_duration",
    "parse_schedule",
    "next_run_at",
    "MIN_INTERVAL_SECONDS",
    "ONESHOT_GRACE_SECONDS",
]

#: Floor on interval schedules — a sub-second interval would busy-spin the
#: scheduler and is never a real user intent.
MIN_INTERVAL_SECONDS: float = 1.0

#: A one-shot whose fire time was missed by up to this many seconds still
#: fires once (catch-up) instead of being dropped; older than this is stale.
ONESHOT_GRACE_SECONDS: float = 120.0

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_EVERY_RE = re.compile(r"^\s*every\s+(.+?)\s*$", re.IGNORECASE)
# A cron field is digits, ``*``, or the ``,-/`` combinators — nothing else.
_CRON_FIELD_RE = re.compile(r"^[\d*/,\-]+$")
# ``daily 07:30`` / ``daily 07:30+08:00`` — the offset is optional; when absent
# the wall clock is taken as UTC (legacy behaviour).
_DAILY_RE = re.compile(
    r"^\s*daily\s+(\d{1,2}):(\d{2})\s*(Z|[+-]\d{2}:?\d{2})?\s*$",
    re.IGNORECASE,
)
# ``weekly mon 09:00+08:00`` — day names and 0-6 / 7 (Sunday) both accepted.
_WEEKLY_RE = re.compile(
    r"^\s*weekly\s+([A-Za-z]{3,9}|[0-7])\s+(\d{1,2}):(\d{2})\s*"
    r"(Z|[+-]\d{2}:?\d{2})?\s*$",
    re.IGNORECASE,
)
# ``±HH:MM`` / ``±HHMM`` offset payload of the two forms above.
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")

_DURATION_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}

#: Day-of-week names → cron's 0-6 (Sunday = 0), accepting 3-letter and full
#: spellings. ``7`` is also accepted on input and folded to ``0``.
_DOW_NAMES: dict[str, int] = {
    "sun": 0, "sunday": 0,
    "mon": 1, "monday": 1,
    "tue": 2, "tues": 2, "tuesday": 2,
    "wed": 3, "weds": 3, "wednesday": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
}

_USAGE = (
    "Expected one of: a duration ('30m', '2h', '1d'), an 'every <duration>' "
    "phrase ('every 2h'), 'daily HH:MM[+ZZ:ZZ]' ('daily 07:30+08:00'), "
    "'weekly <dow> HH:MM[+ZZ:ZZ]' ('weekly mon 09:00+08:00'), a 5-field cron "
    "expression ('0 9 * * *'), or an ISO-8601 timestamp "
    "('2026-08-01T09:00:00Z')."
)


class ScheduleParseError(ValueError):
    """Raised when a schedule string cannot be parsed into a :class:`Schedule`."""


class CronUnavailableError(RuntimeError):
    """Raised when a cron schedule is used but ``croniter`` is not installed."""


def parse_duration(text: str) -> int:
    """Parse a bare duration like ``"30m"`` / ``"2h"`` / ``"1d"`` into seconds.

    Units: ``s`` (seconds), ``m`` (minutes), ``h`` (hours), ``d`` (days).

    Raises:
        ScheduleParseError: if ``text`` is not a ``<int><unit>`` duration.
    """
    match = _DURATION_RE.match(text or "")
    if match is None:
        raise ScheduleParseError(f"invalid duration: {text!r}. {_USAGE}")
    value = int(match.group(1))
    unit = match.group(2).lower()
    if value <= 0:
        raise ScheduleParseError(f"duration must be positive: {text!r}")
    return value * _DURATION_UNIT_SECONDS[unit]


def _looks_like_cron(text: str) -> bool:
    """True iff ``text`` is a whitespace-separated 5-field cron expression."""
    fields = text.split()
    if len(fields) != 5:
        return False
    return all(_CRON_FIELD_RE.match(f) is not None for f in fields)


def _parse_offset(raw: str | None) -> int | None:
    """Parse a trailing ``Z`` / ``±HH:MM`` / ``±HHMM`` into east-of-UTC minutes.

    ``None`` (no offset given) returns ``None`` so the caller can keep the
    legacy UTC anchoring; an explicit ``Z`` returns ``0`` (UTC stated on
    purpose). Raises :class:`ScheduleParseError` on a malformed offset.
    """
    if raw is None:
        return None
    if raw.upper() == "Z":
        return 0
    m = _OFFSET_RE.match(raw)
    if m is None:
        raise ScheduleParseError(f"invalid UTC offset: {raw!r}")
    sign = -1 if m.group(1) == "-" else 1
    hours, minutes = int(m.group(2)), int(m.group(3))
    if hours > 23 or minutes > 59:
        raise ScheduleParseError(f"invalid UTC offset: {raw!r}")
    return sign * (hours * 60 + minutes)


def _parse_hh_mm(hh: str, mm: str, *, text: str) -> tuple[int, int]:
    """Validate an ``HH``/``MM`` pair from a daily/weekly form."""
    hour, minute = int(hh), int(mm)
    if hour > 23 or minute > 59:
        raise ScheduleParseError(f"invalid time of day in {text!r} (use HH:MM)")
    return hour, minute


def _parse_dow(raw: str, *, text: str) -> int:
    """Map a weekday token (name or ``0``-``7``) to cron's 0-6, Sunday = 0."""
    token = raw.lower()
    if token.isdigit():
        value = int(token)
        if value > 7:
            raise ScheduleParseError(f"invalid weekday in {text!r}: {raw!r}")
        return 0 if value == 7 else value
    dow = _DOW_NAMES.get(token)
    if dow is None:
        raise ScheduleParseError(f"invalid weekday in {text!r}: {raw!r}")
    return dow


def _load_croniter() -> type:
    """Return the ``croniter`` class, or raise :class:`CronUnavailableError`."""
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise CronUnavailableError(
            "cron expressions require the 'croniter' package; install it with "
            "`pip install croniter` (interval and one-shot schedules work "
            "without it)."
        ) from exc
    return croniter


def parse_schedule(
    text: str, *, now: datetime, start_at: datetime | None = None
) -> Schedule:
    """Parse a human schedule string into a :class:`Schedule`.

    Args:
        text: the schedule string (see module docstring for the grammar).
        now: current tz-aware UTC time (injected from the platform Clock);
            used only to anchor a bare-duration one-shot at ``now + duration``.
        start_at: optional FIRST fire time (tz-aware UTC) for a recurring
            schedule — lets "every 2h, first run at 09:00" be expressed. Ignored
            for a ONCE schedule (whose ``run_at`` already IS its only fire).

    Raises:
        ScheduleParseError: if ``text`` matches none of the supported forms.
        CronUnavailableError: if ``text`` is a cron expression but ``croniter``
            is not installed.
    """
    now = ensure_aware_utc(now)
    raw = (text or "").strip()
    if not raw:
        raise ScheduleParseError(f"empty schedule. {_USAGE}")
    first = ensure_aware_utc(start_at) if start_at is not None else None

    # 1. "daily HH:MM[±ZZ:ZZ]" → cron anchored in the stated zone
    daily = _DAILY_RE.match(raw)
    if daily is not None:
        hour, minute = _parse_hh_mm(daily.group(1), daily.group(2), text=raw)
        offset = _parse_offset(daily.group(3))
        expr = f"{minute} {hour} * * *"
        _load_croniter()  # fail fast + actionably when croniter is absent
        return Schedule(
            kind=ScheduleKind.CRON,
            display=raw,
            cron_expr=expr,
            start_at=first,
            tz_offset_minutes=offset,
        )

    # 2. "weekly <dow> HH:MM[±ZZ:ZZ]" → cron anchored in the stated zone
    weekly = _WEEKLY_RE.match(raw)
    if weekly is not None:
        dow = _parse_dow(weekly.group(1), text=raw)
        hour, minute = _parse_hh_mm(weekly.group(2), weekly.group(3), text=raw)
        offset = _parse_offset(weekly.group(4))
        expr = f"{minute} {hour} * * {dow}"
        _load_croniter()
        return Schedule(
            kind=ScheduleKind.CRON,
            display=raw,
            cron_expr=expr,
            start_at=first,
            tz_offset_minutes=offset,
        )

    # 3. "every <duration>" → interval
    every = _EVERY_RE.match(raw)
    if every is not None:
        seconds = parse_duration(every.group(1))
        if seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleParseError(
                f"interval too small (< {MIN_INTERVAL_SECONDS}s): {raw!r}"
            )
        return Schedule(
            kind=ScheduleKind.INTERVAL,
            display=raw,
            interval_seconds=float(seconds),
            start_at=first,
        )

    # 4. 5-field cron expression → cron (validated via croniter). A raw cron
    #    carries no zone, so it keeps the legacy UTC anchoring unless the caller
    #    used the daily/weekly forms above.
    if _looks_like_cron(raw):
        croniter = _load_croniter()
        if not croniter.is_valid(raw):
            raise ScheduleParseError(f"invalid cron expression: {raw!r}")
        return Schedule(
            kind=ScheduleKind.CRON,
            display=raw,
            cron_expr=raw,
            start_at=first,
        )

    # 5. ISO-8601 timestamp → one-shot at that instant
    #    (heuristic: an ISO datetime contains 'T' or looks like YYYY-MM-DD;
    #    from_iso8601 rejects naive inputs, so a caller must pass an offset.)
    if "T" in raw or re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        try:
            run_at = from_iso8601(raw)
        except ValueError as exc:
            raise ScheduleParseError(str(exc)) from exc
        return Schedule(kind=ScheduleKind.ONCE, display=raw, run_at=run_at)

    # 6. bare duration → one-shot at now + duration
    seconds = parse_duration(raw)
    return Schedule(
        kind=ScheduleKind.ONCE,
        display=raw,
        run_at=now + timedelta(seconds=seconds),
    )


def next_run_at(
    schedule: Schedule,
    *,
    now: datetime,
    last_run_at: datetime | None = None,
) -> datetime | None:
    """Compute the next fire time for ``schedule``.

    Args:
        schedule: the parsed schedule.
        now: current tz-aware UTC time (from the platform Clock).
        last_run_at: the last successful fire, or ``None`` if never run.

    Returns:
        The next tz-aware UTC fire time, or ``None`` when the schedule has no
        further runs (a one-shot that already fired, or a missed one-shot past
        its grace window).

    Notes:
        * ONCE — returns ``run_at`` only if it has never run and is not stale
          beyond :data:`ONESHOT_GRACE_SECONDS`; otherwise ``None``.
        * INTERVAL — ``last_run_at + interval`` if it has run, else
          ``start_at`` when one is set and still ahead, else ``now + interval``
          (a first run one full interval out).
        * CRON — ``croniter(expr, base).get_next()`` where ``base`` is the last
          run when available (anchors recurrence on the real previous fire, so
          a restart does not shift the phase to the restart instant). When
          ``schedule.tz_offset_minutes`` is set, the base is converted to THAT
          zone first so the cron fields name the user's wall clock (a "07:30
          daily" in +08:00 fires 23:30 UTC, not 07:30 UTC); the result is
          normalized back to UTC.
        * A recurring schedule whose ``start_at`` is still in the future fires
          FIRST at ``start_at`` — the user's explicit "begin at" wins over the
          computed cadence, and only later runs follow the interval / cron.
    """
    now = ensure_aware_utc(now)

    if schedule.kind is ScheduleKind.ONCE:
        assert schedule.run_at is not None  # invariant from Schedule
        if last_run_at is not None:
            return None  # a one-shot fires at most once
        run_at = ensure_aware_utc(schedule.run_at)
        if run_at >= now:
            return run_at
        # Missed: fire once if still within the grace window, else drop.
        if (now - run_at).total_seconds() <= ONESHOT_GRACE_SECONDS:
            return run_at
        return None

    # Recurring kinds: an explicit, not-yet-reached first fire time wins.
    if last_run_at is None and schedule.start_at is not None:
        start = ensure_aware_utc(schedule.start_at)
        if start >= now:
            return start

    if schedule.kind is ScheduleKind.INTERVAL:
        assert schedule.interval_seconds is not None  # invariant
        step = timedelta(seconds=schedule.interval_seconds)
        if last_run_at is not None:
            return ensure_aware_utc(last_run_at) + step
        return now + step

    if schedule.kind is ScheduleKind.CRON:
        assert schedule.cron_expr is not None  # invariant
        croniter = _load_croniter()
        base = ensure_aware_utc(last_run_at) if last_run_at is not None else now
        if schedule.tz_offset_minutes is not None:
            # Cron fields name a wall clock in the user's zone: evaluate there.
            zone = timezone(timedelta(minutes=schedule.tz_offset_minutes))
            base = base.astimezone(zone)
        itr = croniter(schedule.cron_expr, base)
        nxt = itr.get_next(datetime)
        return ensure_aware_utc(nxt)

    raise ScheduleParseError(  # pragma: no cover - exhaustive
        f"unknown schedule kind: {schedule.kind!r}"
    )
