# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``scripts.release`` — Clean-Cutover release packaging (PR-074).

This package replaces the legacy ``build_external_release.py`` /
``check_external_release.py`` pair (S0 inventory entry; v2.7 §1
"zero legacy coexistence" — those files are deleted in S8 PR-081).

Public surface:

* ``python -m scripts.release [--apply | --dry-run] [--output-dir DIR]``
  drives the 10-stage build pipeline (clean → frontend_build →
  factory → assemble → write_build_info → sanitize_factory →
  finalize_opensource → check_release → install_smoke → archive).
* :func:`build.run` is the programmatic entry used by integration
  tests under ``tests/integration/release/``.
* :func:`check_release.run` is the standalone black-/whitelist verifier;
  CI can call it on an already-assembled tree without rebuilding.

The release artifact contract is **declarative**, encoded in
``manifest.toml`` next to this package. ``build.py`` reads that manifest
at runtime and never hard-codes path lists.
"""

from __future__ import annotations

__all__: list[str] = []
