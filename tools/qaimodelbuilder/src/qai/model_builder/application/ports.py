# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Abstract ports for the ``model_builder`` application layer.

All ports are :class:`typing.Protocol` so use cases can be tested
with hand-rolled in-memory stubs without instantiating concrete
adapters.

Design notes
------------

* Ports return / accept domain types only — never adapter-internal
  primitives (``Path`` is fine because ``pathlib.Path`` is part of
  the standard library and used throughout the domain).
* The :class:`PackExporterPort` is intentionally a *single-entry*
  interface (``export(...)``) — the legacy export pipeline runs as
  one orchestrated unit; splitting it into "build manifest" / "write
  runner" / "stage weights" sub-ports would just leak adapter detail
  into the use case.
* :class:`TaxonomyClassifierPort` is split off from ``PackExporter``
  because the classifier is also useful to other read paths (a
  future ``GET /api/model-builder/taxonomy/classify`` route can wire
  the same port without going through the export pipeline).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qai.model_builder.domain import (
    ClassifyResult,
    ExportPackCommand,
    ModelWorkspace,
    Pack,
    PackExportResult,
    PackManifestSpec,
)

__all__ = [
    "WorkspaceReaderPort",
    "PackExporterPort",
    "PackValidatorPort",
    "TaxonomyClassifierPort",
]


@runtime_checkable
class WorkspaceReaderPort(Protocol):
    """Probe a Model Builder workspace into a :class:`ModelWorkspace`.

    The adapter resolves ``workdir``, parses ``plan.md`` /
    ``qai_plan.md`` / ``REPORT.md`` / ``inference_manifest.json`` and
    walks ``output/`` to build a fully-populated workspace VO.

    Raises :class:`qai.model_builder.domain.WorkspaceNotReadyError`
    when the directory is missing, lives outside the WoS_AI root, or
    has no ``output/`` subdirectory.
    """

    async def read(self, *, workdir: Path) -> ModelWorkspace:
        """Probe ``workdir`` and return the workspace VO."""
        ...


@runtime_checkable
class TaxonomyClassifierPort(Protocol):
    """Three-layer classifier for ``model_name → (group, task)``.

    Pipeline (matches the legacy
    ``backend/app_builder/taxonomy_classifier.classify``):

    1. Manual override (caller-supplied ``category`` short-circuits);
    2. Rule-based keyword match against the bundled rules table;
    3. Model-shape heuristic (input/output dim cues from the
       inference manifest);
    4. LLM fallback — only invoked when the optional ``llm_callable``
       is wired by DI.

    The port stays sync because every step is in-memory; LLM access
    is on the caller's side and may itself be async, so the LLM
    callable accepted by adapters is ``Callable[[str, str], str]``.
    """

    def classify(
        self,
        *,
        model_name: str,
        infer_manifest: dict[str, Any] | None = None,
    ) -> ClassifyResult:
        """Run the classifier and return a :class:`ClassifyResult`."""
        ...


@runtime_checkable
class PackExporterPort(Protocol):
    """Materialise an ``app_pack/`` directory from a workspace.

    The adapter writes (incrementally — keeping ``examples/``,
    ``provenance/``, ``weights/``, ``assets/`` across re-exports):

    * ``manifest.json`` — :class:`PackManifestSpec` serialised;
    * ``runner.py`` — generated from a category-specific template;
    * ``requirements.txt`` — minimal numpy entry by default;
    * ``examples/<image>...`` + ``examples/LICENSES.md``;
    * ``provenance/source_plan.md`` + ``source_REPORT.md`` +
      ``accuracy_summary.json`` + ``import_meta.json``;
    * ``weights/<model>_<label>.bin`` — symlink (best-effort) or
      copy of the source ``.bin``;
    * ``_candidate.json`` — top-level structural-validation marker.

    Returns a :class:`PackExportResult` whose ``success`` mirrors the
    legacy ``all_pass`` aggregate over the ``checks`` block.

    Raises :class:`qai.model_builder.domain.MissingContextBinError`,
    :class:`qai.model_builder.domain.MissingQaiAppBuilderError`,
    :class:`qai.model_builder.domain.SmokeTestFailedError`,
    or :class:`qai.model_builder.domain.ManifestGenerationError`
    on hard failures.
    """

    async def export(
        self,
        *,
        workspace: ModelWorkspace,
        command: ExportPackCommand,
    ) -> PackExportResult:
        """Run the export pipeline; never raise on soft validation
        failures — those land in ``result.checks`` / ``result.errors``.
        Hard infrastructure failures still raise from the domain
        error hierarchy."""
        ...


@runtime_checkable
class PackValidatorPort(Protocol):
    """Validate an already-emitted Pack directory.

    Mirrors the legacy ``features/model-builder/scripts/qai_pack_validate.py``
    behaviour: it is invoked after :class:`PackExporterPort.export`
    completes (or against a Pack handed in from disk) to enforce
    structural invariants — manifest schema, ``runner.py`` compiles,
    weights checksum matches ``assets.checksum``, ``_candidate.json``
    structurally consistent.

    Returns the ``(success, errors)`` tuple; never raises for
    validation failure (only for I/O / parse errors that would also
    block the export pipeline).
    """

    async def validate(self, *, pack: Pack) -> tuple[bool, tuple[str, ...]]:
        """Inspect ``pack`` on disk and return ``(success, errors)``."""
        ...

    async def validate_dir(
        self,
        *,
        pack_dir: Path,
    ) -> tuple[bool, tuple[str, ...]]:
        """Same as :meth:`validate` but accepts a raw directory path
        (useful to validate a Pack handed in by the importer side)."""
        ...
