# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CLI entry point for the seed-defaults loader (PR-062).

Typical usage on a fresh install (run after init_data_dir)::

    python -m scripts.init.seed_defaults \\
        --data-root      <prefix>/data \\
        --factory-root  <prefix>/defaults \\
        [--dry-run | --apply | --verify]

Exit codes match :mod:`scripts.init.init_data_dir`.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Sequence

from qai.platform.logging import configure_logging

from tools.init._common.modes import VALID_MODES, parse_mode
from tools.init.seed_defaults import run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_defaults",
        description=(
            "Load PR-060 staging JSONL files into qai.db. Idempotent: "
            "re-running this on a populated database silently skips "
            "already-present rows."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="data/ directory (must already contain db/qai.db; PR-061).",
    )
    parser.add_argument(
        "--factory-root",
        type=Path,
        required=True,
        help=(
            "factory/ bundle directory (must contain db_staging/*.jsonl)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_const", dest="mode", const="dry-run",
        help="(default) Plan inserts without writing.",
    )
    mode.add_argument(
        "--apply",
        action="store_const", dest="mode", const="apply",
        help="INSERT OR IGNORE staged rows into qai.db.",
    )
    mode.add_argument(
        "--verify",
        action="store_const", dest="mode", const="verify",
        help="Confirm every staged PK has a matching DB row.",
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
        return 2

    if not args.data_root.exists() or not args.data_root.is_dir():
        sys.stderr.write(
            f"error: --data-root not a directory: {args.data_root}\n"
        )
        return 3
    if not args.factory_root.exists() or not args.factory_root.is_dir():
        sys.stderr.write(
            f"error: --factory-root not a directory: {args.factory_root}\n"
        )
        return 3

    try:
        result = run(
            mode=mode,
            data_root=args.data_root,
            factory_root=args.factory_root,
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
        if mode == "apply":
            for table, count in result.rows_inserted.items():
                sys.stdout.write(
                    f"{table}: inserted {count}, skipped {result.rows_skipped[table]}\n"
                )

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
