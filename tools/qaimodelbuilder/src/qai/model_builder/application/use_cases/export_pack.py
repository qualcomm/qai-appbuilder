# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ExportPackUseCase`` — top-level orchestrator for ModelBuilder → Pack export.

Equivalent to the legacy
``features/model-builder/scripts/qai_pack_export.py:export_pack`` 11-step
pipeline. The use case itself is a thin orchestrator: it delegates
the workspace probe to :class:`WorkspaceReaderPort`, the actual
emission to :class:`PackExporterPort`, and the post-write validation
to :class:`PackValidatorPort`.

Behaviour parity with the legacy script
---------------------------------------

* ``precisions`` may be empty → falls back to single-precision auto
  detect (workspace reader picks the first ``<model>_<label>.bin``
  under ``output/`` whose label is supported);
* ``default_precision`` defaults to the first ``precisions`` entry;
* every input precision token is normalised to plan-form (``int8``
  → ``w8a8``) before the workspace reader probes for binaries;
* validation failures (missing ``REPORT.md``, sub-threshold cosine,
  etc.) become entries on ``PackExportResult.errors`` rather than
  exceptions — only hard infra failures (missing default ``.bin``,
  ``qai_appbuilder`` unavailable, smoke-test crash) raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qai.model_builder.application.ports import (
    PackExporterPort,
    PackValidatorPort,
    WorkspaceReaderPort,
)
from qai.model_builder.domain import (
    ExportPackCommand,
    PackExportResult,
    Precision,
)

__all__ = ["ExportPackUseCase"]


@dataclass(slots=True)
class ExportPackUseCase:
    """Wire :class:`WorkspaceReaderPort` → :class:`PackExporterPort`."""

    workspace_reader: WorkspaceReaderPort
    pack_exporter: PackExporterPort
    pack_validator: PackValidatorPort | None = None

    async def execute(self, command: ExportPackCommand) -> PackExportResult:
        """Run the full export pipeline.

        The use case keeps no internal state across calls; concurrent
        invocations on the same workspace are safe insofar as the
        adapter's ``_clean_for_re_export`` helper is — the legacy
        script also relies on the FS being the single coordination
        point for re-export.
        """
        # Step 0: normalise + validate the command's path constraints
        # *before* hitting the workspace reader, so the route can
        # surface a clean ValidationError without burning through I/O.
        if not isinstance(command.model_workdir, Path):
            raise TypeError("command.model_workdir must be a Path")

        # Normalise precision tokens via the Precision VO. This raises
        # nothing for unknown tokens — they are kept verbatim so the
        # workspace reader can emit a readable
        # "Unknown precision 'foo'" diagnostic pointing at the offender.
        normalised_precisions: tuple[Precision, ...] = tuple(
            Precision.from_token(t) for t in command.precisions
        )
        normalised_command = ExportPackCommand(
            model_workdir=command.model_workdir,
            model_name=command.model_name,
            precisions=tuple(p.plan_key for p in normalised_precisions),
            default_precision=(
                Precision.from_token(command.default_precision).plan_key
                if command.default_precision
                else None
            ),
            pack_id_override=command.pack_id_override,
            category_override=command.category_override,
            display_name_override=command.display_name_override,
            input_kind_override=command.input_kind_override,
            output_kind_override=command.output_kind_override,
        )

        # Step 1: probe the workspace.
        workspace = await self.workspace_reader.read(
            workdir=normalised_command.model_workdir,
        )

        # Step 2: hand the workspace + normalised command to the
        # exporter. The exporter is the place where the 11-step legacy
        # pipeline lives; the use case never reaches into adapter
        # internals beyond what the result VO exposes.
        result = await self.pack_exporter.export(
            workspace=workspace,
            command=normalised_command,
        )

        # Step 3: post-emit structural validation. Optional — when
        # ``pack_validator`` is wired the failures fold into
        # ``result.errors``, but the export's own ``checks`` aggregate
        # is the legacy authoritative gate (parity with
        # ``_candidate.json:ready``).
        if self.pack_validator is not None and result.success:
            ok, validation_errors = await self.pack_validator.validate_dir(
                pack_dir=result.pack_path,
            )
            if not ok:
                # Tail-append validator errors but keep ``success``
                # driven by the exporter's own checks aggregate so the
                # legacy contract is preserved.
                merged_errors = (*result.errors, *validation_errors)
                result = PackExportResult(
                    success=result.success,
                    pack_id=result.pack_id,
                    display_name=result.display_name,
                    pack_path=result.pack_path,
                    candidate_json_path=result.candidate_json_path,
                    manifest_path=result.manifest_path,
                    runner_path=result.runner_path,
                    requirements_path=result.requirements_path,
                    examples_dir=result.examples_dir,
                    provenance_dir=result.provenance_dir,
                    weights_dir=result.weights_dir,
                    variants=result.variants,
                    checks=result.checks,
                    failed_checks=result.failed_checks,
                    log_lines=result.log_lines,
                    errors=merged_errors,
                )

        return result
