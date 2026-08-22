# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`AuditSinkPort` (PR-040, append-only).

Schema reference: ``qai-db-schema.md`` §1.6 (security_audit_entry).
This adapter only implements ``append`` per the port contract; the
companion :class:`SqliteAuditQuery` (in :mod:`audit_query`) handles
the read side of the same table (issue (a) decision A — see PR-040
``api-contract.md`` §7.6).

Append performance is bounded by SQLite write throughput (WAL mode +
``synchronous=NORMAL``). The
``ix_security_audit_entry_subject_time`` and
``ix_security_audit_entry_time`` indexes accelerate the typical read
patterns and have minimal write cost (single INSERT per record).

Rotation (PR-G1 / F-17, 2026-06-06)
-----------------------------------
V1 truth source for "audit rotation" is
``backend/app_builder/audit_log.py:31`` — a **file-level** rotation
(``_AUDIT_MAX_BYTES = 10 * 1024 * 1024``, single ``.1.bak`` backup),
applicable to the V1 ``app_builder`` jsonl file audit. V1 had **no**
SQLite security-audit rotation; the V2 plan (`security-implementation.md`
appendix D TODO S-4 / GAP-REMEDIATION-PLAN F-17) tracks it as the
"V1-equivalent" requirement (10MB / 5 backup-equivalent → ~50K rows
single-table cap).

Design (adapter-internal, port-shape unchanged):

* Optional constructor arg ``max_rows: int | None`` — when set, the
  sink keeps the ``security_audit_entry`` table bounded to roughly
  this many rows. When ``None`` (the default), rotation is disabled
  and behaviour is byte-for-byte identical to PR-040.
* To avoid an N+1 ``SELECT COUNT(*)`` on every ``append``, the sink
  only re-checks row count every ``check_interval`` writes (defaults
  to ``max(100, max_rows // 100)``); when the threshold is crossed,
  it deletes the oldest rows down to ``max_rows * 0.9`` in a single
  ``DELETE`` (using the PK index ``id ASC`` — ULIDs are
  monotonically time-ordered so this matches "oldest first" semantics).
* Failures during prune are best-effort logged and swallowed; the
  ``append`` itself must remain durable. (V1 ``audit_log.py:78``
  parity — "best-effort rotation, never fail the write".)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger

from qai.security.domain.entities import AuditEntry

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteAuditSink"]


_INSERT_SQL = (
    "INSERT INTO security_audit_entry "
    "(id, occurred_at, subject_kind, subject_identifier, resource_kind, "
    "resource_identifier, decision, rule_id, correlation_id, note, channel, "
    "op, process_path, command_line, actor_pid, actor_parent_pid) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_COUNT_SQL = "SELECT COUNT(*) FROM security_audit_entry"
# ``id`` ASC == "oldest first" because the column stores ULIDs which are
# monotonically time-ordered; this matches the V1 file-level
# "drop oldest" semantic without paying the cost of an
# ``ORDER BY occurred_at`` (we already have the PK index for ``id``).
#
# The ``occurred_at < ?`` guard implements the "minimum retention floor"
# (see :class:`SqliteAuditSink`): even when the row-count cap is
# breached, entries newer than the cutoff are NEVER deleted. This means
# rotation may leave the table temporarily above ``max_rows`` if the
# retention window is heavily loaded (a deliberate tradeoff — the user
# explicitly asked for "at least N days retention" over strict row cap).
_PRUNE_SQL = (
    "DELETE FROM security_audit_entry WHERE id IN ("
    "  SELECT id FROM security_audit_entry "
    "  WHERE occurred_at < ? "
    "  ORDER BY id ASC LIMIT ?"
    ")"
)

_log = get_logger(__name__)


class SqliteAuditSink:
    """aiosqlite implementation of :class:`AuditSinkPort` (write side only).

    Read access is intentionally NOT exposed here; callers that need to
    list / filter audit entries depend on
    :class:`SqliteAuditQuery` instead (issue (a) decision A — PR-040).

    Parameters
    ----------
    db:
        Shared :class:`qai.platform.persistence.Database` handle.
    max_rows:
        Optional row-count cap (PR-G1 / F-17). When set, the sink
        prunes the oldest rows down to ``int(max_rows * 0.9)`` whenever
        the count crosses ``max_rows``. ``None`` (default) disables
        rotation entirely (PR-040 byte-for-byte behaviour). Values
        ``<= 0`` are normalised to ``None``.
    check_interval:
        How many ``append`` calls to skip between row-count probes.
        Defaults to ``max(100, max_rows // 100)`` so a 50_000-row cap
        only re-checks once every 500 writes — keeping hot-path
        overhead down to a single INSERT in the steady state. When
        ``max_rows`` is ``None`` this argument is ignored.
    min_retention_days:
        Minimum retention floor (default 7). Rows whose ``occurred_at``
        is newer than ``now - min_retention_days`` are NEVER pruned,
        even when the ``max_rows`` cap is breached. Set to ``None`` (or
        ``<= 0``) to disable the floor and revert to pure row-count
        rotation. When ``max_rows`` is ``None`` this argument is
        ignored (nothing to prune).
    """

    __slots__ = (
        "_db",
        "_max_rows",
        "_target_rows",
        "_check_interval",
        "_writes_since_check",
        "_min_retention_days",
    )

    _DEFAULT_HIGH_WATERMARK_RATIO = 0.9

    def __init__(
        self,
        *,
        db: "Database",
        max_rows: int | None = None,
        check_interval: int | None = None,
        min_retention_days: int | None = 7,
    ) -> None:
        self._db = db
        # Normalise ``max_rows``: non-positive values disable rotation.
        if max_rows is not None and max_rows <= 0:
            max_rows = None
        self._max_rows: int | None = max_rows
        if max_rows is None:
            self._target_rows = 0
            self._check_interval = 0
        else:
            self._target_rows = max(
                1, int(max_rows * self._DEFAULT_HIGH_WATERMARK_RATIO)
            )
            if check_interval is None or check_interval <= 0:
                # Re-check at least every 100 writes; for large caps,
                # check every ``max_rows / 100`` writes (e.g. 500 writes
                # for ``max_rows=50_000``) so we don't pay COUNT on every
                # append.
                check_interval = max(100, max_rows // 100)
            self._check_interval = check_interval
        # Normalise ``min_retention_days``: non-positive disables the floor.
        if min_retention_days is not None and min_retention_days <= 0:
            min_retention_days = None
        self._min_retention_days: int | None = min_retention_days
        self._writes_since_check = 0

    async def append(self, entry: AuditEntry) -> None:
        params = (
            entry.audit_id,
            entry.occurred_at.isoformat(),
            entry.subject.kind,
            entry.subject.identifier,
            entry.resource.kind,
            entry.resource.identifier,
            entry.decision.value,
            entry.rule_id,
            entry.correlation_id,
            entry.note,
            entry.channel,
            entry.op,
            entry.process_path,
            entry.command_line,
            entry.actor_pid,
            entry.actor_parent_pid,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute(_INSERT_SQL, params)
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.audit_entry.append_failed",
                f"failed to append audit entry {entry.audit_id!r}: {exc}",
                operation="audit_entry.append",
                cause=exc,
            ) from exc

        if self._max_rows is None:
            return

        self._writes_since_check += 1
        if self._writes_since_check < self._check_interval:
            return
        self._writes_since_check = 0
        # Best-effort rotation: any failure here must NOT propagate
        # because the durable INSERT above has already committed.
        # Mirrors V1 ``audit_log.py:78`` ("rotate failures are
        # logged-and-swallowed").
        try:
            await self._maybe_prune()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "security.audit_entry.rotation_failed",
                error=str(exc),
                max_rows=self._max_rows,
            )

    async def _maybe_prune(self) -> None:
        """Probe row count; if above ``max_rows``, prune the oldest rows
        down to ~90% of the cap, but NEVER delete rows newer than the
        retention floor (``now - min_retention_days``).

        When the retention floor is disabled (``min_retention_days`` is
        ``None``) the cutoff is set to "now" (effectively removing the
        floor, since no row can have an ``occurred_at`` in the future
        under normal operation).
        """

        assert self._max_rows is not None  # guarded by caller
        if self._min_retention_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=self._min_retention_days
            )
        else:
            # No retention floor requested: use a far-future sentinel so
            # the ``occurred_at < ?`` guard admits every existing row
            # (equivalent to no floor). We can't use ``now()`` because
            # rows written in the same wall-clock second (or with a
            # writer using the same ``now``) would tie on the ISO string
            # and be filtered OUT by the strict ``<`` comparison, which
            # would silently disable rotation under fast-write bursts.
            cutoff = datetime.max.replace(tzinfo=timezone.utc)
        async with self._db.connection() as conn:
            cur = await conn.execute(_COUNT_SQL)
            row = await cur.fetchone()
            await cur.close()
            current = int(row[0]) if row else 0
            if current <= self._max_rows:
                return
            to_delete = current - self._target_rows
            if to_delete <= 0:
                return
            cur2 = await conn.execute(
                _PRUNE_SQL, (cutoff.isoformat(), to_delete)
            )
            deleted = cur2.rowcount if cur2.rowcount is not None else 0
            await cur2.close()
            await conn.commit()
        _log.info(
            "security.audit_entry.rotated",
            deleted=deleted,
            kept=self._target_rows,
            max_rows=self._max_rows,
            min_retention_days=self._min_retention_days,
        )
