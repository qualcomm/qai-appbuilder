# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Per-engine monthly usage quota tracking for keyed (paid) search engines.

Provides :class:`QuotaStore` for atomic increment + threshold-check semantics.
The store is backed by the shared SQLite ``search_engine_quota`` table
(migration 071).

Design
------
- Each keyed engine gets a configurable monthly cap (default 1000 — matching
  typical free-tier allowances).
- Every successful search call increments the counter.
- Threshold warnings are emitted at regular intervals:
  * Every 100 uses while usage < 900
  * Every 20 uses once usage >= 900
- Users can disable notifications per-engine.
- When the quota is exhausted, the engine is skipped (falls back to keyless).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qai.platform.persistence import Database

__all__ = ["QuotaInfo", "QuotaStore", "QuotaWarning"]

_log = logging.getLogger(__name__)

_DEFAULT_MONTHLY_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class QuotaInfo:
    """Snapshot of one engine's current-month quota state."""

    engine_id: str
    month_key: str
    usage_count: int
    monthly_limit: int
    notify_enabled: bool
    remaining: int
    exhausted: bool


@dataclass(frozen=True, slots=True)
class QuotaWarning:
    """A threshold warning to surface to the frontend.

    ``kind`` is one of:
    - ``"threshold"`` — periodic usage milestone reached
    - ``"exhausted"`` — quota fully used up, engine will be skipped
    """

    engine_id: str
    kind: str  # "threshold" | "exhausted"
    usage_count: int
    monthly_limit: int
    message: str


def _current_month_key() -> str:
    """Return the current month as ``YYYY-MM``."""
    return datetime.now(UTC).strftime("%Y-%m")


# Notification thresholds: warn every N uses. Once past the "urgent" boundary,
# the interval tightens to catch rapidly-approaching exhaustion.
_WARN_INTERVAL_NORMAL = 100
_WARN_INTERVAL_URGENT = 20
_URGENT_THRESHOLD = 900

def _should_warn(usage: int, limit: int) -> bool:
    """Whether the current usage count triggers a threshold warning.

    Rules:
    - Every 100 uses while usage < 900
    - Every 20 uses once usage >= 900
    - At exactly the limit (exhausted)
    """
    if usage >= limit:
        return True
    if usage >= _URGENT_THRESHOLD:
        return usage % _WARN_INTERVAL_URGENT == 0
    return usage % _WARN_INTERVAL_NORMAL == 0 and usage > 0


class QuotaStore:
    """Read-modify-write access to ``search_engine_quota`` (migration 071).

    Thread-safe via SQLite's own locking (each operation uses an isolated
    connection from the pool).
    """

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def increment(
        self, engine_id: str, *, monthly_limit: int | None = None
    ) -> tuple[QuotaInfo, QuotaWarning | None]:
        """Atomically increment usage and return current state + optional warning.

        If no row exists for the current month, one is created with the given
        (or default) ``monthly_limit``.  Returns a :class:`QuotaWarning` when
        the post-increment count hits a notification threshold.
        """
        month = _current_month_key()
        limit = monthly_limit if monthly_limit is not None else _DEFAULT_MONTHLY_LIMIT

        async with self._db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                # Upsert: create if missing, otherwise just increment.
                await conn.execute(
                    """
                    INSERT INTO search_engine_quota
                        (engine_id, month_key, usage_count, monthly_limit, notify_enabled, updated_at)
                    VALUES (?, ?, 1, ?, 1, datetime('now'))
                    ON CONFLICT(engine_id, month_key) DO UPDATE SET
                        usage_count = usage_count + 1,
                        updated_at = datetime('now')
                    """,
                    (engine_id, month, limit),
                )
                row = await conn.execute(
                    """
                    SELECT usage_count, monthly_limit, notify_enabled
                    FROM search_engine_quota
                    WHERE engine_id = ? AND month_key = ?
                    """,
                    (engine_id, month),
                )
                result = await row.fetchone()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        usage_count = result[0]
        actual_limit = result[1]
        notify_enabled = bool(result[2])
        remaining = max(0, actual_limit - usage_count)
        exhausted = usage_count >= actual_limit

        info = QuotaInfo(
            engine_id=engine_id,
            month_key=month,
            usage_count=usage_count,
            monthly_limit=actual_limit,
            notify_enabled=notify_enabled,
            remaining=remaining,
            exhausted=exhausted,
        )

        warning: QuotaWarning | None = None
        if notify_enabled and _should_warn(usage_count, actual_limit):
            if exhausted:
                warning = QuotaWarning(
                    engine_id=engine_id,
                    kind="exhausted",
                    usage_count=usage_count,
                    monthly_limit=actual_limit,
                    message=f"Engine '{engine_id}' has reached its monthly quota ({usage_count}/{actual_limit}).",
                )
            else:
                warning = QuotaWarning(
                    engine_id=engine_id,
                    kind="threshold",
                    usage_count=usage_count,
                    monthly_limit=actual_limit,
                    message=f"Engine '{engine_id}' usage: {usage_count}/{actual_limit} this month.",
                )

        return info, warning

    async def is_exhausted(self, engine_id: str) -> bool:
        """Quick check: has this engine hit its monthly limit?"""
        month = _current_month_key()
        try:
            async with self._db.connection() as conn:
                row = await conn.execute(
                    """
                    SELECT usage_count, monthly_limit
                    FROM search_engine_quota
                    WHERE engine_id = ? AND month_key = ?
                    """,
                    (engine_id, month),
                )
                result = await row.fetchone()
        except Exception:  # noqa: BLE001
            return False  # On error, allow usage (fail open)
        if result is None:
            return False
        return result[0] >= result[1]

    async def get_quota(self, engine_id: str) -> QuotaInfo:
        """Return the current month's quota info for one engine."""
        month = _current_month_key()
        try:
            async with self._db.connection() as conn:
                row = await conn.execute(
                    """
                    SELECT usage_count, monthly_limit, notify_enabled
                    FROM search_engine_quota
                    WHERE engine_id = ? AND month_key = ?
                    """,
                    (engine_id, month),
                )
                result = await row.fetchone()
        except Exception:  # noqa: BLE001
            return QuotaInfo(
                engine_id=engine_id,
                month_key=month,
                usage_count=0,
                monthly_limit=_DEFAULT_MONTHLY_LIMIT,
                notify_enabled=True,
                remaining=_DEFAULT_MONTHLY_LIMIT,
                exhausted=False,
            )
        if result is None:
            return QuotaInfo(
                engine_id=engine_id,
                month_key=month,
                usage_count=0,
                monthly_limit=_DEFAULT_MONTHLY_LIMIT,
                notify_enabled=True,
                remaining=_DEFAULT_MONTHLY_LIMIT,
                exhausted=False,
            )
        usage = result[0]
        limit = result[1]
        notify = bool(result[2])
        return QuotaInfo(
            engine_id=engine_id,
            month_key=month,
            usage_count=usage,
            monthly_limit=limit,
            notify_enabled=notify,
            remaining=max(0, limit - usage),
            exhausted=usage >= limit,
        )

    async def get_all_quotas(self) -> list[QuotaInfo]:
        """Return quota info for all engines with usage this month."""
        month = _current_month_key()
        try:
            async with self._db.connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT engine_id, usage_count, monthly_limit, notify_enabled
                    FROM search_engine_quota
                    WHERE month_key = ?
                    ORDER BY engine_id
                    """,
                    (month,),
                )
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [
            QuotaInfo(
                engine_id=row[0],
                month_key=month,
                usage_count=row[1],
                monthly_limit=row[2],
                notify_enabled=bool(row[3]),
                remaining=max(0, row[2] - row[1]),
                exhausted=row[1] >= row[2],
            )
            for row in rows
        ]

    async def set_monthly_limit(self, engine_id: str, limit: int) -> QuotaInfo:
        """Update the monthly limit for an engine (takes effect immediately)."""
        month = _current_month_key()
        async with self._db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    INSERT INTO search_engine_quota
                        (engine_id, month_key, usage_count, monthly_limit, notify_enabled, updated_at)
                    VALUES (?, ?, 0, ?, 1, datetime('now'))
                    ON CONFLICT(engine_id, month_key) DO UPDATE SET
                        monthly_limit = ?,
                        updated_at = datetime('now')
                    """,
                    (engine_id, month, limit, limit),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return await self.get_quota(engine_id)

    async def set_notify_enabled(self, engine_id: str, enabled: bool) -> QuotaInfo:
        """Toggle notification for an engine."""
        month = _current_month_key()
        val = 1 if enabled else 0
        async with self._db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    INSERT INTO search_engine_quota
                        (engine_id, month_key, usage_count, monthly_limit, notify_enabled, updated_at)
                    VALUES (?, ?, 0, ?, ?, datetime('now'))
                    ON CONFLICT(engine_id, month_key) DO UPDATE SET
                        notify_enabled = ?,
                        updated_at = datetime('now')
                    """,
                    (engine_id, month, _DEFAULT_MONTHLY_LIMIT, val, val),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return await self.get_quota(engine_id)
