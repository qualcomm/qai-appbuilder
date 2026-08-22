# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""One-shot child process: extract a ``.bin`` I/O contract + zero-tensor smoke
test, OUT-OF-PROCESS.

Why this file exists
====================
The export pipeline's I/O-contract probe loads a QNN context binary via
``qai_appbuilder`` and runs a zero-tensor inference (the export quality gate).
Historically this ran **in the API service process**
(``_io_contract_probe.extract_and_smoke_test_contract`` called directly from
``QaiPackExporter._extract_io_contract``). The QNN native library writes to
process ``fd 1`` / ``fd 2`` via C ``printf`` / ``cout`` (e.g.
``[WARNING] Time: model_inference``), so those bytes leaked into the long-lived
service's stdout / stderr and its log file, and a native crash (0xC0000005)
inside the probe would take the whole service down.

This script is the child half of the out-of-process move (Plan B1). It is
launched by absolute path (``python <this> <bin> [--shared-dir <dir>]``) by
``_io_contract_probe.extract_and_smoke_test_contract_subprocess`` in the parent
(service) process.

Isolation contract
==================
* Before importing ANY native library, we arm faulthandler and apply
  fd-level stdout protection via :mod:`qai.platform.process.stdout_guard`
  (the same guard the App Builder ``_runner_bootstrap`` uses): fd 1 -> stderr
  so native ``printf`` goes to stderr (captured by the parent's stderr PIPE,
  never the service log), and ``sys.stdout`` becomes the saved event fd on
  which we write exactly one JSON result envelope.
* stderr is passed through to the parent (crash tracebacks / native noise land
  there for diagnostics, NOT in the service log).

Result envelope (single line on protected stdout)
=================================================
Success::

    {"ok": true, "io_contract": { ...validated contract... }}

Failure::

    {"ok": false, "error_code": "<model_builder.*>", "message": "<detail>"}

``error_code`` mirrors the ``.code`` of the domain exception the in-process
path would have raised, so the parent can reconstruct the identical typed
exception (``MissingQaiAppBuilderError`` / ``SmokeTestFailedError``).

Exit code
=========
* ``0`` on success (a valid ``io_contract`` was produced).
* ``1`` on a handled failure (a well-formed ``ok:false`` envelope was emitted).
* non-zero WITHOUT a valid envelope (e.g. a native segfault) -> the parent
  treats it as a smoke-test failure and attaches the child's stderr tail.
"""

from __future__ import annotations

# NOTE: import ordering is load-bearing. ``qai.platform.process.stdout_guard``
# imports only the standard library, so it is safe to import and run first —
# BEFORE any native library (``qai_appbuilder`` and its QNN DLLs are imported
# lazily inside the pure probe function further down). See the module docstring
# for the ordering rule.
from qai.platform.process.stdout_guard import arm_faulthandler, protect_stdout

arm_faulthandler()
protect_stdout()

import argparse  # noqa: E402 — must follow the stdout guard above
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402


def _emit(envelope: dict) -> None:
    """Write exactly one JSON envelope line to the protected event stdout."""
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="_smoke_probe_child",
        description="Out-of-process QNN .bin I/O contract + zero-tensor smoke test.",
    )
    parser.add_argument("context_bin", help="Absolute path to the .bin context binary.")
    parser.add_argument(
        "--shared-dir",
        default=None,
        help=(
            "Optional dir containing qnn_helper.py / io_validator.py "
            "(App Builder shared runner helpers)."
        ),
    )
    args = parser.parse_args()

    # Import the pure probe logic + typed domain errors. These are pure-Python
    # (no native code) so importing them after the stdout guard is fine and does
    # not risk leaking native output. ``extract_and_smoke_test_contract`` is the
    # very same function the in-process path used, so success/failure semantics
    # are identical — the only change is WHERE it runs.
    try:
        from qai.model_builder.adapters._io_contract_probe import (
            extract_and_smoke_test_contract,
        )
        from qai.model_builder.domain import (
            MissingQaiAppBuilderError,
            SmokeTestFailedError,
        )
    except Exception as exc:  # noqa: BLE001 — bootstrap import failure
        # This is an environment/wiring failure of the CHILD itself (not the
        # probe). Surface it as a smoke-test failure envelope so the parent
        # still gets a typed error rather than an opaque non-zero exit.
        _emit(
            {
                "ok": False,
                "error_code": "model_builder.smoke_test_failed",
                "message": f"child bootstrap import failed: {exc!r}",
            }
        )
        return 1

    shared_dir = Path(args.shared_dir) if args.shared_dir else None
    try:
        contract = extract_and_smoke_test_contract(
            Path(args.context_bin),
            shared_dir=shared_dir,
        )
    except (MissingQaiAppBuilderError, SmokeTestFailedError) as exc:
        # Preserve the exact typed failure the in-process path raised by
        # forwarding the domain ``.code`` + message; the parent rebuilds the
        # same exception class from ``error_code``.
        _emit(
            {
                "ok": False,
                "error_code": getattr(exc, "code", "model_builder.smoke_test_failed"),
                "message": str(exc),
            }
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — any unexpected probe error
        # Map anything unexpected to a smoke-test failure (the in-process path
        # would also have propagated it into the exporter's ``except Exception``
        # branch and, in strict mode, raised). Keep the class name in the
        # message for diagnostics.
        _emit(
            {
                "ok": False,
                "error_code": "model_builder.smoke_test_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1

    _emit({"ok": True, "io_contract": contract})
    return 0


if __name__ == "__main__":
    sys.exit(main())
