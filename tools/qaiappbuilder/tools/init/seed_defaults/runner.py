# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation of the seed-defaults loader (PR-062)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from qai.platform.logging import get_logger

from .._common.modes import Mode
from .._common.report import InitReport, InitReportEntry

_LOGGER = get_logger("qai.init.seed_defaults")


# Staging JSONL → table dispatch table.  Each entry declares:
#   - the JSONL filename under ``factory/db_staging/``
#   - the target table name in ``qai.db``
#   - the column list (order = INSERT order)
#   - a transform function (jsonl row dict → tuple of column values),
#     which also takes care of mapping the PR-060 ``_kind`` envelope
#     to bare table columns.
_STAGING: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "cloud_models_to_model_catalog_entry.jsonl",
        "model_catalog_entry",
        (
            "id", "name", "provider", "source_url", "description",
            "taxonomy_tags_json", "current_version_id",
            "created_at", "updated_at",
        ),
    ),
    (
        "kv_user_prefs.jsonl",
        "kv_user_prefs",
        ("key", "value_json", "updated_at"),
    ),
    (
        # Built-in roster-template presets (reusable discussion "teams"):
        # seeded once into chat_roster_template (migration 038). Pure V2
        # enhancement. ``id`` first = PK used for idempotency probing. Built-in
        # member ``model_id`` is left empty so the user picks a model on import
        # (and so external edition sanitisation never has to touch these rows).
        "chat_roster_template.jsonl",
        "chat_roster_template",
        (
            "id", "name", "description", "members_json", "is_builtin",
            "created_at", "updated_at",
            # Multi-language (i18n) sidecar columns (migration 056): per-locale
            # {"en":..,"zh-CN":..,"zh-TW":..} maps; NULL for custom rows → fall
            # back to the canonical single-language columns above.
            "name_i18n_json", "description_i18n_json", "members_i18n_json",
        ),
    ),
    (
        # Built-in agent-template presets (reusable SINGLE roles; §27 three-tier
        # template system): seeded once into chat_agent_template (migration
        # 039). Pure V2 enhancement. ``id`` first = PK used for idempotency
        # probing. Built-in ``model_id`` is left null so the user picks a model
        # on import (and so external edition sanitisation never touches these).
        "chat_agent_template.jsonl",
        "chat_agent_template",
        (
            "id", "name", "description", "display_name", "model_id", "persona",
            "config_json", "is_builtin", "created_at", "updated_at",
            # Multi-language (i18n) sidecar columns (migration 056).
            "name_i18n_json", "description_i18n_json",
            "display_name_i18n_json", "persona_i18n_json",
        ),
    ),
    (
        # Built-in mode-template presets (collaboration modes 讨论/评审/辩论/实施;
        # §26/§27 three-tier template system): seeded once into
        # chat_mode_template (migration 040). Pure V2 enhancement. ``id`` first =
        # PK used for idempotency probing.
        "chat_mode_template.jsonl",
        "chat_mode_template",
        (
            "id", "name", "description", "framing", "tool_policy_json",
            "flow_policy_json", "is_builtin", "created_at", "updated_at",
            # Multi-language (i18n) sidecar columns (migration 056).
            "name_i18n_json", "description_i18n_json", "framing_i18n_json",
        ),
    ),
)


@dataclass
class RunResult:
    mode: Mode
    report: InitReport
    rows_inserted: dict[str, int] = field(default_factory=dict)
    rows_skipped: dict[str, int] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0 if self.report.is_ok() else 1


def run(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
) -> RunResult:
    """Load PR-060 staging JSONL files into ``data/db/qai.db``."""
    report = InitReport(
        initialiser="seed_defaults",
        mode=mode,
        data_root=str(data_root),
        factory_root=str(factory_root),
    )
    rows_inserted: dict[str, int] = {}
    rows_skipped: dict[str, int] = {}

    db_path = data_root / "db" / "qai.db"
    staging_dir = factory_root / "db_staging"

    if mode == "verify":
        _verify(
            db_path=db_path,
            staging_dir=staging_dir,
            report=report,
        )
        return RunResult(mode=mode, report=report)

    # Apply mode requires both inputs already in place; dry-run can plan
    # against a not-yet-applied tree (the operator wants to see the full
    # pipeline plan before any stage runs).
    if mode == "apply":
        if not db_path.exists():
            report.add_error(
                f"qai.db missing at {db_path}; run init_data_dir first"
            )
            return RunResult(mode=mode, report=report)
        if not staging_dir.is_dir():
            report.add_error(
                f"staging dir missing at {staging_dir}; run compile_factory first"
            )
            return RunResult(mode=mode, report=report)
    elif mode == "dry-run":
        # Surface as informational entries, not errors, so the pipeline
        # dry-run continues through subsequent stages.
        if not db_path.exists():
            report.add(InitReportEntry(
                initialiser="seed_defaults.precondition",
                location="noop_dryrun",
                target=str(db_path),
                note="qai.db not yet created (will exist after init_data_dir apply)",
            ))
        if not staging_dir.is_dir():
            report.add(InitReportEntry(
                initialiser="seed_defaults.precondition",
                location="noop_dryrun",
                target=str(staging_dir),
                note="staging dir not yet created (will exist after compile_factory apply)",
            ))

    for filename, table, columns in _STAGING:
        path = staging_dir / filename
        if not path.exists():
            report.add(InitReportEntry(
                initialiser=f"seed_defaults.{table}",
                location="skipped_empty",
                target=str(path),
                note="staging file absent",
            ))
            rows_inserted[table] = 0
            rows_skipped[table] = 0
            continue

        rows = list(_iter_jsonl(path))
        if mode == "dry-run":
            report.add(InitReportEntry(
                initialiser=f"seed_defaults.{table}",
                location="qai_db_table_seed",
                target=table,
                rows=len(rows),
                note=f"dry-run: would insert from {filename}",
            ))
            rows_inserted[table] = 0
            rows_skipped[table] = len(rows)
            continue

        # apply
        inserted, skipped = _insert_rows(
            db_path=db_path,
            table=table,
            columns=columns,
            rows=rows,
            staging_filename=filename,
            report=report,
        )
        rows_inserted[table] = inserted
        rows_skipped[table] = skipped
        report.add(InitReportEntry(
            initialiser=f"seed_defaults.{table}",
            location="qai_db_table_seed",
            target=table,
            rows=inserted,
            note=(
                f"from {filename}; {inserted} inserted, "
                f"{skipped} skipped (already present)"
            ),
        ))

    return RunResult(
        mode=mode,
        report=report,
        rows_inserted=rows_inserted,
        rows_skipped=rows_skipped,
    )


# ---------------------------------------------------------------------------
# JSONL → INSERT
# ---------------------------------------------------------------------------


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{lineno}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(obj, dict):
                raise ValueError(
                    f"{path}:{lineno}: not a JSON object"
                )
            yield obj


def _row_to_tuple(
    row: dict[str, Any], columns: tuple[str, ...]
) -> tuple[Any, ...]:
    """Project ``row`` to a column-ordered tuple.

    Missing columns default to ``None``. PR-060 envelope keys (``_kind``,
    ``_provenance``) are intentionally ignored; the SQLite schema does
    not have columns for them and they are auxiliary metadata.
    """
    return tuple(row.get(col) for col in columns)


def _insert_rows(
    *,
    db_path: Path,
    table: str,
    columns: tuple[str, ...],
    rows: list[dict[str, Any]],
    staging_filename: str,
    report: InitReport,
) -> tuple[int, int]:
    """Return ``(inserted, skipped)``.

    Strategy: a per-row "does PK already exist?" probe followed by a
    plain ``INSERT`` (NOT ``INSERT OR IGNORE``). The two-step variant
    is intentional — ``INSERT OR IGNORE`` silently absorbs *every*
    constraint violation including CHECK and NOT NULL, so a legitimately
    bad staging row (e.g. ``provider="invalid_enum"`` against the
    ``model_catalog_entry`` provider CHECK) would be silently dropped.
    The two-step variant lets idempotent re-runs skip cleanly while
    surfacing schema-violating rows as real errors in the report.
    """
    pk = columns[0]
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    insert_sql = (
        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
    )
    exists_sql = f"SELECT 1 FROM {table} WHERE {pk} = ? LIMIT 1"

    # Idempotent forward-compatible BACKFILL (AGENTS.md §8 rule 6): when a
    # built-in preset row was seeded by an OLDER install (before a later
    # migration tail-appended new ``*_i18n_json`` columns), the plain "PK
    # exists → skip" idempotency below would leave those new columns NULL
    # forever, so a re-run of the installer never delivers the multi-language
    # text to already-provisioned users. To fix that WITHOUT ever clobbering
    # user edits, we backfill ONLY columns whose name ends in ``_i18n_json``
    # (pure additive translation sidecars), ONLY when the DB value is currently
    # NULL and the staging row provides a value. The per-column
    # ``WHERE <col> IS NULL`` guard makes it strictly monotonic (NULL→value,
    # never value→other), and these columns only exist on built-in template
    # tables, so user-authored rows (which carry their own text in the base
    # columns and NULL i18n) are untouched.
    i18n_columns = tuple(c for c in columns if c.endswith("_i18n_json"))

    with sqlite3.connect(str(db_path)) as conn:
        # Ensure busy timeout so concurrent installs (rare but possible)
        # don't immediately fail.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        inserted = 0
        skipped = 0
        backfilled = 0
        for row in rows:
            try:
                values = _row_to_tuple(row, columns)
            except Exception as exc:  # noqa: BLE001
                report.add_error(
                    f"{staging_filename}: row projection failed: {exc}"
                )
                continue
            pk_value = row.get(pk)
            if pk_value is None:
                report.add_error(
                    f"{table}: staging row missing primary key column "
                    f"{pk!r}"
                )
                continue
            # Idempotency probe: skip rows already present.
            try:
                existing = cur.execute(exists_sql, (pk_value,)).fetchone()
            except sqlite3.OperationalError as exc:
                report.add_error(
                    f"{table}: operational error checking {pk_value!r}: {exc}"
                )
                continue
            if existing is not None:
                # Row already present: skip the INSERT, but idempotently
                # backfill any additive ``*_i18n_json`` sidecar column that is
                # still NULL in the DB yet supplied by staging (AGENTS.md §8
                # rule 6). Monotonic (only NULL→value) and confined to i18n
                # columns, so user data is never overwritten.
                for col in i18n_columns:
                    new_value = row.get(col)
                    if new_value is None:
                        continue
                    try:
                        upd = cur.execute(
                            f"UPDATE {table} SET {col} = ? "
                            f"WHERE {pk} = ? AND {col} IS NULL",
                            (new_value, pk_value),
                        )
                    except (
                        sqlite3.IntegrityError,
                        sqlite3.OperationalError,
                    ) as exc:
                        report.add_error(
                            f"{table}: error backfilling "
                            f"{col!r} for {pk_value!r}: {exc}"
                        )
                        continue
                    if upd.rowcount == 1:
                        backfilled += 1
                skipped += 1
                continue
            try:
                cur.execute(insert_sql, values)
            except sqlite3.IntegrityError as exc:
                report.add_error(
                    f"{table}: integrity error inserting {pk_value!r}: {exc}"
                )
                continue
            except sqlite3.OperationalError as exc:
                report.add_error(
                    f"{table}: operational error inserting "
                    f"{pk_value!r}: {exc}"
                )
                continue
            if cur.rowcount == 1:
                inserted += 1
            else:
                # Defensive: should not happen on a successful INSERT
                # without OR IGNORE.
                report.add_error(
                    f"{table}: INSERT returned rowcount={cur.rowcount} "
                    f"for {pk_value!r}; expected 1"
                )
        conn.commit()
    _LOGGER.info(
        "seed_defaults.applied",
        table=table,
        inserted=inserted,
        skipped=skipped,
        backfilled=backfilled,
    )
    if backfilled:
        report.add(InitReportEntry(
            initialiser=f"seed_defaults.{table}",
            location="qai_db_table_backfill",
            target=table,
            rows=backfilled,
            note=(
                f"backfilled {backfilled} NULL *_i18n_json value(s) on "
                f"pre-existing built-in rows (idempotent forward-compat)"
            ),
        ))
    return inserted, skipped


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _verify(
    *,
    db_path: Path,
    staging_dir: Path,
    report: InitReport,
) -> None:
    if not db_path.exists():
        report.add_error(f"verify: qai.db missing at {db_path}")
        return
    if not staging_dir.is_dir():
        report.add_error(f"verify: staging dir missing at {staging_dir}")
        return

    with sqlite3.connect(str(db_path)) as conn:
        for filename, table, columns in _STAGING:
            path = staging_dir / filename
            if not path.exists():
                report.add(InitReportEntry(
                    initialiser=f"seed_defaults.verify_{table}",
                    location="skipped_empty",
                    target=str(path),
                    note="staging file absent (no rows to verify)",
                ))
                continue
            staged_rows = list(_iter_jsonl(path))
            if not staged_rows:
                report.add(InitReportEntry(
                    initialiser=f"seed_defaults.verify_{table}",
                    location="skipped_empty",
                    target=str(path),
                    note="0 staged rows",
                ))
                continue
            # Use the first column as the lookup key (model_catalog_entry.id
            # / kv_user_prefs.key — both are the primary key).
            pk = columns[0]
            missing: list[str] = []
            for row in staged_rows:
                key_val = row.get(pk)
                if key_val is None:
                    report.add_error(
                        f"verify: {table}: staged row missing PK column "
                        f"{pk!r}"
                    )
                    continue
                cur = conn.execute(
                    f"SELECT 1 FROM {table} WHERE {pk} = ? LIMIT 1",
                    (key_val,),
                )
                if cur.fetchone() is None:
                    missing.append(str(key_val))
            if missing:
                report.add_error(
                    f"verify: {table} missing {len(missing)} of "
                    f"{len(staged_rows)} staged rows; first few: "
                    + ", ".join(missing[:5])
                )
            else:
                report.add(InitReportEntry(
                    initialiser=f"seed_defaults.verify_{table}",
                    location="qai_db_table_seed",
                    target=table,
                    rows=len(staged_rows),
                    note=f"all {len(staged_rows)} staged rows present",
                ))


__all__ = ["RunResult", "run"]
