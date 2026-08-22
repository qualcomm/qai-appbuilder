# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain layer for the ``model_builder`` bounded context.

Pure business types:

* :mod:`.value_objects` — :class:`Precision`, :class:`ModelKind`,
  :class:`IoKind`, :class:`Variant`, :class:`Provenance`,
  :class:`PackManifestSpec`, :class:`PackExportResult`,
  :class:`ExportPackCommand`.
* :mod:`.entities` — :class:`ModelWorkspace`, :class:`Pack`.
* :mod:`.taxonomy` — group / task / legacy-category SSOT (private copy
  per ``[importlinter:contract:context-isolation]``).
* :mod:`.errors` — typed domain errors raised by use cases.
"""

from __future__ import annotations

from .entities import ModelWorkspace, Pack
from .errors import (
    InvalidPrecisionError,
    ManifestGenerationError,
    MissingContextBinError,
    MissingQaiAppBuilderError,
    SmokeTestFailedError,
    WorkspaceNotReadyError,
)
from .taxonomy import (
    LEGACY_CATEGORY_MAP,
    TAXONOMY_VERSION,
    ClassifyResult,
    all_group_ids,
    all_task_ids,
    group_of_task,
    io_for_task,
    legacy_for,
    task_label,
)
from .value_objects import (
    AccuracySummary,
    ExportPackCommand,
    IoKind,
    ModelKind,
    PackExportResult,
    PackManifestSpec,
    Precision,
    Provenance,
    Variant,
)

__all__ = [
    "ModelWorkspace",
    "Pack",
    "InvalidPrecisionError",
    "ManifestGenerationError",
    "MissingContextBinError",
    "MissingQaiAppBuilderError",
    "SmokeTestFailedError",
    "WorkspaceNotReadyError",
    "LEGACY_CATEGORY_MAP",
    "TAXONOMY_VERSION",
    "ClassifyResult",
    "all_group_ids",
    "all_task_ids",
    "group_of_task",
    "io_for_task",
    "legacy_for",
    "task_label",
    "AccuracySummary",
    "ExportPackCommand",
    "IoKind",
    "ModelKind",
    "PackExportResult",
    "PackManifestSpec",
    "Precision",
    "Provenance",
    "Variant",
]
