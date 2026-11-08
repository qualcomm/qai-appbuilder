# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — system / startup endpoints (FEATURE-INVENTORY §3.1).

Covers SYS-01 and SYS-02. Response shapes are taken from
``interfaces/http/routes/system.py`` (``HealthResponse``, ``BuildInfoResponse``,
``EditionResponse``, ``PlatformInfoResponse``, ``PythonInfoResponse``).

These run against the real backend subprocess started by the ``backend``
fixture (port 8989, temp data dir, login gate OFF).
"""

from __future__ import annotations

import platform as _platform
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import REPO_ROOT, Backend

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# SYS-01  backend starts and /api/system/health is healthy
# ---------------------------------------------------------------------------


def test_sys01_health_is_ok(http: httpx.Client):
    r = http.get("/api/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Parses as an ISO-8601 timestamp.
    datetime.fromisoformat(body["timestamp"])


def test_sys01_health_reports_a_working_database(http: httpx.Client):
    db = http.get("/api/system/health").json()["db"]
    # Keys come from HealthResponse.db (journal_mode / foreign_keys /
    # user_version / size_bytes).
    assert set(db) >= {"journal_mode", "foreign_keys", "user_version", "size_bytes"}
    assert db["journal_mode"] == "wal"
    # The app relies on FK enforcement being switched on.
    assert db["foreign_keys"] in (1, True)
    # NOTE: ``user_version`` is PRAGMA user_version, which this project never
    # sets (it is always 0). Migration state lives in _qai_schema_migrations -
    # see the next test. A non-empty file proves the schema was created.
    assert db["size_bytes"] > 0


def test_sys01_all_shipped_migrations_were_applied(backend: Backend):
    """Every migration file we ship is recorded as applied in the fresh DB.

    Compares against the SQL files on disk rather than a hard-coded number, so
    adding a migration does not require touching this test, but a migration that
    silently fails to apply (or a stale DB) does fail it.
    """
    shipped = sorted(
        p.stem
        for p in (REPO_ROOT / "src" / "qai" / "platform" / "persistence" / "migrations_sql").glob("0*.sql")
    )
    assert shipped, "no migration files found - wrong path?"

    db_file = backend.data_dir / "db" / "qai.db"
    assert db_file.is_file()
    # Read-only URI so the test can never modify the backend's database.
    con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        applied = sorted(r[0] for r in con.execute("SELECT id FROM _qai_schema_migrations"))
    finally:
        con.close()
    assert applied == shipped


# ---------------------------------------------------------------------------
# SYS-02  build-info / edition / platform / python-info are sane
# ---------------------------------------------------------------------------


def test_sys02_build_info(http: httpx.Client, backend: Backend):
    body = http.get("/api/system/build-info").json()
    assert body["name"] == "qaiappbuilder"
    assert body["version"]  # non-empty
    assert body["edition"] in ("internal", "external")
    # Data isolation: the backend must report the TEMP data dir we gave it, not
    # the user's real one. This is the guard behind the whole isolation design.
    assert Path(body["data_dir"]).resolve() == backend.data_dir.resolve()


def test_sys02_edition_is_consistent_with_build_info(http: httpx.Client):
    edition = http.get("/api/system/edition").json()
    build = http.get("/api/system/build-info").json()
    assert edition["edition"] == build["edition"]
    assert edition["is_internal"] is (edition["edition"] == "internal")


def test_sys02_platform_matches_this_machine(http: httpx.Client):
    body = http.get("/api/system/platform").json()
    # The backend runs on the same host as the test, so these must agree.
    assert body["system"] == _platform.system()
    assert body["machine"] == _platform.machine()


def test_sys02_python_info(http: httpx.Client):
    body = http.get("/api/system/python-info").json()
    assert body["version"]
    assert Path(body["executable"]).name.lower().startswith("python")
    assert body["platform"] in ("win32", "linux", "darwin")


def test_sys02_config_summary_reflects_the_e2e_port(http: httpx.Client, backend: Backend):
    body = http.get("/api/system/config/summary").json()
    assert body["port"] == backend.port
    assert body["host"] == "127.0.0.1"
