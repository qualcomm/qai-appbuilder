# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Async SQLite engine with safe defaults.

Single ``Database`` instance per application; created in lifespan and
injected through DI. NOT a singleton — tests build their own instances.

Wraps :mod:`aiosqlite`. Sets WAL mode + synchronous=NORMAL + foreign_keys=ON
on first connection of every session. Tracks open connections so callers
can leak-check during tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qai.platform.errors import (
    ConfigurationError,
    InfrastructureError,
    PersistenceError,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite as _aiosqlite_t

_log = get_logger(__name__)

# Default PRAGMAs applied on every connection. WAL gives us readers + writer
# concurrency without blocking; NORMAL synchronous trades a small durability
# window on power loss for a large throughput gain (acceptable for a local
# desktop app).
_INIT_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("temp_store", "MEMORY"),
    ("busy_timeout", "5000"),
)


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Result of :meth:`Database.health_check`."""

    ok: bool
    journal_mode: str
    foreign_keys: bool
    user_version: int
    page_count: int
    page_size: int

    @property
    def size_bytes(self) -> int:
        return self.page_count * self.page_size


class Database:
    """Async SQLite engine wrapper.

    Lifecycle:
        db = Database(path=...)          # cheap; no connection yet
        await db.start()                 # sanity check + create file/dirs
        async with db.connection() as c: # leases a fresh connection
            ...
        await db.close()

    The wrapper does NOT pool connections; ``aiosqlite`` already serialises
    operations through a per-connection thread, so creating a connection
    per logical operation (and reusing only within a single async task) is
    the simplest correct strategy. Callers needing transactional batches
    use ``async with db.connection() as conn: ... conn.commit()``.
    """

    def __init__(self, *, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        self._path = path
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._open_connections = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def open_connections(self) -> int:
        """Number of connections currently leased by callers (tests rely on this)."""
        return self._open_connections

    async def start(self) -> None:
        """Open & close one connection to validate the file and apply PRAGMAs.

        If the database file exists but fails an ``integrity_check``, it is
        renamed to ``<name>.corrupted.<timestamp>`` and a fresh empty database
        is created in its place so the application can always start cleanly.
        History data is lost, but the service remains operational.
        """
        async with self._lock:
            if self._started:
                return
            if self._closed:
                raise InfrastructureError(
                    "persistence.db_closed",
                    "Cannot start a Database that has been closed",
                )
            self._path.parent.mkdir(parents=True, exist_ok=True)

            # --- corruption guard -------------------------------------------
            # Only run the check when the file already exists; a brand-new
            # (absent) path is fine — SQLite will create it on first connect.
            if self._path.exists():
                await self._recover_if_corrupt()
            # ----------------------------------------------------------------

            try:
                async with self._raw_connect() as conn:
                    await self._apply_pragmas(conn)
            except (PersistenceError, ConfigurationError):
                raise
            except Exception as exc:  # noqa: BLE001 — re-raise via PersistenceError
                raise PersistenceError(
                    "persistence.start_failed",
                    f"Failed to open database at {self._path!s}",
                    operation="start",
                    cause=exc,
                ) from exc
            self._started = True
            _log.info(
                "database.started",
                path=str(self._path),
                exists=self._path.exists(),
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            if self._open_connections > 0:
                # Tests rely on this to surface leaks.
                _log.warning(
                    "database.close_with_open_connections",
                    open_connections=self._open_connections,
                )
            self._closed = True
            self._started = False

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        """Lease a fresh aiosqlite connection.

        The returned object is the underlying ``aiosqlite.Connection``;
        we intentionally do not wrap it to keep dependencies thin.
        Callers must ``await conn.commit()`` after writes; the context
        manager only closes the connection on exit, it does NOT auto-commit.
        """
        if self._closed:
            raise InfrastructureError(
                "persistence.db_closed",
                "Cannot acquire connection from a closed Database",
            )
        if not self._started:
            raise InfrastructureError(
                "persistence.db_not_started",
                "Database.start() must be awaited before connection()",
            )

        async with self._raw_connect() as conn:
            await self._apply_pragmas(conn)
            self._open_connections += 1
            try:
                yield conn
            finally:
                self._open_connections -= 1

    async def health_check(self) -> DatabaseHealth:
        """Run a small set of pragmas for diagnostic output."""
        async with self.connection() as conn:
            jm = await _scalar(conn, "PRAGMA journal_mode")
            fk = await _scalar(conn, "PRAGMA foreign_keys")
            uv = await _scalar(conn, "PRAGMA user_version")
            pc = await _scalar(conn, "PRAGMA page_count")
            ps = await _scalar(conn, "PRAGMA page_size")
        return DatabaseHealth(
            ok=True,
            journal_mode=str(jm).lower(),
            foreign_keys=bool(int(fk)),
            user_version=int(uv),
            page_count=int(pc),
            page_size=int(ps),
        )

    async def execute(self, sql: str, parameters: tuple[Any, ...] | None = None) -> None:
        """Convenience: run a single statement and commit."""
        async with self.connection() as conn:
            try:
                await conn.execute(sql, parameters or ())
                await conn.commit()
            except Exception as exc:  # noqa: BLE001
                raise PersistenceError(
                    "persistence.execute_failed",
                    f"execute() failed: {exc}",
                    operation="execute",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _recover_if_corrupt(self) -> None:
        """Run ``PRAGMA integrity_check``; if it fails, back up and delete the
        corrupted file so a fresh database is created on the next connect.

        Handles two failure modes:
        1. SQLite can open the file but reports integrity errors.
        2. SQLite cannot open the file at all (e.g. truncated / not a DB).
        """
        corrupt = False
        try:
            async with self._raw_connect() as conn:
                cur = await conn.execute("PRAGMA integrity_check")
                rows = await cur.fetchall()
                await cur.close()
            results = [row[0] for row in rows]
            if results == ["ok"]:
                return  # healthy — nothing to do
            # integrity_check returned one or more error rows
            corrupt = True
            try:
                _log.error(
                    "database.corrupt_detected",
                    path=str(self._path),
                    errors=results,
                )
            except Exception:  # noqa: BLE001 — log failure must not abort recovery
                pass
        except Exception as exc:  # noqa: BLE001 — file unreadable / not a DB
            corrupt = True
            try:
                _log.error(
                    "database.open_failed_on_start",
                    path=str(self._path),
                    error=type(exc).__name__,
                )
            except Exception:  # noqa: BLE001 — log failure must not abort recovery
                pass

        if not corrupt:
            return

        # Reach here only on corruption or open failure → rename & recreate.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self._path.with_name(
            f"{self._path.stem}.corrupted.{ts}{self._path.suffix}"
        )
        try:
            shutil.move(str(self._path), str(backup))
            try:
                _log.warning(
                    "database.corrupt_backed_up",
                    original=str(self._path),
                    backup=str(backup),
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — rename failed; try plain delete
            try:
                _log.warning(
                    "database.corrupt_backup_failed",
                    path=str(self._path),
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                self._path.unlink(missing_ok=True)
                try:
                    _log.warning("database.corrupt_deleted", path=str(self._path))
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — give up; start() will raise
                try:
                    _log.error(
                        "database.corrupt_unrecoverable",
                        path=str(self._path),
                    )
                except Exception:  # noqa: BLE001
                    pass

    @contextlib.asynccontextmanager
    async def _raw_connect(self) -> AsyncIterator[Any]:
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover
            raise ConfigurationError(
                "persistence.aiosqlite_unavailable",
                "aiosqlite is required for the SQLite backend; "
                "install with `pip install aiosqlite`.",
            ) from exc
        conn = await aiosqlite.connect(self._path)
        try:
            yield conn
        finally:
            await conn.close()

    @staticmethod
    async def _apply_pragmas(conn: Any) -> None:
        for name, value in _INIT_PRAGMAS:
            await conn.execute(f"PRAGMA {name} = {value}")
        await conn.commit()


async def _scalar(conn: Any, sql: str) -> Any:
    cur = await conn.execute(sql)
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return None
    return row[0]
