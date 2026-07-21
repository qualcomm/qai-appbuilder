# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Structured init report.

Symmetric to :class:`tools.build.factory_compiler._common.report.MigrationReport` but
records *creation* events rather than *transformation* mappings.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator, Literal

# Locations a fresh-init action can target.
InitLocation = Literal[
    "data_dir",         # data/ subtree creation
    "data_db_qai",      # data/db/qai.db
    "data_db_migration", # one applied SQL migration
    "data_blobs",       # data/blobs/<context>/ subtree
    "data_secrets",     # data/secrets/ subtree
    "data_user_config", # data/user_config.toml
    "qai_db_table_seed",# row(s) inserted into qai.db table
    "secret_namespace", # SecretStore namespace registered
    "skipped_existing", # already in place (idempotency)
    "skipped_empty",    # source had no rows
    "noop_dryrun",      # dry-run plan entry
]


@dataclass(frozen=True)
class InitReportEntry:
    """One initialisation step."""

    initialiser: str
    """Identifier (e.g. ``"data_dir.create_blob_tree"``)."""

    location: InitLocation
    """Destination kind."""

    target: str
    """Concrete path or table name."""

    rows: int = 0
    """Row count for table seeds; 0 otherwise."""

    note: str = ""


@dataclass
class InitReport:
    """Aggregate of all :class:`InitReportEntry` rows."""

    initialiser: str
    mode: str
    data_root: str = ""
    factory_root: str = ""
    entries: list[InitReportEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, entry: InitReportEntry) -> None:
        self.entries.append(entry)

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def is_ok(self) -> bool:
        return not self.errors

    def iter_by_location(self) -> Iterator[tuple[InitLocation, list[InitReportEntry]]]:
        buckets: dict[InitLocation, list[InitReportEntry]] = {}
        for entry in self.entries:
            buckets.setdefault(entry.location, []).append(entry)
        order: tuple[InitLocation, ...] = (
            "data_dir",
            "data_db_qai",
            "data_db_migration",
            "data_blobs",
            "data_secrets",
            "data_user_config",
            "qai_db_table_seed",
            "secret_namespace",
            "skipped_existing",
            "skipped_empty",
            "noop_dryrun",
        )
        for loc in order:
            if loc in buckets:
                yield loc, buckets[loc]

    def to_dict(self) -> dict[str, Any]:
        return {
            "initialiser": self.initialiser,
            "mode": self.mode,
            "data_root": self.data_root,
            "factory_root": self.factory_root,
            "errors": list(self.errors),
            "entries": [asdict(e) for e in self.entries],
        }

    def to_jsonl(self) -> str:
        header = {
            "_kind": "init_report",
            "initialiser": self.initialiser,
            "mode": self.mode,
            "data_root": self.data_root,
            "factory_root": self.factory_root,
            "errors": list(self.errors),
            "entry_count": len(self.entries),
        }
        lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
        for entry in self.entries:
            lines.append(
                json.dumps(
                    {"_kind": "init_entry", **asdict(entry)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return "\n".join(lines) + "\n"

    def render_summary(self) -> str:
        lines: list[str] = [
            f"# {self.initialiser} ({self.mode})",
            f"  data_root     = {self.data_root}",
            f"  factory_root = {self.factory_root}",
            f"  entries       = {len(self.entries)}",
            f"  errors        = {len(self.errors)}",
            "",
        ]
        for loc, group in self.iter_by_location():
            lines.append(f"  [{loc}] ({len(group)})")
            for entry in group:
                rows = f"  rows={entry.rows}" if entry.rows else ""
                note = f"  ({entry.note})" if entry.note else ""
                lines.append(f"    {entry.target}{rows}{note}")
            lines.append("")
        if self.errors:
            lines.append("  ! errors:")
            for err in self.errors:
                lines.append(f"    - {err}")
        return "\n".join(lines)


def merge_reports(
    *,
    initialiser: str,
    mode: str,
    data_root: str,
    factory_root: str,
    parts: Iterable[InitReport],
) -> InitReport:
    out = InitReport(
        initialiser=initialiser,
        mode=mode,
        data_root=data_root,
        factory_root=factory_root,
    )
    for part in parts:
        for entry in part.entries:
            out.entries.append(entry)
        for err in part.errors:
            out.errors.append(err)
    return out


__all__ = [
    "InitLocation",
    "InitReport",
    "InitReportEntry",
    "merge_reports",
]
