# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Per-engine health scoring for the independent web-search aggregator.

A bounded integer counter (``-50..+50``, initial ``0``) tracks each engine's
recent reliability so a persistently-failing engine drops out of the fallback
chain instead of slowing every search. The state machine (plan
web-search-independent-engine-integration §12.10) runs opportunistically inside
the aggregator's per-engine ``finally`` — no background probing, no timers.

Rules (§12.2-§12.5, §12.8):

* success ``+1`` / failure ``-1``, clamped to ``[score_min, score_max]``;
* a 5-minute dedup window suppresses repeat events from the same engine so a
  fan-out or a manual retry burst cannot flood the score;
* a daily ``±cap`` bounds one calendar day's total movement;
* ``consecutive_fails_hard`` straight failures snap the score to
  ``min(score, disable_threshold)`` immediately, bypassing dedup and the daily
  cap (the only rule that may break the daily bound);
* any success zeroes the consecutive-failure counter;
* a local clock that jumps *backwards* (stored ``today_date`` is later than
  today) zeroes the consecutive-failure counter and restarts the daily count.

Cumulative ``total_calls`` / ``total_successes`` are always incremented (they
feed the settings-panel success-rate readout) and are never gated by the dedup
window or the daily cap.

Persistence is the shared async SQLite :class:`~qai.platform.persistence.Database`
(table ``search_engine_score``, migration 063). A first-touch engine is created
lazily via ``INSERT OR IGNORE`` and thereafter updated in place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from qai.platform.persistence import Database

__all__ = [
    "MANUAL_AUTO",
    "MANUAL_FORCED_OFF",
    "MANUAL_FORCED_ON",
    "EngineScore",
    "ScoreStore",
    "ScoringConfig",
]

_log = get_logger(__name__)

#: ``manual_state`` values (plan §12.7). ``auto`` defers to the score,
#: ``forced_on`` always participates, ``forced_off`` never participates.
MANUAL_AUTO = "auto"
MANUAL_FORCED_ON = "forced_on"
MANUAL_FORCED_OFF = "forced_off"
_MANUAL_STATES = frozenset({MANUAL_AUTO, MANUAL_FORCED_ON, MANUAL_FORCED_OFF})

#: Plan §12.12 defaults — used verbatim when ``search_config.toml`` supplies no
#: ``[independent_search.scoring]`` section or a value falls outside its range.
_DEFAULT_SCORE_MAX = 50
_DEFAULT_SCORE_MIN = -50
_DEFAULT_INITIAL_SCORE = 0
_DEFAULT_DISABLE_THRESHOLD = -25
_DEFAULT_DAILY_CHANGE_CAP = 3
_DEFAULT_DEDUP_WINDOW_SECONDS = 300
_DEFAULT_CONSECUTIVE_FAILS_HARD = 20

#: Validation ranges for the two footgun parameters (plan §15.6). A value
#: outside these bounds reverts to the default and logs a warning.
_DISABLE_THRESHOLD_MIN = -50
_DISABLE_THRESHOLD_MAX = -1
_CONSECUTIVE_FAILS_HARD_MIN = 5
_CONSECUTIVE_FAILS_HARD_MAX = 100


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoringConfig:
    """Validated ``[search.scoring]`` parameters.

    Built via :meth:`from_mapping`, which clamps the footgun parameters to
    their safe ranges (§15.6) and falls back to the §12.12 defaults for any
    missing / malformed entry.
    """

    consecutive_fails_hard: int = _DEFAULT_CONSECUTIVE_FAILS_HARD
    daily_change_cap: int = _DEFAULT_DAILY_CHANGE_CAP
    dedup_window_seconds: int = _DEFAULT_DEDUP_WINDOW_SECONDS
    disable_threshold: int = _DEFAULT_DISABLE_THRESHOLD
    initial_score: int = _DEFAULT_INITIAL_SCORE
    score_max: int = _DEFAULT_SCORE_MAX
    score_min: int = _DEFAULT_SCORE_MIN

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> ScoringConfig:
        """Return a config from a ``[search.scoring]`` mapping, defaults for gaps."""
        data = raw if isinstance(raw, dict) else {}
        disable_threshold = _int_in_range(
            data.get("disable_threshold"),
            default=_DEFAULT_DISABLE_THRESHOLD,
            low=_DISABLE_THRESHOLD_MIN,
            high=_DISABLE_THRESHOLD_MAX,
            name="disable_threshold",
        )
        consecutive_fails_hard = _int_in_range(
            data.get("consecutive_fails_hard"),
            default=_DEFAULT_CONSECUTIVE_FAILS_HARD,
            low=_CONSECUTIVE_FAILS_HARD_MIN,
            high=_CONSECUTIVE_FAILS_HARD_MAX,
            name="consecutive_fails_hard",
        )
        return cls(
            consecutive_fails_hard=consecutive_fails_hard,
            daily_change_cap=_int_or_default(
                data.get("daily_change_cap"), _DEFAULT_DAILY_CHANGE_CAP
            ),
            dedup_window_seconds=_int_or_default(
                data.get("dedup_window_seconds"), _DEFAULT_DEDUP_WINDOW_SECONDS
            ),
            disable_threshold=disable_threshold,
            initial_score=_int_or_default(
                data.get("initial_score"), _DEFAULT_INITIAL_SCORE
            ),
            score_max=_int_or_default(data.get("score_max"), _DEFAULT_SCORE_MAX),
            score_min=_int_or_default(data.get("score_min"), _DEFAULT_SCORE_MIN),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineScore:
    """A snapshot of one engine's scoring row."""

    consecutive_fails: int
    engine_id: str
    last_recorded_ts: int
    manual_state: str
    score: int
    today_count: int
    today_date: str
    total_calls: int
    total_successes: int
    updated_at: int

    @property
    def success_rate(self) -> float:
        """Cumulative success ratio in ``[0.0, 1.0]`` (``0.0`` with no calls)."""
        if self.total_calls <= 0:
            return 0.0
        return self.total_successes / self.total_calls


class ScoreStore:
    """Read-modify-write access to ``search_engine_score`` (migration 063).

    Wraps the shared async :class:`~qai.platform.persistence.Database`; every
    method leases one connection via ``db.connection()`` and commits its own
    writes, mirroring the platform repository convention. The ``config`` is
    resolved once from ``[search.scoring]`` (or plan defaults) unless injected.
    """

    __slots__ = ("_cfg", "_db", "_default_off")

    def __init__(
        self,
        db: Database,
        *,
        config: ScoringConfig | None = None,
        default_off_engines: frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._cfg = config if config is not None else _load_scoring_config()
        # Engine ids whose spec says ``enabled_by_default = false``. Without
        # this the flag was dead config: ``_initial()`` seeded every engine as
        # ``auto`` and ``_is_enabled`` only looks at the score, so a
        # config-level "off" never reached the fallback chain. Seeding
        # ``forced_off`` makes the flag mean what it says while keeping the
        # user's explicit toggle authoritative (once a row exists, the stored
        # ``manual_state`` wins — this default only applies to engines the
        # user has never touched).
        self._default_off = default_off_engines or frozenset()

    @property
    def config(self) -> ScoringConfig:
        return self._cfg

    async def record_outcome(
        self,
        engine_id: str,
        success: bool,
        outcome_type: str | None = None,
    ) -> EngineScore:
        """Fold one search outcome into the engine's score (§12.10).

        ``outcome_type`` is accepted for call-site symmetry with the error
        taxonomy but is intentionally not weighted — every failure counts the
        same (§12.2). Returns the post-update snapshot.
        """
        del outcome_type  # not weighted; see docstring
        cfg = self._cfg
        now = int(time.time())
        today = _local_today_iso()
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    s = await self._load_locked(conn, engine_id)
                    updated = _apply_outcome(s, success=success, cfg=cfg, now=now, today=today)
                    await self._write(conn, updated)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                return updated
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError(
                "persistence.search_score_record",
                f"Failed to record outcome for engine {engine_id!r}",
                operation="record_outcome",
                cause=exc,
            ) from exc

    async def get_score(self, engine_id: str) -> int:
        """Return the engine's current integer score (initial score if unseen)."""
        return (await self.get_state(engine_id)).score

    async def get_state(self, engine_id: str) -> EngineScore:
        """Return the engine's full snapshot (a synthetic initial row if unseen)."""
        try:
            async with self._db.connection() as conn:
                return await self._load_locked(conn, engine_id)
        except Exception as exc:
            raise PersistenceError(
                "persistence.search_score_read",
                f"Failed to read score for engine {engine_id!r}",
                operation="get_state",
                cause=exc,
            ) from exc

    async def set_manual_state(self, engine_id: str, state: str) -> EngineScore:
        """Set the manual override (``auto`` / ``forced_on`` / ``forced_off``)."""
        if state not in _MANUAL_STATES:
            raise ValueError(
                f"manual_state must be one of {sorted(_MANUAL_STATES)}, got {state!r}"
            )
        now = int(time.time())
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    s = await self._load_locked(conn, engine_id)
                    updated = _replace(s, manual_state=state, updated_at=now)
                    await self._write(conn, updated)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                return updated
        except Exception as exc:
            raise PersistenceError(
                "persistence.search_score_manual",
                f"Failed to set manual state for engine {engine_id!r}",
                operation="set_manual_state",
                cause=exc,
            ) from exc

    async def reset_score(self, engine_id: str) -> EngineScore:
        """Zero ``score`` / ``consecutive_fails`` / ``today_count`` (§12.11).

        Leaves ``manual_state`` and the cumulative totals untouched.
        """
        now = int(time.time())
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    s = await self._load_locked(conn, engine_id)
                    updated = _replace(
                        s,
                        score=self._cfg.initial_score,
                        consecutive_fails=0,
                        today_count=0,
                        updated_at=now,
                    )
                    await self._write(conn, updated)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                return updated
        except Exception as exc:
            raise PersistenceError(
                "persistence.search_score_reset",
                f"Failed to reset score for engine {engine_id!r}",
                operation="reset_score",
                cause=exc,
            ) from exc

    async def is_enabled(self, engine_id: str) -> bool:
        """Whether the engine participates in the fallback chain (§12.10).

        ``forced_on`` always yes, ``forced_off`` always no; otherwise the score
        must sit strictly above ``disable_threshold``.
        """
        s = await self.get_state(engine_id)
        return _is_enabled(s, self._cfg)

    async def sort_key(self, engine_id: str, priority_hint: int) -> tuple[int, int]:
        """Sort key ``(-score, -priority_hint)`` (§12.10).

        Higher score sorts first; ties (notably the all-zero first search) fall
        back to the spec's ``priority_hint`` so the intended order still holds.
        """
        s = await self.get_state(engine_id)
        return (-s.score, -priority_hint)

    async def _load_locked(self, conn: Any, engine_id: str) -> EngineScore:
        cur = await conn.execute(
            "SELECT score, consecutive_fails, last_recorded_ts, today_date, "
            "today_count, manual_state, total_calls, total_successes, updated_at "
            "FROM search_engine_score WHERE engine_id = ?",
            (engine_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return self._initial(engine_id)
        return EngineScore(
            engine_id=engine_id,
            score=int(row[0]),
            consecutive_fails=int(row[1]),
            last_recorded_ts=int(row[2]),
            today_date=str(row[3]),
            today_count=int(row[4]),
            manual_state=str(row[5]),
            total_calls=int(row[6]),
            total_successes=int(row[7]),
            updated_at=int(row[8]),
        )

    async def _write(self, conn: Any, s: EngineScore) -> None:
        await conn.execute(
            "INSERT INTO search_engine_score ("
            "engine_id, score, consecutive_fails, last_recorded_ts, today_date, "
            "today_count, manual_state, total_calls, total_successes, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(engine_id) DO UPDATE SET "
            "score = excluded.score, "
            "consecutive_fails = excluded.consecutive_fails, "
            "last_recorded_ts = excluded.last_recorded_ts, "
            "today_date = excluded.today_date, "
            "today_count = excluded.today_count, "
            "manual_state = excluded.manual_state, "
            "total_calls = excluded.total_calls, "
            "total_successes = excluded.total_successes, "
            "updated_at = excluded.updated_at",
            (
                s.engine_id,
                s.score,
                s.consecutive_fails,
                s.last_recorded_ts,
                s.today_date,
                s.today_count,
                s.manual_state,
                s.total_calls,
                s.total_successes,
                s.updated_at,
            ),
        )

    def _initial(self, engine_id: str) -> EngineScore:
        return EngineScore(
            engine_id=engine_id,
            score=self._cfg.initial_score,
            consecutive_fails=0,
            last_recorded_ts=0,
            today_date="",
            today_count=0,
            manual_state=(
                MANUAL_FORCED_OFF
                if engine_id in self._default_off
                else MANUAL_AUTO
            ),
            total_calls=0,
            total_successes=0,
            updated_at=0,
        )


def _is_enabled(s: EngineScore, cfg: ScoringConfig) -> bool:
    if s.manual_state == MANUAL_FORCED_ON:
        return True
    if s.manual_state == MANUAL_FORCED_OFF:
        return False
    return s.score > cfg.disable_threshold


def _apply_outcome(
    s: EngineScore,
    *,
    success: bool,
    cfg: ScoringConfig,
    now: int,
    today: str,
) -> EngineScore:
    """Pure fold of the §12.10 state machine over one snapshot."""
    total_calls = s.total_calls + 1
    total_successes = s.total_successes + (1 if success else 0)

    today_date = s.today_date
    today_count = s.today_count
    # §12.8 time-travel: a stored date LATER than today means the local clock
    # rolled back — restart the day AND clear the consecutive-fail streak so a
    # backward jump cannot leave a stale count that trips the hard-disable rule.
    time_reversed = bool(s.today_date) and s.today_date > today
    if today_date != today:
        today_date = today
        today_count = 0
    prior_consecutive = 0 if time_reversed else s.consecutive_fails
    consecutive_fails = 0 if success else prior_consecutive + 1
    score = s.score

    base = _replace(
        s,
        today_date=today_date,
        today_count=today_count,
        consecutive_fails=consecutive_fails,
        total_calls=total_calls,
        total_successes=total_successes,
        updated_at=now,
    )

    # Hard disable: N straight failures snap the score down, bypassing dedup
    # and the daily cap. This is the only rule allowed to break the daily bound.
    if not success and consecutive_fails >= cfg.consecutive_fails_hard:
        return _replace(base, score=min(score, cfg.disable_threshold))

    # 5-minute dedup: repeat events inside the window do not move the score.
    if now - s.last_recorded_ts < cfg.dedup_window_seconds:
        return base

    # Daily cap: once the day's quota is spent the score stays put.
    if today_count >= cfg.daily_change_cap:
        return base

    score = min(score + 1, cfg.score_max) if success else max(score - 1, cfg.score_min)
    return _replace(
        base, score=score, last_recorded_ts=now, today_count=today_count + 1
    )


def _replace(s: EngineScore, **changes: object) -> EngineScore:
    fields: dict[str, object] = {
        "engine_id": s.engine_id,
        "score": s.score,
        "consecutive_fails": s.consecutive_fails,
        "last_recorded_ts": s.last_recorded_ts,
        "today_date": s.today_date,
        "today_count": s.today_count,
        "manual_state": s.manual_state,
        "total_calls": s.total_calls,
        "total_successes": s.total_successes,
        "updated_at": s.updated_at,
    }
    fields.update(changes)
    return EngineScore(**fields)  # type: ignore[arg-type]


def _load_scoring_config() -> ScoringConfig:
    """Resolve ``[independent_search.scoring]`` via the shared-kernel config.

    The typed accessor ``get_independent_search_config`` is owned by
    ``qai.platform.web_search.config`` (roster shipped to both editions). A
    missing accessor / section degrades to the §12.12 defaults.
    """
    raw: object = None
    try:
        from qai.platform.web_search.config import (
            get_independent_search_config,
        )

        common = get_independent_search_config()
        if isinstance(common, dict):
            raw = common.get("scoring")
    except (ImportError, AttributeError):
        raw = None
    return ScoringConfig.from_mapping(raw if isinstance(raw, dict) else None)


def _int_or_default(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _int_in_range(
    value: object,
    *,
    default: int,
    low: int,
    high: int,
    name: str,
) -> int:
    resolved = _int_or_default(value, default)
    if low <= resolved <= high:
        return resolved
    _log.warning(
        "search.scoring.param_out_of_range",
        param=name,
        value=resolved,
        low=low,
        high=high,
        fallback=default,
    )
    return default


def _local_today_iso() -> str:
    """Return today's date in ISO ``YYYY-MM-DD`` form, in the process local zone.

    Plan §12.8's time-travel detection ("stored ``today_date`` is later than
    today") is defined against the user's *local* calendar, so this helper
    goes through ``astimezone()`` (which attaches the platform zoneinfo) rather
    than the naive ``date.today()`` that ruff's DTZ011 forbids.
    """
    return datetime.now().astimezone().date().isoformat()
