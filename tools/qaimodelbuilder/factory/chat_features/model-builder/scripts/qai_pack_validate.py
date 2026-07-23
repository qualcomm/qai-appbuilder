# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""``qai_pack_validate.py`` — structurally validate a candidate Pack.

V1-parity entry point for the SKILL workflow.  Driven exactly like V1 —

    <python_x64_venv>\\Scripts\\python.exe \\
        factory\\chat_features\\model-builder\\scripts\\qai_pack_validate.py ^
          C:\\WoS_AI\\<model_name>\\app_pack

— so the command never depends on a ``qai`` console-script being on
``PATH``.

Thin forwarder: the real validation logic lives in the V2 use case
:class:`qai.model_builder.application.use_cases.validate_pack.ValidatePackUseCase`,
reached through ``scripts.build.model_builder_cli.pack_validate_main``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT / "src", _REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from scripts.build.model_builder_cli import pack_validate_main  # noqa: E402


def main() -> int:
    return pack_validate_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
