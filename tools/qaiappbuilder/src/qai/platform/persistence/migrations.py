# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Schema migrations: sequential ``NNN_description.sql`` files.

We deliberately keep this hand-rolled and tiny (~150 lines) instead of
pulling in alembic for S1, because:
- The plan declares one merged ``qai.db`` with a small number of tables.
- Migrations need to run synchronously at lifespan startup.
- alembic adds a heavy dependency (sqlalchemy core + autogenerate) that
  most callers won't use during S2/S3.

Migration files live in a directory passed at runtime (typically
``src/qai/platform/persistence/migrations/sql/``) and are named
``NNN_short_name.sql`` where NNN is a zero-padded integer (e.g. 001).

Each migration is applied in its own transaction. The applied set is
tracked in a ``_qai_schema_migrations`` table created on first run.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger

from .database import Database

_log = get_logger(__name__)

_FILENAME_RE = re.compile(r"^(\d{3,})_([a-z][a-z0-9_]*)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    path: Path

    @property
    def id(self) -> str:
        return f"{self.version:03d}_{self.name}"


class MigrationRunner:
    """Applies pending migrations from a directory."""

    def __init__(self, *, db: Database, migrations_dir: Path) -> None:
        if not isinstance(migrations_dir, Path):
            raise TypeError("migrations_dir must be a pathlib.Path")
        self._db = db
        self._dir = migrations_dir

    async def run(self) -> list[str]:
        """Apply all pending migrations. Returns the list of newly applied ids."""
        await self._ensure_schema_table()
        applied = await self._fetch_applied()
        all_migrations = list(_discover(self._dir))
        _validate_no_gaps([m.version for m in all_migrations])

        pending = [m for m in all_migrations if m.id not in applied]
        new_ids: list[str] = []
        for mig in pending:
            await self._apply_one(mig)
            new_ids.append(mig.id)
        if new_ids:
            _log.info("persistence.migrations_applied", count=len(new_ids), ids=new_ids)
        return new_ids

    async def applied_ids(self) -> set[str]:
        await self._ensure_schema_table()
        return await self._fetch_applied()

    async def _ensure_schema_table(self) -> None:
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS _qai_schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

    async def _fetch_applied(self) -> set[str]:
        async with self._db.connection() as conn:
            cur = await conn.execute("SELECT id FROM _qai_schema_migrations")
            rows = await cur.fetchall()
            await cur.close()
        return {row[0] for row in rows}

    async def _apply_one(self, mig: Migration) -> None:
        _log.info("persistence.applying_migration", id=mig.id)
        async with self._db.connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                # ``executescript`` runs all statements but commits at end;
                # we want to share a single explicit transaction so that on
                # failure the entire migration rolls back.
                await _execute_script(conn, mig.sql)
                await conn.execute(
                    "INSERT INTO _qai_schema_migrations (id, applied_at) VALUES (?, ?)",
                    (mig.id, datetime.now(timezone.utc).isoformat()),
                )
                await conn.commit()
            except Exception as exc:  # noqa: BLE001
                await conn.rollback()
                raise PersistenceError(
                    "persistence.migration_failed",
                    f"Migration {mig.id} failed: {exc}",
                    operation="migrate",
                    cause=exc,
                ) from exc


async def migrate(db: Database, *, migrations_dir: Path) -> list[str]:
    """Convenience function used by lifespan code."""
    return await MigrationRunner(db=db, migrations_dir=migrations_dir).run()


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _discover(directory: Path) -> Iterator[Migration]:
    if not directory.is_dir():
        # An empty migrations dir is valid (no schema yet).
        return iter(())
    files = sorted(directory.iterdir(), key=lambda p: p.name)
    return (mig for mig in (_parse(f) for f in files) if mig is not None)


def _parse(path: Path) -> Migration | None:
    if not path.is_file() or path.suffix != ".sql":
        return None
    m = _FILENAME_RE.match(path.name)
    if m is None:
        raise PersistenceError(
            "persistence.bad_migration_filename",
            f"Migration filename must match NNN_name.sql: {path.name!r}",
            operation="discover_migrations",
        )
    version = int(m.group(1))
    name = m.group(2)
    sql = path.read_text(encoding="utf-8")
    return Migration(version=version, name=name, sql=sql, path=path)


def _validate_no_gaps(versions: list[int]) -> None:
    if not versions:
        return
    if len(set(versions)) != len(versions):
        dupes = sorted({v for v in versions if versions.count(v) > 1})
        raise PersistenceError(
            "persistence.duplicate_migration_version",
            f"Duplicate migration versions: {dupes}",
            operation="discover_migrations",
        )
    sorted_versions = sorted(versions)
    if sorted_versions != list(range(sorted_versions[0], sorted_versions[0] + len(sorted_versions))):
        raise PersistenceError(
            "persistence.gapped_migration_versions",
            f"Migration versions must be contiguous; got {sorted_versions}",
            operation="discover_migrations",
        )


async def _execute_script(conn: Any, sql: str) -> None:
    """Execute a multi-statement SQL script through aiosqlite.

    aiosqlite's ``executescript`` issues an implicit COMMIT, which would
    break our explicit transaction; so we split on ``;`` ourselves.
    """
    statements = [s.strip() for s in _split_sql_statements(sql) if s.strip()]
    for stmt in statements:
        await conn.execute(stmt)


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script on top-level ``;`` boundaries.

    Respects single-line ``--`` comments, basic single/double quoted
    strings, multi-line ``/* */`` comments (stripped first), and SQL
    compound statement blocks delimited by ``BEGIN`` / ``END``. The
    BEGIN/END awareness is required for ``CREATE TRIGGER`` definitions
    whose body contains semicolon-terminated statements that must NOT
    be split off into separate top-level statements.
    """
    text = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    # Depth of nested BEGIN ... END blocks. While > 0 we ignore ``;``
    # for splitting purposes — they belong to the compound block body.
    begin_depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            # skip until newline (do not append the comment to buf)
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Match BEGIN / END as whole words (case-insensitive). They
        # must be flanked by non-word characters or string boundaries
        # so we don't match identifiers like ``MYBEGIN`` or ``DEPENDED``.
        if ch in ("B", "b") and _word_boundary_match(text, i, "BEGIN"):
            begin_depth += 1
            buf.append(text[i:i + 5])
            i += 5
            continue
        if ch in ("E", "e") and _word_boundary_match(text, i, "END"):
            if begin_depth > 0:
                begin_depth -= 1
            buf.append(text[i:i + 3])
            i += 3
            continue
        if ch == ";" and begin_depth == 0:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def _word_boundary_match(text: str, pos: int, keyword: str) -> bool:
    """Return True iff ``keyword`` (case-insensitive) starts at ``pos``
    and is flanked by non-word characters (or string boundaries) so
    plain identifiers containing the keyword as a substring don't
    match.
    """
    n = len(text)
    klen = len(keyword)
    if pos + klen > n:
        return False
    if text[pos:pos + klen].upper() != keyword.upper():
        return False
    # Left boundary
    if pos > 0:
        left = text[pos - 1]
        if left.isalnum() or left == "_":
            return False
    # Right boundary
    if pos + klen < n:
        right = text[pos + klen]
        if right.isalnum() or right == "_":
            return False
    return True
