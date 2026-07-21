# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation of the fresh ``data/`` initialiser (PR-061)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger
from qai.platform.persistence import Database
from qai.platform.persistence.migrations import MigrationRunner

from .._common.modes import Mode
from .._common.report import InitReport, InitReportEntry

_LOGGER = get_logger("qai.init.data_dir")


# Directory tree the initialiser creates under ``--data-root``.
# Order matters: parents listed first.
EXPECTED_DIRS: tuple[str, ...] = (
    "db",
    "db/backups",
    "blobs",
    "blobs/chat",
    "blobs/app_builder",
    "blobs/uploads",
    "blobs/uploads/audio",
    "blobs/uploads/images",
    "audit",
    "audit/app_builder",
    "cache",
    "prefs",
    "secrets",
    "tmp",
)

# Tables expected to exist after running the 7 SQL migrations
# (mirrors qai-db-schema.md §0.2 + the SQL files under
# ``src/qai/platform/persistence/migrations_sql/``).
EXPECTED_TABLES: frozenset[str] = frozenset({
    # 001_create_security_schema.sql
    "security_policy",
    "security_policy_rule",
    "security_path_grant",
    "security_acl_tracking",
    "security_permission_request",
    "security_audit_entry",
    # 002_create_chat_schema.sql
    "chat_conversation",
    "chat_message",
    "chat_conversation_tab",
    "chat_experience",
    # 003_create_app_builder_schema.sql
    "app_builder_model_definition",
    "app_builder_run",
    "app_builder_artifact",
    "app_builder_share",
    "app_builder_voice_pref",
    "app_builder_audit_entry",
    "app_builder_import_commit",
    # 004_create_ai_coding_schema.sql
    "ai_coding_session",
    "ai_coding_message",
    "ai_coding_permission_request",
    "ai_coding_tool_invocation",
    "ai_coding_skill",
    # 005_create_channels_schema.sql
    "channels_instance",
    "channels_message",
    "channels_session_index",
    "channels_qr_login_challenge",
    # 006_create_model_catalog_schema.sql
    "model_catalog_entry",
    "model_catalog_version",
    "model_catalog_download_job",
    "model_catalog_release_manifest",
    "model_catalog_release_manifest_entry",
    "model_catalog_skill",
    # 007_create_kv_user_prefs.sql
    "kv_user_prefs",
    # framework
    "_qai_schema_migrations",
})


# Minimal placeholder when ``factory/user_config.toml`` is absent.
_PLACEHOLDER_USER_CONFIG = """\
# QAI user configuration (placeholder).
#
# This file was created by tools/init/data_dir because no
# factory/user_config.toml was bundled with the install.
# Edit at runtime via the UI or the qai config command.

[meta]
generated_by = "tools/init/data_dir"
schema_version = 1
"""


@dataclass
class RunResult:
    mode: Mode
    report: InitReport
    files_written: list[Path] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.report.is_ok() else 1


def run(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path | None,
    sql_migrations_dir: Path,
) -> RunResult:
    """Run the fresh-init for one ``data/`` tree.

    Args:
        mode: ``dry-run`` / ``apply`` / ``verify``.
        data_root: target ``data/`` directory (absolute).
        factory_root: bundle root containing ``user_config.toml`` (and,
            for downstream PR-062/063, ``db_staging/*.jsonl`` +
            ``secrets_manifest.json``). May be ``None`` if no factory
            bundle is provided.
        sql_migrations_dir: location of the ``NNN_*.sql`` files
            (typically ``src/qai/platform/persistence/migrations_sql``).

    Returns:
        :class:`RunResult` with the populated report.
    """
    report = InitReport(
        initialiser="data_dir",
        mode=mode,
        data_root=str(data_root),
        factory_root=str(factory_root) if factory_root else "",
    )
    files_written: list[Path] = []

    if mode == "verify":
        _verify(
            data_root=data_root,
            sql_migrations_dir=sql_migrations_dir,
            report=report,
        )
        return RunResult(mode=mode, report=report)

    if mode == "apply":
        _apply_directories(data_root=data_root, report=report, files_written=files_written)
        _apply_qai_db(
            data_root=data_root,
            sql_migrations_dir=sql_migrations_dir,
            report=report,
            files_written=files_written,
        )
        _apply_user_config(
            data_root=data_root,
            factory_root=factory_root,
            report=report,
            files_written=files_written,
        )
        _write_manifest(
            data_root=data_root,
            factory_root=factory_root,
            sql_migrations_dir=sql_migrations_dir,
            files_written=files_written,
            report=report,
        )
    else:
        # dry-run: enumerate every step we would have taken.
        _plan_directories(data_root=data_root, mode=mode, report=report)
        _plan_qai_db(
            sql_migrations_dir=sql_migrations_dir,
            data_root=data_root,
            report=report,
        )
        _plan_user_config(
            data_root=data_root,
            factory_root=factory_root,
            report=report,
        )

    return RunResult(mode=mode, report=report, files_written=files_written)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _plan_directories(
    *,
    data_root: Path,
    mode: Mode,
    report: InitReport,
) -> None:
    for rel in EXPECTED_DIRS:
        target = data_root / rel
        if target.exists() and target.is_dir():
            report.add(InitReportEntry(
                initialiser="data_dir.create_dir",
                location="skipped_existing",
                target=str(target),
                note="directory already present",
            ))
        else:
            report.add(InitReportEntry(
                initialiser="data_dir.create_dir",
                location="data_dir" if mode == "apply" else "noop_dryrun",
                target=str(target),
            ))


def _plan_qai_db(
    *,
    sql_migrations_dir: Path,
    data_root: Path,
    report: InitReport,
) -> None:
    db_path = data_root / "db" / "qai.db"
    report.add(InitReportEntry(
        initialiser="data_dir.create_qai_db",
        location="data_db_qai" if not db_path.exists() else "skipped_existing",
        target=str(db_path),
    ))
    if not sql_migrations_dir.is_dir():
        report.add_error(
            f"sql_migrations_dir not found: {sql_migrations_dir}"
        )
        return
    sql_files = sorted(
        p for p in sql_migrations_dir.iterdir()
        if p.suffix == ".sql"
    )
    for sql in sql_files:
        report.add(InitReportEntry(
            initialiser="data_dir.apply_migration",
            location="data_db_migration",
            target=sql.name,
        ))


def _plan_user_config(
    *,
    data_root: Path,
    factory_root: Path | None,
    report: InitReport,
) -> None:
    target = data_root / "user_config.toml"
    if target.exists():
        report.add(InitReportEntry(
            initialiser="data_dir.copy_user_config",
            location="skipped_existing",
            target=str(target),
        ))
        return
    src = (
        factory_root / "user_config.toml"
        if factory_root is not None
        else None
    )
    note = (
        "from factory/user_config.toml"
        if src and src.exists()
        else "minimal placeholder (no factory bundle)"
    )
    report.add(InitReportEntry(
        initialiser="data_dir.copy_user_config",
        location="data_user_config",
        target=str(target),
        note=note,
    ))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply_directories(
    *,
    data_root: Path,
    report: InitReport,
    files_written: list[Path],
) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    for rel in EXPECTED_DIRS:
        target = data_root / rel
        already = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        if not already:
            files_written.append(target)
            _LOGGER.info("data_dir.created", path=str(target), kind="dir")
            report.add(InitReportEntry(
                initialiser="data_dir.create_dir",
                location="data_dir",
                target=str(target),
            ))
        else:
            report.add(InitReportEntry(
                initialiser="data_dir.create_dir",
                location="skipped_existing",
                target=str(target),
            ))


def _apply_qai_db(
    *,
    data_root: Path,
    sql_migrations_dir: Path,
    report: InitReport,
    files_written: list[Path],
) -> None:
    db_path = data_root / "db" / "qai.db"
    if not sql_migrations_dir.is_dir():
        report.add_error(
            f"sql_migrations_dir not found: {sql_migrations_dir}"
        )
        return

    new_db = not db_path.exists()
    db = Database(path=db_path)
    try:
        applied_ids = asyncio.run(_apply_migrations(db, sql_migrations_dir))
    except PersistenceError as exc:
        report.add_error(
            f"qai.db migration failed: {exc.code}: {exc.message}"
        )
        return

    if new_db:
        files_written.append(db_path)
        report.add(InitReportEntry(
            initialiser="data_dir.create_qai_db",
            location="data_db_qai",
            target=str(db_path),
        ))
    else:
        report.add(InitReportEntry(
            initialiser="data_dir.create_qai_db",
            location="skipped_existing",
            target=str(db_path),
            note="re-using existing qai.db; only pending migrations applied",
        ))

    for mid in applied_ids:
        report.add(InitReportEntry(
            initialiser="data_dir.apply_migration",
            location="data_db_migration",
            target=mid,
        ))

    # Confirm expected tables now exist (defence in depth — the
    # migration runner returned success but did the SQL really create
    # everything we expected?).
    missing = _check_expected_tables(db_path)
    if missing:
        report.add_error(
            "qai.db missing tables after migration: "
            + ", ".join(sorted(missing))
        )


async def _apply_migrations(db: Database, dir_: Path) -> list[str]:
    await db.start()
    try:
        runner = MigrationRunner(db=db, migrations_dir=dir_)
        new_ids = await runner.run()
        return new_ids
    finally:
        await db.close()


def _check_expected_tables(db_path: Path) -> set[str]:
    """Synchronous read of sqlite_master to detect missing tables."""
    if not db_path.exists():
        return set(EXPECTED_TABLES)
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    present = {r[0] for r in rows}
    return EXPECTED_TABLES - present


def _apply_user_config(
    *,
    data_root: Path,
    factory_root: Path | None,
    report: InitReport,
    files_written: list[Path],
) -> None:
    target = data_root / "user_config.toml"
    if target.exists():
        report.add(InitReportEntry(
            initialiser="data_dir.copy_user_config",
            location="skipped_existing",
            target=str(target),
            note="user_config.toml already present; not overwritten",
        ))
        return

    src = (
        factory_root / "user_config.toml"
        if factory_root is not None
        else None
    )
    if src is not None and src.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        files_written.append(target)
        report.add(InitReportEntry(
            initialiser="data_dir.copy_user_config",
            location="data_user_config",
            target=str(target),
            note=f"copied from {src}",
        ))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_PLACEHOLDER_USER_CONFIG, encoding="utf-8")
        files_written.append(target)
        report.add(InitReportEntry(
            initialiser="data_dir.copy_user_config",
            location="data_user_config",
            target=str(target),
            note="minimal placeholder (no factory bundle)",
        ))


def _write_manifest(
    *,
    data_root: Path,
    factory_root: Path | None,
    sql_migrations_dir: Path,
    files_written: list[Path],
    report: InitReport,
) -> None:
    target = data_root / "init_manifest.json"
    obj: dict[str, Any] = {
        "_kind": "init_data_dir_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_root": str(data_root),
        "factory_root": str(factory_root) if factory_root else None,
        "sql_migrations_dir": str(sql_migrations_dir),
        "expected_dirs": list(EXPECTED_DIRS),
        "expected_tables": sorted(EXPECTED_TABLES),
        "files_written": [str(p) for p in files_written],
        "schema_version_at_init": _read_schema_version(data_root),
    }
    target.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files_written.append(target)
    report.add(InitReportEntry(
        initialiser="data_dir.write_manifest",
        location="data_dir",
        target=str(target),
    ))


def _read_schema_version(data_root: Path) -> str | None:
    """Return latest applied migration id, or None if qai.db absent."""
    db_path = data_root / "db" / "qai.db"
    if not db_path.exists():
        return None
    with sqlite3.connect(str(db_path)) as conn:
        try:
            rows = conn.execute(
                "SELECT id FROM _qai_schema_migrations ORDER BY id"
            ).fetchall()
        except sqlite3.DatabaseError:
            return None
    return rows[-1][0] if rows else None


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _verify(
    *,
    data_root: Path,
    sql_migrations_dir: Path,
    report: InitReport,
) -> None:
    if not data_root.is_dir():
        report.add_error(f"verify: data_root {data_root} does not exist")
        return

    for rel in EXPECTED_DIRS:
        target = data_root / rel
        if not target.is_dir():
            report.add_error(f"verify: missing directory {target}")
        else:
            report.add(InitReportEntry(
                initialiser="data_dir.verify_dir",
                location="data_dir",
                target=str(target),
            ))

    db_path = data_root / "db" / "qai.db"
    if not db_path.exists():
        report.add_error(f"verify: qai.db missing at {db_path}")
        return
    missing = _check_expected_tables(db_path)
    if missing:
        report.add_error(
            "verify: qai.db missing tables: " + ", ".join(sorted(missing))
        )
    else:
        report.add(InitReportEntry(
            initialiser="data_dir.verify_qai_db",
            location="data_db_qai",
            target=str(db_path),
            note=f"{len(EXPECTED_TABLES)} expected tables present",
        ))

    # Confirm all SQL migrations were applied.
    if sql_migrations_dir.is_dir():
        expected_migration_ids = {
            p.stem for p in sql_migrations_dir.iterdir()
            if p.suffix == ".sql"
        }
        with sqlite3.connect(str(db_path)) as conn:
            try:
                rows = conn.execute(
                    "SELECT id FROM _qai_schema_migrations"
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                report.add_error(f"verify: cannot read _qai_schema_migrations: {exc}")
                return
        applied = {r[0] for r in rows}
        unapplied = expected_migration_ids - applied
        if unapplied:
            report.add_error(
                "verify: pending migrations: " + ", ".join(sorted(unapplied))
            )
        else:
            report.add(InitReportEntry(
                initialiser="data_dir.verify_migrations",
                location="data_db_migration",
                target="all applied",
                note=f"{len(applied)} migration(s)",
            ))

    user_cfg = data_root / "user_config.toml"
    if not user_cfg.exists():
        report.add_error(f"verify: user_config.toml missing at {user_cfg}")
    else:
        try:
            import tomllib
            tomllib.loads(user_cfg.read_text(encoding="utf-8"))
            report.add(InitReportEntry(
                initialiser="data_dir.verify_user_config",
                location="data_user_config",
                target=str(user_cfg),
            ))
        except Exception as exc:  # noqa: BLE001 — verify must surface anything
            report.add_error(f"verify: user_config.toml invalid TOML: {exc}")


__all__ = ["EXPECTED_DIRS", "EXPECTED_TABLES", "RunResult", "run"]
