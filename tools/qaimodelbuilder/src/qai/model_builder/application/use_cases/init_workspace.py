# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``InitWorkspaceUseCase`` — bootstrap an empty Model Builder workspace.

Equivalent to the legacy
``features/model-builder/scripts/qai_workspace_init.py`` script: lays
out the canonical ``C:/WoS_AI/<model>/`` skeleton (``output/`` +
``plan.md`` placeholder + ``REPORT.md`` placeholder) so a downstream
conversion pipeline can populate it.

The use case is intentionally minimal — it does no FS work itself,
only orchestrates a tiny port (defined inline so we don't over-grow
``ports.py`` for a one-shot adapter).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["InitWorkspaceUseCase", "WorkspaceInitializerPort"]


@runtime_checkable
class WorkspaceInitializerPort(Protocol):
    """Create an empty Model Builder workspace skeleton.

    Adapters live under ``qai.model_builder.adapters`` (filesystem
    backing) and may optionally seed ``plan.md`` from a template.
    """

    async def init(
        self,
        *,
        workdir: Path,
        model_name: str,
        precisions: tuple[str, ...] = (),
    ) -> None:
        ...


@dataclass(slots=True)
class InitWorkspaceUseCase:
    """Create the on-disk workspace layout for a new model."""

    workspace_initializer: WorkspaceInitializerPort

    async def execute(
        self,
        *,
        workdir: Path,
        model_name: str,
        precisions: tuple[str, ...] = (),
    ) -> None:
        await self.workspace_initializer.init(
            workdir=workdir,
            model_name=model_name,
            precisions=precisions,
        )
