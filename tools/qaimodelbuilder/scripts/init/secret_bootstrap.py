# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CLI entry point for the secret-namespace bootstrapper (PR-063).

Typical usage on a fresh install (run after init_data_dir +
seed_defaults)::

    python -m scripts.init.secret_bootstrap \\
        --data-root      <prefix>/data \\
        --factory-root  <prefix>/defaults \\
        --secret-backend auto \\
        [--dry-run | --apply | --verify] \\
        [--json]

What it does
------------
Reads ``factory/secrets_manifest.json`` (compile_factory output) and
registers each ``(service, key)`` namespace with the configured
:class:`SecretStore` using an empty placeholder value. The user fills
the real value via the UI post-install. Re-running ``--apply`` is
idempotent — already-registered namespaces are skipped (the
:class:`SecretStore.exists` check ensures real credentials are never
clobbered).

``--secret-backend null`` selects an in-memory :class:`NullSecretStore`,
which is intended for tests and CI smoke runs that must not touch the
host's keyring or write encrypted files. Note that an in-memory store
is per-process: a follow-up ``--verify`` invocation in a fresh process
won't see it.

Exit codes match :mod:`scripts.init.init_data_dir`.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Sequence

from qai.platform.logging import configure_logging
from qai.platform.persistence.secrets import NullSecretStore, SecretStore

from tools.init._common.modes import VALID_MODES, parse_mode
from tools.init.secret_bootstrap import run

_BACKEND_CHOICES = ("auto", "keyring", "file", "null")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secret_bootstrap",
        description=(
            "Register secret namespaces declared in "
            "factory/secrets_manifest.json into the configured "
            "SecretStore. Idempotent: existing namespaces are skipped, "
            "never overwritten."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="data/ directory (PR-061 must already have created it).",
    )
    parser.add_argument(
        "--factory-root",
        type=Path,
        required=True,
        help=(
            "factory/ bundle directory (must contain "
            "secrets_manifest.json from compile_factory)."
        ),
    )
    parser.add_argument(
        "--secret-backend",
        choices=_BACKEND_CHOICES,
        default="auto",
        help=(
            "Backend selection: 'auto' (default; OS keyring then file "
            "fallback), 'keyring' (force OS keyring), 'file' (force "
            "encrypted-file fallback), or 'null' (in-memory; tests "
            "only — does not touch the OS keyring or filesystem)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_const", dest="mode", const="dry-run",
        help="(default) Plan registrations without touching the store.",
    )
    mode.add_argument(
        "--apply",
        action="store_const", dest="mode", const="apply",
        help="Register each namespace with an empty placeholder.",
    )
    mode.add_argument(
        "--verify",
        action="store_const", dest="mode", const="verify",
        help="Confirm every manifest namespace exists in the store.",
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

    # CRITICAL: keep stdout clean for --json by routing logs to stderr.
    configure_logging(stream=sys.stderr)

    try:
        mode = parse_mode(args.mode)
    except ValueError:
        parser.error(f"--mode must be one of {VALID_MODES}, got {args.mode!r}")
        return 2  # not reached

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

    secret_backend: SecretStore | None
    if args.secret_backend == "null":
        secret_backend = NullSecretStore()
    elif args.secret_backend == "auto":
        # Let the runner build a backend via build_secret_store(prefer="auto").
        secret_backend = None
    else:
        # 'keyring' or 'file' — build here so we can pass the explicit
        # preference. We import lazily to keep --null runs free of any
        # optional dependency.
        from qai.platform.config.paths import DataPaths
        from qai.platform.persistence.secrets import build_secret_store
        try:
            secret_backend = build_secret_store(
                data_paths=DataPaths(args.data_root),
                prefer=args.secret_backend,
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"error: cannot build --secret-backend {args.secret_backend!r}: {exc}\n"
            )
            return 4

    try:
        result = run(
            mode=mode,
            data_root=args.data_root,
            factory_root=args.factory_root,
            secret_backend=secret_backend,
        )
    except OSError as exc:
        traceback.print_exc()
        sys.stderr.write(f"error: apply-time IO error: {exc}\n")
        return 4
    except RuntimeError as exc:
        # Raised by the runner when the manifest leaks a non-redacted
        # value — security invariant.
        traceback.print_exc()
        sys.stderr.write(f"error: {exc}\n")
        return 4

    if args.json:
        sys.stdout.write(result.report.to_jsonl())
    else:
        sys.stdout.write(result.report.render_summary())
        sys.stdout.write("\n")
        if mode == "apply":
            sys.stdout.write(
                f"namespaces: registered={result.namespaces_registered}, "
                f"skipped={result.namespaces_skipped}\n"
            )

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
