# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CLI entry point for the install pipeline orchestrator (PR-064).

Single command that chains all four S6 stages (PR-060..063) into the
canonical install used by the release script::

    python -m scripts.init.install \\
        --factory-source        <prefix>/factory/_source \\
        --factory-source-data   <prefix>/legacy/data \\
        --factory-root          <prefix>/defaults \\
        --data-root              <prefix>/data \\
        --sql-migrations         src/qai/platform/persistence/migrations_sql \\
        --secret-backend         auto \\
        [--dry-run | --apply | --verify] \\
        [--skip compile_factory,...] \\
        [--timestamp 2026-01-01T00-00-00Z] \\
        [--json]

Production installs ship a pre-built ``factory/`` bundle and
therefore omit ``--factory-source``; the orchestrator auto-skips
the compile_factory stage in that case (or the operator may pass
``--skip compile_factory`` to be explicit).

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
import json as _json
import sys
import traceback
from pathlib import Path
from typing import Sequence

from qai.platform.logging import configure_logging

from tools.init._common.modes import VALID_MODES, parse_mode
from tools.init.install import run
from tools.init.install.runner import STAGE_NAMES

_VALID_BACKENDS: tuple[str, ...] = ("auto", "keyring", "file", "null")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install",
        description=(
            "Run the canonical QAI install pipeline: "
            "compile_factory -> data_dir -> seed_defaults -> "
            "secret_bootstrap -> edition_secrets. Idempotent; safe to re-run."
        ),
    )
    parser.add_argument(
        "--factory-source",
        type=Path,
        default=None,
        help=(
            "Factory defaults source directory (factory/_source/) consumed "
            "by the compile_factory stage. Optional: when omitted (or "
            "path missing) compile_factory is auto-skipped."
        ),
    )
    parser.add_argument(
        "--factory-source-data",
        type=Path,
        default=None,
        help=(
            "Legacy data/ directory consumed by compile_factory (alongside "
            "--factory-source). Optional: same auto-skip rule."
        ),
    )
    parser.add_argument(
        "--factory-root",
        type=Path,
        required=True,
        help=(
            "Path to the factory/ bundle. compile_factory writes here; "
            "downstream stages read from here."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Target data/ directory (created if absent).",
    )
    parser.add_argument(
        "--sql-migrations",
        type=Path,
        required=True,
        help=(
            "Directory containing NNN_*.sql migration files "
            "(typically src/qai/platform/persistence/migrations_sql)."
        ),
    )
    parser.add_argument(
        "--secret-backend",
        choices=_VALID_BACKENDS,
        default="auto",
        help=(
            "SecretStore backend selector for PR-063: "
            "auto (keyring with file fallback), keyring, file, or null."
        ),
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help=(
            "Optional UTC timestamp string for deterministic backup "
            "directory names in compile_factory apply mode."
        ),
    )
    parser.add_argument(
        "--skip",
        type=str,
        default="",
        help=(
            "CSV of stage names to skip. Valid: "
            + ", ".join(STAGE_NAMES)
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_const", dest="mode", const="dry-run",
        help="(default) Plan every stage without writing.",
    )
    mode.add_argument(
        "--apply",
        action="store_const", dest="mode", const="apply",
        help="Run all stages; stop on first failure.",
    )
    mode.add_argument(
        "--verify",
        action="store_const", dest="mode", const="verify",
        help="Run every stage's verify; never short-circuit.",
    )
    parser.set_defaults(mode="dry-run")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSONL line per stage on stdout.",
    )
    return parser


def _resolve_secret_backend(name: str, data_root: Path):
    """Translate ``--secret-backend`` choice into an opaque store handle.

    ``auto`` is signalled by passing ``None`` so PR-063 builds the
    default store itself; the explicit choices construct the matching
    store eagerly so PR-063 receives a ready-to-use object.

    On Linux, ``auto`` is transparently rewritten to ``file`` before the
    store is constructed, so the factory's ``prefer="auto"`` path (which
    already applies the same Linux-first-file logic) is never reached
    with a stale ``None`` sentinel that would re-trigger keyring probing
    inside ``tools/init/secret_bootstrap/runner.py``.
    """
    import sys as _sys

    if name == "auto" and _sys.platform == "linux":
        name = "file"  # skip keyring probe on Linux (see factory.py)
    if name == "auto":
        return None
    # Lazy imports — keep the CLI fast when the user only needs --help.
    from qai.platform.config.paths import DataPaths
    from qai.platform.persistence.secrets import (
        NullSecretStore,
        build_secret_store,
    )

    if name == "null":
        return NullSecretStore()
    data_paths = DataPaths(data_root)
    return build_secret_store(data_paths=data_paths, prefer=name)


def _resolve_is_internal(data_root: Path) -> bool:
    """Resolve the runtime edition (internal vs external) for install.

    Uses the app's standard edition resolution path
    (``load_settings(repo_root=...)`` reads ``<repo_root>/build_info.json``
    with NO environment variable). The repo/artifact root in the install
    context is ``data_root.parent`` (CLI convention ``--data-root
    <repo_root>/data``; release smoke uses ``<output_dir>/_smoke_data`` whose
    parent is the artifact root carrying the external ``build_info.json``).

    Degrades to ``True`` (internal / full feature set) on any failure — same
    default as ``Settings.edition`` for the dev source tree. This gates the
    internal-only ``edition_secrets`` stage so an external artifact never
    provisions internal factory credentials.
    """
    try:
        from qai.platform.config.settings import load_settings

        repo_root = data_root.resolve().parent
        return bool(load_settings(repo_root=repo_root).is_internal)
    except Exception:  # noqa: BLE001 — never let edition resolution abort install
        return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    configure_logging(stream=sys.stderr)

    try:
        mode = parse_mode(args.mode)
    except ValueError:
        parser.error(f"--mode must be one of {VALID_MODES}, got {args.mode!r}")
        return 2  # not reached

    # Normalise --skip CSV.
    skip_stages: tuple[str, ...] = tuple(
        s.strip() for s in args.skip.split(",") if s.strip()
    )
    invalid = [s for s in skip_stages if s not in STAGE_NAMES]
    if invalid:
        parser.error(
            f"--skip contains unknown stage(s) {invalid}; "
            f"valid: {list(STAGE_NAMES)}"
        )
        return 2  # not reached

    if not args.sql_migrations.exists() or not args.sql_migrations.is_dir():
        sys.stderr.write(
            f"error: --sql-migrations not a directory: {args.sql_migrations}\n"
        )
        return 3

    # Resolve secret backend.
    try:
        secret_backend = _resolve_secret_backend(
            args.secret_backend, args.data_root
        )
    except Exception as exc:  # noqa: BLE001 — surface backend init failure
        traceback.print_exc()
        sys.stderr.write(
            f"error: cannot build secret backend {args.secret_backend!r}: {exc}\n"
        )
        return 4

    # Resolve the runtime edition (internal vs external) the same way the
    # app does: ``load_settings(repo_root=...)`` reads <repo_root>/build_info.json
    # (NO env var). In the install context the repo/artifact root is the parent
    # of --data-root (CLI convention: --data-root <repo_root>/data; the release
    # smoke uses <output_dir>/_smoke_data whose parent is the artifact root that
    # holds the external build_info.json). Falls back to internal on any failure
    # (mirrors Settings.edition default for the dev source tree). Used to gate
    # the internal-only edition_secrets stage.
    is_internal = _resolve_is_internal(args.data_root)

    try:
        result = run(
            mode=mode,
            factory_source=args.factory_source,
            factory_source_data=args.factory_source_data,
            factory_root=args.factory_root,
            data_root=args.data_root,
            sql_migrations_dir=args.sql_migrations,
            secret_backend=secret_backend,
            timestamp=args.timestamp,
            skip_stages=skip_stages,
            is_internal=is_internal,
        )
    except OSError as exc:
        traceback.print_exc()
        sys.stderr.write(f"error: apply-time IO error: {exc}\n")
        return 4

    if args.json:
        for stage in result.stages:
            line = {
                "_kind": "install_stage",
                "name": stage.name,
                "exit_code": stage.exit_code,
                "error_count": stage.error_count,
                "summary": stage.report_summary,
            }
            sys.stdout.write(_json.dumps(line, ensure_ascii=False) + "\n")
        # Final overall line so consumers can detect the run end without
        # having to count stages.
        sys.stdout.write(_json.dumps({
            "_kind": "install_overall",
            "mode": result.mode,
            "exit_code": result.exit_code,
            "stages": len(result.stages),
        }, ensure_ascii=False) + "\n")
    else:
        # Header includes the orchestrator-level paths so the output is
        # self-contained.
        sys.stdout.write(f"# install ({result.mode})\n")
        sys.stdout.write(f"  data_root     = {args.data_root}\n")
        sys.stdout.write(f"  factory_root = {args.factory_root}\n")
        sys.stdout.write(f"  stages        = {len(result.stages)}\n")
        ok = result.exit_code == 0
        sys.stdout.write(f"  overall       = {'ok' if ok else 'FAIL'}\n")
        sys.stdout.write("\n")
        width = max((len(s.name) for s in result.stages), default=0)
        for idx, stage in enumerate(result.stages, start=1):
            sys.stdout.write(
                f"  [{idx}] {stage.name.ljust(width)}  "
                f"exit={stage.exit_code}  errors={stage.error_count}\n"
            )
        # Print per-stage detailed summaries (errors first if any).
        for stage in result.stages:
            if stage.error_count or mode == "verify":
                sys.stdout.write("\n")
                sys.stdout.write(stage.report_summary)
                if not stage.report_summary.endswith("\n"):
                    sys.stdout.write("\n")

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
