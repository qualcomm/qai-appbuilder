# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Concrete :class:`WorkspaceInitializerPort` adapter.

Equivalent to ``features/model-builder/scripts/qai_workspace_init.py``:
creates the canonical Model Builder workspace skeleton at
``C:/WoS_AI/<model_name>/`` with an ``output/`` subdir and a minimal
``plan.md`` placeholder so the conversion pipeline has somewhere to
write its outputs.

Idempotent: calling :meth:`init` on an existing workspace adds only
the missing pieces; it never overwrites a populated ``plan.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qai.model_builder.application.use_cases.init_workspace import (
    WorkspaceInitializerPort,
)

__all__ = ["FileSystemWorkspaceInitializer"]


_PLAN_MD_TEMPLATE = """# {model_name} — Model Builder plan

<!--
This file holds the per-model conversion plan. Fields under
"## Config" are read by the export pipeline; keep the
``KEY = value`` shape verbatim.
-->

## Config

```
MODEL_NAME    = {model_name}
PRECISION     = {precision}
OUTPUT_DIR    = {output_dir}
TARGET_ARCH   = windows-aarch64
END_TIME      =
WORK_TIME     =
```

## Notes

(Filled in by the agent during conversion.)
"""


@dataclass(slots=True)
class FileSystemWorkspaceInitializer:
    """Bootstrap a Model Builder workspace skeleton.

    ``wos_ai_root`` defaults to ``C:/WoS_AI`` for parity with the
    legacy script; tests pass a ``tmp_path``.
    """

    wos_ai_root: Path = Path("C:/WoS_AI")

    async def init(
        self,
        *,
        workdir: Path,
        model_name: str,
        precisions: tuple[str, ...] = (),
    ) -> None:
        # Path-traversal guard same as the workspace reader.
        try:
            resolved = workdir.resolve(strict=False)
            wos_root_resolved = self.wos_ai_root.resolve(strict=False)
        except OSError:
            resolved, wos_root_resolved = workdir, self.wos_ai_root
        try:
            resolved.relative_to(wos_root_resolved)
        except ValueError as exc:
            raise ValueError(
                f"workdir must be under {wos_root_resolved}: got {resolved}"
            ) from exc

        resolved.mkdir(parents=True, exist_ok=True)
        (resolved / "output").mkdir(parents=True, exist_ok=True)

        plan_path = resolved / "plan.md"
        if plan_path.is_file():
            return
        precision = (precisions[0] if precisions else "fp16").lower()
        plan_path.write_text(
            _PLAN_MD_TEMPLATE.format(
                model_name=model_name,
                precision=precision,
                output_dir=str(resolved / "output"),
            ),
            encoding="utf-8",
        )
