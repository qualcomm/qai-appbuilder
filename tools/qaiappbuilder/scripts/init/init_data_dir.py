# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CLI entry point for the ``data/`` directory initialiser (PR-061).

Typical usage on a fresh install::

    python -m scripts.init.init_data_dir \\
        --data-root         <prefix>/data \\
        --factory-root     <prefix>/defaults \\
        --sql-migrations    src/qai/platform/persistence/migrations_sql \\
        [--dry-run | --apply | --verify] \\
        [--json]

Exit codes
----------
* 0 — success
* 1 — verify failed OR an apply step reported errors
* 2 — argparse rejected the arguments
* 3 — required path does not exist
* 4 — IO / database error during apply
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Sequence

from qai.platform.logging import configure_logging

from tools.init._common.modes import VALID_MODES, parse_mode
from tools.init.data_dir import run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="init_data_dir",
        description=(
            "Initialise a fresh QAI data/ directory from scratch. "
            "Creates the directory tree, applies the 7 SQL migrations to "
            "a new qai.db, and copies the bundled factory/user_config.toml. "
            "No legacy data is read; refactor-plan §9.4.10."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Target data/ directory (will be created if absent).",
    )
    parser.add_argument(
        "--factory-root",
        type=Path,
        default=None,
        help=(
            "Path to factory/ bundle from compile_factory. Optional: when "
            "absent a minimal placeholder user_config.toml is written."
        ),
    )
    parser.add_argument(
        "--sql-migrations",
        type=Path,
        required=True,
        help=(
            "Path to the directory containing NNN_*.sql migration files "
            "(typically src/qai/platform/persistence/migrations_sql)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_const", dest="mode", const="dry-run",
        help="(default) Plan the steps without writing.",
    )
    mode.add_argument(
        "--apply",
        action="store_const", dest="mode", const="apply",
        help="Create directories, build qai.db, apply migrations.",
    )
    mode.add_argument(
        "--verify",
        action="store_const", dest="mode", const="verify",
        help="Re-check on-disk state against the manifest.",
    )
    parser.set_defaults(mode="dry-run")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSONL report on stdout.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    configure_logging(stream=sys.stderr)

    try:
        mode = parse_mode(args.mode)
    except ValueError:
        parser.error(f"--mode must be one of {VALID_MODES}, got {args.mode!r}")
        return 2  # not reached

    if not args.sql_migrations.exists() or not args.sql_migrations.is_dir():
        sys.stderr.write(
            f"error: --sql-migrations not a directory: {args.sql_migrations}\n"
        )
        return 3

    if (
        args.factory_root is not None
        and not args.factory_root.is_dir()
        and mode == "apply"
    ):
        # apply tolerates missing defaults (placeholder user_config.toml
        # is generated); but if the user explicitly pointed at a
        # non-existing path we want to fail fast.
        sys.stderr.write(
            f"error: --factory-root does not exist: {args.factory_root}\n"
        )
        return 3

    try:
        result = run(
            mode=mode,
            data_root=args.data_root,
            factory_root=args.factory_root,
            sql_migrations_dir=args.sql_migrations,
        )
    except OSError as exc:
        traceback.print_exc()
        sys.stderr.write(f"error: apply-time IO error: {exc}\n")
        return 4

    if args.json:
        sys.stdout.write(result.report.to_jsonl())
    else:
        sys.stdout.write(result.report.render_summary())
        sys.stdout.write("\n")
        if result.files_written:
            sys.stdout.write(
                f"Wrote {len(result.files_written)} files/dirs under {args.data_root}.\n"
            )

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
