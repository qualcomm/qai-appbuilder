# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`AuditQueryPort` (PR-040, read-only).

Schema reference: ``qai-db-schema.md`` §1.6 (security_audit_entry).
Companion to :class:`qai.security.adapters.audit_sink.SqliteAuditSink`;
together they implement the CQRS-style split established by PR-040
issue (a) decision A - see ``api-contract.md`` §7.6.

The implementation deliberately uses the
``ix_security_audit_entry_time`` index (``ORDER BY occurred_at DESC``)
so the route layer's ``GET /api/security/audit/recent`` is constant-
time independent of total audit volume.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger

from qai.security.domain.entities import AuditEntry
from qai.security.domain.value_objects import (
    PolicyAction,
    Resource,
    Subject,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteAuditQuery"]


_SELECT_COLUMNS = (
    "id, occurred_at, subject_kind, subject_identifier, resource_kind, "
    "resource_identifier, decision, rule_id, correlation_id, note, channel, "
    "op, process_path, command_line, actor_pid, actor_parent_pid"
)

# Native FileGuard denial identifier - the audit sink writes rows with this
# subject shape whenever ``guard64.dll`` reports a DENY back through the
# native ASK bridge. See ``apps/api/_file_guard_bridge.py`` (subject
# construction) + ``qai.security.domain.entities.AuditEntry`` (schema).
_NATIVE_GUARD_SUBJECT_KIND = "system"
_NATIVE_GUARD_SUBJECT_IDENTIFIER = "native.file_guard"

# Multiplier for the initial candidate window; we over-fetch so the
# Python-side pid-tree walk has enough rows to reconstruct the transitive
# descendant set before applying ``limit``. 8x is a heuristic - large
# enough to keep the common LLM shell pattern (<= 3 layers, <= 10 denies
# per process) working while bounding worst-case memory.
_CANDIDATE_WINDOW_MULTIPLIER = 8

_log = get_logger(__name__)


class SqliteAuditQuery:
    """aiosqlite implementation of :class:`AuditQueryPort` (read side only)."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def recent(self, *, limit: int) -> list[AuditEntry]:
        """Return the ``limit`` most-recent entries, newest first.

        ``limit < 0`` is treated as ``limit = 0`` per the port contract
        (returns an empty list); negative values are not propagated to
        SQLite where they would mean "no limit".
        """

        if limit <= 0:
            return []
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLUMNS} "
                    "FROM security_audit_entry "
                    "ORDER BY occurred_at DESC "
                    "LIMIT ?",
                    (int(limit),),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.audit_entry.recent_failed",
                f"failed to query recent audit entries: {exc}",
                operation="audit_entry.recent",
                cause=exc,
            ) from exc
        return [self._row_to_entry(r) for r in rows]

    async def query_native_denies_by_pid_tree(
        self,
        *,
        root_pid: int,
        since: datetime,
        max_depth: int = 3,
        limit: int = 50,
    ) -> tuple[AuditEntry, ...]:
        """Return native FileGuard DENY entries under ``root_pid``'s pid tree.

        See :class:`qai.security.application.ports.AuditQueryPort` for the
        contract (identification key, ordering, error semantics).

        Algorithm
        ---------
        1. One indexed window scan pulls ``limit * 8`` native.file_guard
           DENY rows with ``occurred_at >= since`` (uses
           ``ix_security_audit_entry_subject_time`` because the leading
           columns ``(subject_kind, subject_identifier, occurred_at DESC)``
           match the WHERE clause exactly).
        2. Python-side BFS over ``actor_parent_pid`` seeds from
           ``{root_pid}`` and expands ``max_depth`` levels. This covers the
           common LLM shell nesting (``bash -> mv``,
           ``bash -> python -> mv``, ``bash -> python -> bash -> mv``)
           without paying for a recursive CTE.
        3. Filter to rows whose ``actor_pid`` OR ``actor_parent_pid`` is in
           the known-pid set (``actor_pid`` included so a direct denial on
           ``root_pid`` itself - e.g. LLM asks exec to run a blocked shell
           command whose top-level syscall is intercepted - is not lost).
        4. Sort ASC by ``occurred_at`` and truncate to ``limit``.

        Any exception is swallowed with a WARNING log and an empty tuple is
        returned, matching the port contract: an audit query failure MUST
        NOT break the exec / background-process tool result the caller is
        assembling.
        """

        if limit <= 0 or max_depth < 0:
            return ()

        # Fetch a generous candidate window so the pid-tree walk has enough
        # rows to reconstruct transitive descendants before we truncate.
        window = max(limit * _CANDIDATE_WINDOW_MULTIPLIER, limit)
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLUMNS} "
                    "FROM security_audit_entry "
                    "WHERE subject_kind = ? "
                    "  AND subject_identifier = ? "
                    "  AND decision = ? "
                    "  AND occurred_at >= ? "
                    "  AND actor_pid IS NOT NULL "
                    "ORDER BY occurred_at ASC "
                    "LIMIT ?",
                    (
                        _NATIVE_GUARD_SUBJECT_KIND,
                        _NATIVE_GUARD_SUBJECT_IDENTIFIER,
                        PolicyAction.DENY.value,
                        since.isoformat(),
                        int(window),
                    ),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "security.audit_entry.native_denies_query_failed",
                error=str(exc),
                root_pid=root_pid,
            )
            return ()

        if not rows:
            return ()

        # Extract (actor_pid, actor_parent_pid) pairs once; indexes match
        # ``_SELECT_COLUMNS`` ordering (actor_pid=14, actor_parent_pid=15).
        pid_pairs: list[tuple[int, int | None]] = []
        for row in rows:
            actor_pid_raw = row[14]
            if actor_pid_raw is None:
                # Defensive - the WHERE clause already excludes NULL
                # actor_pid, but keep the guard so a schema change never
                # crashes the query.
                continue
            parent_raw = row[15]
            pid_pairs.append(
                (
                    int(actor_pid_raw),
                    None if parent_raw is None else int(parent_raw),
                )
            )

        # BFS expansion over ``actor_parent_pid``. Semantics of
        # ``max_depth``: the maximum HOP distance between ``root_pid`` and
        # a matched denial's ``actor_pid``. Concretely:
        #
        # * ``max_depth = 1`` -> only direct children of ``root_pid`` match
        #   (canonical ``bash(root) -> mv(denied)``).
        # * ``max_depth = 2`` -> children + grandchildren
        #   (``bash -> python -> mv``).
        # * ``max_depth = 3`` -> up to great-grandchildren
        #   (``bash -> python -> bash -> mv`` - AGENTS.md target).
        #
        # A denial at depth N is captured when its ``actor_parent_pid`` is
        # in ``known_pids`` after (N - 1) BFS rounds. So we run exactly
        # ``max_depth - 1`` rounds - each round adds one more layer of
        # ancestor pids to ``known_pids``. ``max_depth = 0`` reduces to
        # "only denials whose actor_pid is root_pid itself", achieved by
        # the actor_pid-in-known-pids clause in the filter below.
        known_pids: set[int] = {int(root_pid)}
        for _depth in range(max(0, max_depth - 1)):
            new_pids: set[int] = set()
            for actor_pid, parent_pid in pid_pairs:
                if (
                    parent_pid is not None
                    and parent_pid in known_pids
                    and actor_pid not in known_pids
                ):
                    new_pids.add(actor_pid)
            if not new_pids:
                break
            known_pids |= new_pids

        # A row matches if EITHER its actor_pid or actor_parent_pid is in
        # the known set. Including actor_pid covers the "root_pid itself is
        # blocked" case (top-level shell command intercepted directly).
        matched: list[tuple[Any, ...]] = []
        for row in rows:
            actor_pid_raw = row[14]
            if actor_pid_raw is None:
                continue
            actor_pid = int(actor_pid_raw)
            parent_raw = row[15]
            parent_pid = None if parent_raw is None else int(parent_raw)
            if actor_pid in known_pids or (
                parent_pid is not None and parent_pid in known_pids
            ):
                matched.append(row)

        # ``occurred_at ASC`` was already imposed by the SQL query, so the
        # slice preserves the "execution order" contract.
        matched = matched[: int(limit)]
        try:
            return tuple(self._row_to_entry(r) for r in matched)
        except Exception as exc:  # noqa: BLE001
            # Row-to-entry conversion errors (unexpected schema / value)
            # should not break the caller either.
            _log.warning(
                "security.audit_entry.native_denies_row_decode_failed",
                error=str(exc),
                root_pid=root_pid,
            )
            return ()

    @staticmethod
    def _row_to_entry(row: tuple[object, ...]) -> AuditEntry:
        audit_id = str(row[0])
        occurred_at = datetime.fromisoformat(str(row[1]))
        subject = Subject(kind=str(row[2]), identifier=str(row[3]))
        resource = Resource(kind=str(row[4]), identifier=str(row[5]))
        decision = PolicyAction(str(row[6]))
        rule_id = None if row[7] is None else str(row[7])
        correlation_id = None if row[8] is None else str(row[8])
        note = str(row[9] or "")
        channel = None if row[10] is None else str(row[10])
        op = str(row[11] or "")
        process_path = str(row[12] or "")
        command_line = str(row[13] or "")
        actor_pid = None if row[14] is None else int(row[14])
        actor_parent_pid = None if row[15] is None else int(row[15])
        return AuditEntry(
            audit_id=audit_id,
            occurred_at=occurred_at,
            subject=subject,
            resource=resource,
            decision=decision,
            rule_id=rule_id,
            correlation_id=correlation_id,
            note=note,
            channel=channel,
            op=op,
            process_path=process_path,
            command_line=command_line,
            actor_pid=actor_pid,
            actor_parent_pid=actor_parent_pid,
        )
