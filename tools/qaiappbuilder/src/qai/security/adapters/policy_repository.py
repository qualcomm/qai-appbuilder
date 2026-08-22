# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`PolicyRepositoryPort` (PR-040).

Schema reference: ``qai-db-schema.md`` §1.1 (security_policy) and §1.2
(security_policy_rule). Persistence is parent-child relational, NOT a
JSON blob, so:

* indexes accelerate evaluation order (``ix_security_policy_rule_policy_position``);
* the ``UNIQUE (policy_id, scope, pattern, case_sensitive)`` constraint
  catches contradictory rules at the DB layer before they reach the
  domain ``__post_init__``;
* schema-level CHECKs validate scope / action / lengths verbatim.

Save semantics — atomic full replacement of the singleton policy and
its child rules within one transaction:

    BEGIN IMMEDIATE;
    INSERT OR IGNORE INTO security_policy(id, version, updated_at)
        VALUES('singleton', 0, ?);
    UPDATE security_policy
        SET version = ?, updated_at = ? WHERE id = 'singleton';
    DELETE FROM security_policy_rule WHERE policy_id = 'singleton';
    INSERT INTO security_policy_rule(...) VALUES (?, ...);  -- per rule
    COMMIT;

We rely on ``ON DELETE CASCADE`` on the FK only as a defensive measure;
``DELETE FROM security_policy_rule WHERE policy_id='singleton'``
already removes the rules explicitly so the singleton header row is
never destroyed.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.time import Clock

from qai.security.domain.entities import Policy, PolicyRule
from qai.security.domain.errors import PolicyRuleConflictError
from qai.security.domain.value_objects import (
    PathPattern,
    PolicyAction,
    PolicyMatchKind,
    PolicyOp,
    PolicyScope,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqlitePolicyRepository"]


_SINGLETON_ID = "singleton"


class SqlitePolicyRepository:
    """aiosqlite implementation of :class:`PolicyRepositoryPort`.

    Returns ``Policy.empty(now=clock.now())`` when no policy has been
    saved yet, matching the port contract.
    """

    __slots__ = ("_db", "_clock")

    def __init__(self, *, db: "Database", clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def load(self) -> Policy:
        """Return the current policy or :meth:`Policy.empty` when absent."""

        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT version, updated_at FROM security_policy "
                    "WHERE id = ?",
                    (_SINGLETON_ID,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    return Policy.empty(now=self._clock.now())
                version, updated_at_iso = int(row[0]), str(row[1])

                cur = await conn.execute(
                    "SELECT id, scope, pattern, case_sensitive, action, "
                    "description, op "
                    "FROM security_policy_rule "
                    "WHERE policy_id = ? "
                    "ORDER BY position ASC",
                    (_SINGLETON_ID,),
                )
                rule_rows = await cur.fetchall()
                await cur.close()
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap cleanly
            raise PersistenceError(
                "security.policy.load_failed",
                f"failed to load policy: {exc}",
                operation="policy.load",
                cause=exc,
            ) from exc

        rules = tuple(self._row_to_rule(r) for r in rule_rows)
        return Policy(
            version=version,
            updated_at=_parse_iso(updated_at_iso),
            rules=rules,
        )

    async def save(self, policy: Policy) -> None:
        """Atomically replace the singleton policy and its rules."""

        updated_at_iso = policy.updated_at.isoformat()
        rule_params: list[tuple[object, ...]] = []
        for position, rule in enumerate(policy.rules):
            rule_params.append(
                (
                    rule.rule_id,
                    _SINGLETON_ID,
                    rule.scope.value,
                    rule.pattern.pattern,
                    1 if rule.pattern.case_sensitive else 0,
                    rule.action.value,
                    rule.description,
                    position,
                    rule.op.value,
                )
            )

        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT OR IGNORE INTO security_policy "
                        "(id, version, updated_at) VALUES (?, ?, ?)",
                        (_SINGLETON_ID, 0, updated_at_iso),
                    )
                    await conn.execute(
                        "UPDATE security_policy "
                        "SET version = ?, updated_at = ? WHERE id = ?",
                        (policy.version, updated_at_iso, _SINGLETON_ID),
                    )
                    await conn.execute(
                        "DELETE FROM security_policy_rule "
                        "WHERE policy_id = ?",
                        (_SINGLETON_ID,),
                    )
                    if rule_params:
                        await conn.executemany(
                            "INSERT INTO security_policy_rule "
                            "(id, policy_id, scope, pattern, "
                            "case_sensitive, action, description, position, "
                            "op) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            rule_params,
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except PolicyRuleConflictError:
            raise
        except Exception as exc:  # noqa: BLE001
            # SQLite IntegrityError -> contradictory rules / duplicate id.
            # We translate via the message because aiosqlite does not
            # subclass per-constraint and we want the domain error type
            # to surface for the route layer's 400 mapping.
            module = type(exc).__module__
            if module.startswith("sqlite3") or module.startswith("aiosqlite"):
                raise PolicyRuleConflictError(
                    "policy save violated a uniqueness or CHECK constraint",
                    details={"sqlite_error": str(exc)},
                ) from exc
            raise PersistenceError(
                "security.policy.save_failed",
                f"failed to save policy: {exc}",
                operation="policy.save",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_rule(
        row: tuple[object, object, object, object, object, object, object],
    ) -> PolicyRule:
        rule_id = str(row[0])
        scope = PolicyScope(str(row[1]))
        pattern_text = str(row[2])
        case_sensitive = bool(int(row[3] or 0))
        action = PolicyAction(str(row[4]))
        description = str(row[5] or "")
        # ``op`` is the tail-appended column (migration 014). Rows written
        # before it existed read back as NULL → default to ``any`` so the
        # historical (operation-agnostic) behaviour is preserved exactly.
        op = PolicyOp(str(row[6] or "any"))
        # exec_deny rules are regexes (V1 ``exec_deny_patterns``); every
        # other op is glob. Deriving match_kind from op keeps the wire /
        # storage shape minimal (no separate match_kind column needed).
        match_kind = (
            PolicyMatchKind.REGEX
            if op is PolicyOp.EXEC_DENY
            else PolicyMatchKind.GLOB
        )
        return PolicyRule(
            rule_id=rule_id,
            scope=scope,
            pattern=PathPattern(
                pattern=pattern_text,
                case_sensitive=case_sensitive,
                match_kind=match_kind,
            ),
            action=action,
            description=description,
            op=op,
        )


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a tz-aware ``datetime``.

    SQLite stores ISO strings verbatim; ``datetime.fromisoformat`` round-
    trips with ``isoformat()`` losslessly for tz-aware values.
    """

    return datetime.fromisoformat(value)
