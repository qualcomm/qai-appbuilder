# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aggregate roots / entities for the ``model_builder`` domain.

Two aggregates:

* :class:`ModelWorkspace` — a probed view of ``C:/WoS_AI/<name>/``,
  with only the slots the export pipeline actually consumes;
* :class:`Pack` — an emitted ``app_pack/`` directory ready for
  AppBuilder import.

Both are frozen dataclasses; mutation is via reconstruction. This
keeps the domain layer free of any orchestration logic — that lives
in the use cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .value_objects import (
    AccuracySummary,
    PackManifestSpec,
    Precision,
    Variant,
)

__all__ = ["ModelWorkspace", "Pack"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelWorkspace:
    """Probed view of a Model Builder workspace directory.

    Built by :class:`WorkspaceReaderPort` adapters from the on-disk
    layout::

        <workdir>/
            plan.md            (or qai_plan.md for legacy workspaces)
            REPORT.md
            inference_manifest.json
            output/
                <model>_fp16.bin
                <model>_int8.bin
                ...

    ``cosine_similarities`` / ``latencies_ms`` come from parsing
    ``REPORT.md``; downstream code uses them to derive the per-variant
    :class:`AccuracySummary` and to feed
    ``manifest.provenance.validation``.
    """

    workdir: Path
    model_name: str
    plan_path: Path | None
    report_path: Path | None
    plan_config: dict[str, str]
    inference_manifest: dict[str, object]
    output_dir: Path
    cosine_similarities: tuple[float, ...]
    latencies_ms: tuple[float, ...]
    validation_passed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Pack:
    """A fully-emitted AppBuilder Pack.

    ``manifest_spec`` carries the structured payload that backed the
    on-disk ``manifest.json`` so callers (route, validator, importer
    bridge) can introspect the result without re-reading the file.
    """

    pack_id: str
    pack_dir: Path
    manifest_spec: PackManifestSpec
    variants: tuple[Variant, ...]
    default_precision: Precision
    accuracy: AccuracySummary
    candidate_ready: bool
