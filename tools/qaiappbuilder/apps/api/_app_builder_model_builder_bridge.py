# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context bridge: App Builder route -> Model Builder export use case.

The HTTP route ``POST /api/app-builder/import/auto-export`` lives in
``interfaces.http.routes.app_builder``; the Pack export pipeline
lives in ``qai.model_builder``. ``[importlinter:contract:context-isolation]``
forbids ``qai.app_builder`` from importing ``qai.model_builder``
(and vice versa), so the two contexts collaborate via this bridge in
the ``apps/api`` composition layer (single legitimate cross-context
join point).

The bridge captures both contexts' service namespaces from the same
:class:`Container`, exposes one async method
:meth:`trigger_auto_export`, and translates the legacy auto-export
HTTP body (``modelWorkdir`` / ``precisions`` / ``defaultPrecision``)
into a :class:`qai.model_builder.domain.ExportPackCommand`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qai.model_builder.application.use_cases.export_pack import (
    ExportPackUseCase,
)
from qai.model_builder.domain import ExportPackCommand, PackExportResult

__all__ = [
    "AutoExportJobResult",
    "AppBuilderModelBuilderBridge",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class AutoExportJobResult:
    """Wire-shape outcome surfaced by :meth:`AppBuilderModelBuilderBridge.trigger_auto_export`.

    Mirrors the legacy ``backend/app_builder/api_routes.py:appbuilder_import_auto_export``
    response body so the App Builder frontend's PromoteCard /
    ImportPanel work without changes:

    * ``success`` — ``True`` iff the structural-validation aggregate
      on ``_candidate.json:ready`` was ``True``;
    * ``pack_id`` — emitted ``manifest.modelId`` (kebab-case);
    * ``display_name`` — ``manifest.displayName``;
    * ``source_workdir`` — absolute path to ``<workdir>/app_pack/``
      (matches legacy ``sourceWorkdir`` field — used by subsequent
      ``/api/app-builder/import/dry-run`` + ``commit`` calls);
    * ``output`` — last 1000 chars of the export log (legacy field);
    * ``errors`` — soft-failure messages (missing precisions, etc.).
    """

    success: bool
    pack_id: str
    display_name: str
    source_workdir: str
    output: str
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class AppBuilderModelBuilderBridge:
    """Adapter from the App Builder HTTP route to the Model Builder use case."""

    export_pack_use_case: ExportPackUseCase

    async def trigger_auto_export(
        self,
        *,
        model_workdir: str,
        model_name: str | None = None,
        precisions: tuple[str, ...] = (),
        default_precision: str | None = None,
        category_override: str | None = None,
        display_name_override: str | None = None,
        input_kind_override: str | None = None,
        output_kind_override: str | None = None,
        pack_id_override: str | None = None,
    ) -> AutoExportJobResult:
        """Run the export pipeline and translate the result into the bridge VO.

        Validation of ``model_workdir`` (path traversal etc.) lives
        inside the workspace reader adapter; the bridge does not
        re-validate so the route gets a single source of truth for
        path safety errors.
        """
        command = ExportPackCommand(
            model_workdir=Path(model_workdir),
            model_name=model_name,
            precisions=tuple(precisions),
            default_precision=default_precision,
            pack_id_override=pack_id_override,
            category_override=category_override,
            display_name_override=display_name_override,
            input_kind_override=input_kind_override,
            output_kind_override=output_kind_override,
        )
        result: PackExportResult = await self.export_pack_use_case.execute(
            command,
        )

        # Tail of the log is what the legacy route surfaced as ``output``.
        log_text = "\n".join(result.log_lines)
        log_tail = log_text[-1000:] if log_text else ""

        return AutoExportJobResult(
            success=result.success,
            pack_id=result.pack_id,
            display_name=result.display_name,
            source_workdir=str(result.pack_path),
            output=log_tail,
            errors=tuple(result.errors),
        )

    def probe_export_status(self, *, model_workdir: str) -> str:
        """Report the on-disk auto-export state of a workspace.

        Returns ``"generating"`` while an export is running (the exporter's
        ``.generating`` sentinel is present and fresh), ``"generated"`` once
        ``app_pack/_candidate.json`` exists, or ``"idle"`` otherwise. Pure
        filesystem probe — cheap enough for the Import panel to poll on
        (re)open, which is what lets a user who closed the window
        mid-generation still see "生成中..." instead of the initial button.

        Lives on the bridge (not the App Builder route) because the probe
        helper ships in ``qai.model_builder`` and
        ``[importlinter:contract:context-isolation]`` forbids
        ``qai.app_builder`` from importing it directly.
        """
        # Imported lazily so the module import graph stays clean and the
        # helper's own dependencies load only when the route is exercised.
        from qai.model_builder.adapters._pack_layout import (
            probe_export_status as _probe,
        )

        return _probe(Path(model_workdir))


def build_auto_export_bridge(*, container: Any) -> AppBuilderModelBuilderBridge:
    """Construct the bridge from an :class:`apps.api.di.Container`.

    The ``container`` argument is typed as ``Any`` to avoid a hard
    import cycle with :mod:`apps.api.di` (which already builds
    ``container.model_builder``). Callers pass the live container.
    """
    return AppBuilderModelBuilderBridge(
        export_pack_use_case=container.model_builder.export_pack_use_case,
    )
