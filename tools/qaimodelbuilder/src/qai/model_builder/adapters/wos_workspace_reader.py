# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem adapter for :class:`qai.model_builder.application.ports.WorkspaceReaderPort`.

Maps a ``C:/WoS_AI/<name>/`` directory into a
:class:`qai.model_builder.domain.ModelWorkspace` value object by
parsing ``plan.md`` (or legacy ``qai_plan.md``), ``REPORT.md`` and
``inference_manifest.json``. Path-traversal guard and minimum-size
enforcement live here too — keeping the use case adapter-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qai.model_builder.domain import ModelWorkspace, WorkspaceNotReadyError

from ._plan_parser import parse_inference_manifest, parse_plan, parse_report

__all__ = ["WosAiWorkspaceReader"]


@dataclass(slots=True)
class WosAiWorkspaceReader:
    """Probe a Model Builder workspace under the configured root.

    ``wos_ai_root`` is the on-disk root that callers must constrain
    the workspace to. It defaults to ``C:/WoS_AI`` for parity with
    the legacy
    ``backend/app_builder/api_routes.py:_WOS_AI_ROOT`` constant; tests
    may pass a ``tmp_path`` instead.
    """

    wos_ai_root: Path = Path("C:/WoS_AI")

    async def read(self, *, workdir: Path) -> ModelWorkspace:
        try:
            resolved = workdir.resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise WorkspaceNotReadyError(
                f"invalid model_workdir: {exc}"
            ) from exc

        # Path-traversal guard — refuse anything outside the configured
        # root. The legacy route did the same check; we replicate it
        # adapter-side because ``WorkspaceReaderPort`` is the boundary
        # at which untrusted user input meets the filesystem.
        try:
            wos_root_resolved = self.wos_ai_root.resolve(strict=False)
        except OSError:
            wos_root_resolved = self.wos_ai_root

        try:
            resolved.relative_to(wos_root_resolved)
        except ValueError as exc:
            raise WorkspaceNotReadyError(
                f"model_workdir must be located under {wos_root_resolved}: "
                f"got {resolved}"
            ) from exc

        if not resolved.is_dir():
            raise WorkspaceNotReadyError(
                f"workspace not found: {resolved}"
            )

        # Plan: prefer ``plan.md``, fall back to legacy ``qai_plan.md``.
        plan_path: Path | None = resolved / "plan.md"
        if not plan_path.is_file():
            plan_path = resolved / "qai_plan.md"
        if not plan_path.is_file():
            plan_path = None

        plan_config: dict[str, str] = parse_plan(plan_path) if plan_path else {}

        # Report (validation metrics).
        report_path: Path | None = resolved / "REPORT.md"
        report_metrics: dict[str, Any]
        if report_path.is_file():
            report_metrics = parse_report(report_path)
        else:
            report_path = None
            report_metrics = {
                "cosine_similarities": [],
                "latencies": [],
                "validation_passed": False,
            }

        # Inference manifest (best-effort; empty dict if missing/invalid).
        infer_manifest = parse_inference_manifest(resolved)

        # Output dir presence is mandatory — the export pipeline cannot
        # locate a context binary without it.
        output_dir = resolved / "output"
        if not output_dir.is_dir():
            raise WorkspaceNotReadyError(
                f"output directory not found: {output_dir}"
            )

        # Model name: explicit plan field wins, otherwise the workspace
        # directory name (legacy convention).
        model_name = (
            plan_config.get("MODEL_NAME")
            or resolved.name
        )

        return ModelWorkspace(
            workdir=resolved,
            model_name=str(model_name),
            plan_path=plan_path,
            report_path=report_path,
            plan_config=dict(plan_config),
            inference_manifest=dict(infer_manifest),
            output_dir=output_dir,
            cosine_similarities=tuple(report_metrics.get("cosine_similarities") or ()),
            latencies_ms=tuple(report_metrics.get("latencies") or ()),
            validation_passed=bool(report_metrics.get("validation_passed", False)),
        )
