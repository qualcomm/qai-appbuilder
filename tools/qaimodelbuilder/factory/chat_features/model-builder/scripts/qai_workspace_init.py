# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""``qai_workspace_init.py`` — bootstrap a clean Model Builder workspace.

V1-parity entry point for the SKILL workflow.  The WebUI / Desktop chat
``model-builder`` flow drives this script **exactly like V1** —

    <python_x64_venv>\\Scripts\\python.exe \\
        factory\\chat_features\\model-builder\\scripts\\qai_workspace_init.py <model_name>

— so the command never depends on a ``qai`` console-script being on
``PATH`` (the V2-only ``qai`` CLI is intentionally not used by the
chat workflow; the SKILL stays byte-for-byte aligned with V1's
absolute-python-path + script-file convention).

This is a **thin forwarder**: it does no business logic of its own.
The real work lives in the V2 Clean-Architecture use case
(:class:`qai.model_builder.application.use_cases.init_workspace.InitWorkspaceUseCase`),
reached through the shared composition root
``scripts.build.model_builder_cli.workspace_init_main``.  Reusing that
single root (instead of re-implementing V1's monolithic script body)
keeps the architecture clean while preserving V1's user-visible command
form + behaviour.

A V1 ``--no-templates`` flag is accepted and ignored for command
compatibility (template seeding is governed by the V2 workspace
initializer adapter, not a script flag).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root + ``src`` importable regardless of the subprocess CWD /
# PYTHONPATH (chat ``exec`` spawns this without setting PYTHONPATH).  This file
# lives at ``<repo>/factory/chat_features/model-builder/scripts/`` → four
# parents up is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT / "src", _REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from scripts.build.model_builder_cli import workspace_init_main  # noqa: E402


def main() -> int:
    # Drop the V1-compatible ``--no-templates`` flag the V2 CLI does not model;
    # forward the rest (``<model_name>`` positional + any V2 flags) verbatim.
    argv = [a for a in sys.argv[1:] if a != "--no-templates"]
    return workspace_init_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
