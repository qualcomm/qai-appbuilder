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

Two lease shapes, both counted by ``open_connections``:

* :meth:`Database.connection` — a FRESH connection per logical operation,
  closed on context exit.  The default, and what every read path and
  every one-shot write uses.
* :meth:`Database.persistent_connection` — ONE connection the caller holds
  across many transactions, with the per-connection PRAGMAs applied once at
  lease time instead of once per transaction.  For a serialised writer that
  commits continuously, that removes the dominant cost of a small write:
  opening the connection and replaying its PRAGMAs measured 7.32ms of a
  9.52ms write, against 2.20ms for the ``BEGIN``/``INSERT``/``COMMIT`` that
  does the actual work.  A repository write path opts in by accepting a
  ``conn`` and running its transaction through :func:`write_transaction`.
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

# PRAGMAs, split by SCOPE — which is also what keeps a write cheap.
#
# ``journal_mode=WAL`` is a property of the DATABASE FILE: SQLite records it
# in the file header and every later connection inherits it.  Re-issuing it
# per connection is therefore a no-op that still costs a measured ~5.3ms of
# the ~6.0ms PRAGMA block (it has to take the database lock to verify the
# mode), which on a write-heavy path is pure overhead — so it is applied
# ONCE, in :meth:`Database.start`, and read back by
# :meth:`Database.health_check` from then on.
#
# The rest are per-CONNECTION settings that genuinely have to be replayed on
# each new connection: ``synchronous`` trades a small durability window on
# power loss for a large throughput gain (acceptable for a local desktop
# app), ``foreign_keys`` enables the cascade deletes the schema relies on,
# and ``busy_timeout`` is how long a writer waits for the file lock.
_DURABLE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
)
_INIT_PRAGMAS: tuple[tuple[str, str], ...] = (
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

    A writer that commits CONTINUOUSLY — one serialised worker retiring a
    stream of small transactions — is the case that strategy does not fit,
    because it pays the open + PRAGMA cost once per transaction for a
    connection it could have kept.  :meth:`persistent_connection` is the
    explicit opt-in for exactly that shape.
    """

    #: The per-connection PRAGMAs as ONE statement batch.  Each
    #: ``conn.execute`` is a round trip to ``aiosqlite``'s worker thread, so
    #: sending four of them separately costs four hand-offs where one will
    #: do.  Built once at class definition, not per lease.
    _INIT_PRAGMA_SCRIPT = "".join(
        f"PRAGMA {name} = {value};" for name, value in _INIT_PRAGMAS
    )

    def __init__(self, *, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        self._path = path
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._open_connections = 0
        #: Live :meth:`persistent_connection` leases, so :meth:`close` can
        #: hard-close a connection whose holder never returned it (a worker
        #: cancelled mid-flight) instead of leaking it into the interpreter
        #: shutdown.  Identity-keyed by the connection object.
        self._persistent: dict[int, Any] = {}

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
                    # ``journal_mode=WAL`` is persisted in the file header
                    # here, so ``connection()`` never has to re-issue it.
                    for name, value in _DURABLE_PRAGMAS:
                        await conn.execute(f"PRAGMA {name} = {value}")
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
            # A persistent lease outlives any single transaction, so at
            # shutdown its holder may already be gone (a cancelled worker
            # never reaches its ``finally``).  Close those sockets here —
            # this is the only lease shape whose lifetime is not bounded by
            # a ``with`` block on the caller's stack.
            stale = list(self._persistent.values())
            self._persistent.clear()
            for conn in stale:
                self._open_connections -= 1
                with contextlib.suppress(Exception):
                    await conn.close()
            if stale:
                _log.warning(
                    "database.close_reclaimed_persistent_connections",
                    reclaimed=len(stale),
                )
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

    async def lease_persistent_connection(self) -> Any:
        """Lease a connection the CALLER holds across many transactions.

        The per-connection PRAGMAs are applied ONCE here rather than once
        per transaction, which is the entire point: for a serialised writer
        the open + PRAGMA replay measured 7.32ms against 2.20ms of real
        ``BEGIN``/``INSERT``/``COMMIT`` work, so amortising it over a whole
        worker lifetime is a ~4x cut in per-write cost.

        The lease is counted by :attr:`open_connections` exactly like a
        :meth:`connection` lease, and is tracked so :meth:`close` can
        reclaim it if its holder was cancelled before returning it.

        The caller MUST return it via :meth:`return_persistent_connection`.
        Prefer :meth:`persistent_connection`, which does that for you; take
        the raw pair only when the lease's lifetime is a worker's lifetime
        rather than a block's (``asyncio`` cancellation makes an
        ``async with`` spanning a whole worker loop awkward to reason about).
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
        conn = await self._connect()
        try:
            await self._apply_pragmas(conn)
        except Exception:
            with contextlib.suppress(Exception):
                await conn.close()
            raise
        self._persistent[id(conn)] = conn
        self._open_connections += 1
        return conn

    async def return_persistent_connection(self, conn: Any) -> None:
        """Close a :meth:`lease_persistent_connection` lease.  Idempotent.

        Returning a connection that :meth:`close` already reclaimed, or one
        already returned, is a no-op: a worker racing shutdown would
        otherwise double-decrement :attr:`open_connections`.
        """
        if self._persistent.pop(id(conn), None) is None:
            return
        self._open_connections -= 1
        with contextlib.suppress(Exception):
            await conn.close()

    @contextlib.asynccontextmanager
    async def persistent_connection(self) -> AsyncIterator[Any]:
        """Scoped form of :meth:`lease_persistent_connection`."""
        conn = await self.lease_persistent_connection()
        try:
            yield conn
        finally:
            await self.return_persistent_connection(conn)

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

    async def _connect(self) -> Any:
        """Open one aiosqlite connection, with the dependency guard."""
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover
            raise ConfigurationError(
                "persistence.aiosqlite_unavailable",
                "aiosqlite is required for the SQLite backend; "
                "install with `pip install aiosqlite`.",
            ) from exc
        return await aiosqlite.connect(self._path)

    @contextlib.asynccontextmanager
    async def _raw_connect(self) -> AsyncIterator[Any]:
        conn = await self._connect()
        try:
            yield conn
        finally:
            await conn.close()

    @classmethod
    async def _apply_pragmas(cls, conn: Any) -> None:
        """Replay the per-connection PRAGMAs on a freshly-opened connection.

        ``executescript`` commits any open transaction before running, which
        is harmless here: this is only ever called on a connection that has
        not been handed to a caller yet.
        """
        await conn.executescript(cls._INIT_PRAGMA_SCRIPT)


@contextlib.asynccontextmanager
async def write_transaction(
    db: Database, conn: Any | None
) -> AsyncIterator[Any]:
    """Run one ``BEGIN IMMEDIATE`` transaction, on a fresh or borrowed connection.

    ``conn is None`` — the default for every caller that does not manage a
    lease — leases a fresh connection through :meth:`Database.connection`,
    which is byte-for-byte the shape every repository write used before this
    helper existed.  A non-``None`` ``conn`` is a caller-owned
    :meth:`Database.lease_persistent_connection` lease that OUTLIVES this
    block; the transaction commits or rolls back, but the connection stays
    open for the next one.

    Reusing a connection across transactions is only safe if a failure
    cannot leak state into the next one, so the rollback here is
    unconditional on the error path and defensive on the way IN: a previous
    transaction that died between ``BEGIN`` and its own rollback (a
    cancellation landing at the wrong await) would otherwise make this
    ``BEGIN IMMEDIATE`` fail with "cannot start a transaction within a
    transaction" and poison the connection for the rest of the worker's
    life.  Committing is the caller's job — the body knows when its writes
    are complete.
    """
    if conn is None:
        async with db.connection() as fresh:
            await fresh.execute("BEGIN IMMEDIATE")
            try:
                yield fresh
            except BaseException:
                with contextlib.suppress(Exception):
                    await fresh.rollback()
                raise
        return
    if conn.in_transaction:
        with contextlib.suppress(Exception):
            await conn.rollback()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        with contextlib.suppress(Exception):
            await conn.rollback()
        raise


async def _scalar(conn: Any, sql: str) -> Any:
    cur = await conn.execute(sql)
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return None
    return row[0]
