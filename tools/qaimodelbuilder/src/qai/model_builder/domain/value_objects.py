# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for the ``model_builder`` domain.

Every type here is a frozen dataclass — no behaviour, no I/O, no
framework imports. Dataclass-only so adapters can convert the
results to JSON without bespoke serialization glue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Precision",
    "ModelKind",
    "IoKind",
    "Variant",
    "Provenance",
    "AccuracySummary",
    "PackManifestSpec",
    "PackExportResult",
    "ExportPackCommand",
    "MIN_CONTEXT_BIN_SIZE",
    "COSINE_THRESHOLD_FP",
    "COSINE_THRESHOLD_INT",
    "PACK_SCHEMA_VERSION",
]


# ---------------------------------------------------------------------------
# Constants (mirrored from the legacy qai_pack_export.py SSOT)
# ---------------------------------------------------------------------------

PACK_SCHEMA_VERSION = 1
"""Top-level ``manifest.schema_version`` for emitted Packs."""

MIN_CONTEXT_BIN_SIZE = 1 * 1024 * 1024
"""Reject context binaries smaller than 1 MiB — they are stubs that
would crash AppBuilder on first inference."""

COSINE_THRESHOLD_FP = 0.99
"""Validation gate for floating-point precisions (fp16 / fp32)."""

COSINE_THRESHOLD_INT = 0.95
"""Validation gate for quantised precisions (w8a8 / w8a16 / w4a16 / ...)."""


# ---------------------------------------------------------------------------
# Precision
# ---------------------------------------------------------------------------

# Plan-form (``MODEL_BUILDER`` / ``qai_plan.md``) → filename label form.
# This mirrors the legacy ``PRECISION_LABEL_MAP``. Both tokens are accepted
# as input; the canonical *key* form is what context-binary lookup uses.
_PLAN_TO_LABEL: dict[str, str] = {
    "fp16":  "fp16",
    "fp32":  "fp32",
    "w8a8":  "int8",
    "w8a16": "w8a16",
    "w8a8b8": "int8",
    "w4a16": "w4a16",
    "w4a8":  "int4",
}

# Reverse: filename suffix label → a representative *plan* key.
# ``int8`` and ``int4`` collide on the plan side; we pick the canonical
# preimage (``w8a8`` / ``w4a8``).
_LABEL_TO_PLAN: dict[str, str] = {
    "fp16":  "fp16",
    "fp32":  "fp32",
    "int8":  "w8a8",
    "w8a16": "w8a16",
    "w4a16": "w4a16",
    "int4":  "w4a8",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Precision:
    """A single quantisation level.

    Stored in *plan key* form (``fp16`` / ``w8a8`` / ``w4a16`` / ...).
    Construction also accepts label-form aliases (``int8`` → ``w8a8``)
    for parity with the legacy ``--precisions int8,fp16`` CLI used by
    the auto-export route.
    """

    ALL_PLAN_KEYS: tuple[str, ...] = field(
        default=("fp16", "fp32", "w8a8", "w8a16", "w8a8b8", "w4a16", "w4a8"),
        repr=False,
    )
    ALL_LABELS: tuple[str, ...] = field(
        default=("fp16", "fp32", "int8", "w8a16", "w4a16", "int4"),
        repr=False,
    )

    plan_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan_key, str) or not self.plan_key:
            raise ValueError("Precision.plan_key must be a non-empty string")

    @classmethod
    def from_token(cls, token: str) -> "Precision":
        """Normalise either plan-form or label-form into a :class:`Precision`."""
        if not isinstance(token, str) or not token.strip():
            raise ValueError("precision token must be a non-empty string")
        t = token.strip().lower()
        if t in _PLAN_TO_LABEL:
            return cls(plan_key=t)
        if t in _LABEL_TO_PLAN:
            return cls(plan_key=_LABEL_TO_PLAN[t])
        # Unknown token — keep as-is so the workspace reader can emit a
        # readable "Unknown precision" error pinpointing the offender.
        return cls(plan_key=t)

    @classmethod
    def is_supported_token(cls, token: str) -> bool:
        """Return ``True`` iff ``token`` is one of the recognised precisions."""
        if not isinstance(token, str):
            return False
        t = token.strip().lower()
        return t in _PLAN_TO_LABEL or t in _LABEL_TO_PLAN

    @property
    def label(self) -> str:
        """Filename suffix label form (``int8`` / ``fp16`` / ``w8a16`` / ...)."""
        return _PLAN_TO_LABEL.get(self.plan_key, self.plan_key)

    @property
    def variant_id(self) -> str:
        """``manifest.variants[i].id`` — equals :attr:`label`."""
        return self.label

    @property
    def is_floating_point(self) -> bool:
        return self.plan_key in ("fp16", "fp32")

    @property
    def cosine_threshold(self) -> float:
        return COSINE_THRESHOLD_FP if self.is_floating_point else COSINE_THRESHOLD_INT

    def long_label(self) -> str:
        """Human-readable longLabel for a variant chip / drawer."""
        return _LONG_LABELS.get(self.label, f"{self.label.upper()} precision")


_LONG_LABELS: dict[str, str] = {
    "fp32":  "FP32 · Reference precision",
    "fp16":  "FP16 · Highest accuracy",
    "int8":  "INT8 · ~4x smaller, slightly less accurate",
    "w8a16": "W8A16 · 8-bit weights / 16-bit activations",
    "w4a16": "W4A16 · 4-bit weights / 16-bit activations",
    "int4":  "INT4 · Smallest, most aggressive quantization",
}


# ---------------------------------------------------------------------------
# ModelKind / IoKind
# ---------------------------------------------------------------------------

# These are deliberately separate types even though they share the same
# string set — input vs. output direction has independent semantics in
# the AppBuilder schema.

_KINDS: tuple[str, ...] = ("image", "audio", "text", "multi", "json")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelKind:
    """Coarse modality of a model (image / audio / text / multi / json)."""

    value: str

    def __post_init__(self) -> None:
        if self.value not in _KINDS:
            raise ValueError(
                f"ModelKind.value must be one of {_KINDS}, got {self.value!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class IoKind:
    """Input or output kind on the AppBuilder schema (matches :class:`ModelKind` set)."""

    value: str

    def __post_init__(self) -> None:
        if self.value not in _KINDS:
            raise ValueError(
                f"IoKind.value must be one of {_KINDS}, got {self.value!r}"
            )

    @classmethod
    def is_supported_token(cls, token: str) -> bool:
        return isinstance(token, str) and token in _KINDS


# ---------------------------------------------------------------------------
# Variant
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class Variant:
    """One per-precision entry of ``manifest.variants[]``.

    Mirrors the wire schema documented in
    ``docs/30-ui-ux/multi-variant-pack-contract.md`` §1.1 and §7.

    ``context_bin_path`` is the absolute path to the source ``.bin``
    file under ``<workdir>/output/``; the exporter symlinks (or copies)
    it under ``<pack>/weights/<basename>`` and stores the final
    ``installPath`` in :attr:`install_path`.
    """

    precision: Precision
    context_bin_path: Path
    context_bin_name: str
    size_bytes: int
    sha256: str
    mtime_iso: str
    is_default: bool
    install_path: str
    latency_ms: int = 0
    memory_mb: int = 0


# ---------------------------------------------------------------------------
# AccuracySummary / Provenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class AccuracySummary:
    """Validation outcome rolled up from ``REPORT.md``.

    Persisted at ``<pack>/provenance/accuracy_summary.json`` and also
    flattened into ``manifest.provenance.validation`` so external
    importers do not need to crack open the provenance directory.
    """

    precision: Precision
    cosine_threshold: float
    cosine_values: tuple[float, ...]
    cosine_min: float | None
    cosine_max: float | None
    cosine_mean: float | None
    cosine_all_pass: bool
    latencies_ms: tuple[float, ...]
    latency_mean_ms: float | None
    validation_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": self.precision.plan_key,
            "precision_type": (
                "floating_point" if self.precision.is_floating_point else "quantized"
            ),
            "cosine_threshold": self.cosine_threshold,
            "cosine_values": list(self.cosine_values),
            "cosine_min": self.cosine_min,
            "cosine_max": self.cosine_max,
            "cosine_mean": self.cosine_mean,
            "cosine_all_pass": self.cosine_all_pass,
            "latencies_ms": list(self.latencies_ms),
            "latency_mean_ms": self.latency_mean_ms,
            "validation_passed": self.validation_passed,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """Source-of-truth metadata for the emitted Pack.

    Maps 1:1 to ``manifest.provenance`` and the contents of
    ``<pack>/provenance/``: ``source_plan.md`` (copied verbatim),
    ``source_REPORT.md`` (copied or placeholder), ``accuracy_summary.json``
    (serialised :class:`AccuracySummary`), ``import_meta.json``.
    """

    source_workdir: Path
    source_plan_path: Path | None
    source_report_path: Path | None
    plan_config: dict[str, str]
    target_arch: str
    pipeline: str
    export_tool: str
    exported_at_utc: str
    cosine_similarities: tuple[float, ...]
    latencies_ms: tuple[float, ...]
    validation_passed: bool


# ---------------------------------------------------------------------------
# PackManifestSpec — the structured manifest VO
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class PackManifestSpec:
    """The fully-realised ``manifest.json`` payload as a value object.

    Adapters serialise this with :meth:`to_dict` to emit the on-disk
    JSON. Keeping it as a VO lets the validator and the bridge work
    against a stable contract regardless of JSON-schema drift.
    """

    schema_version: int
    pack_id: str
    display_name: str
    legacy_category: str
    taxonomy_group: str
    taxonomy_task: str | None
    taxonomy_source: str
    taxonomy_confidence: float
    version: str
    vendor: str
    description: str
    long_description: str
    tags: tuple[str, ...]
    runner_kind: ModelKind
    input_kind: IoKind
    output_kind: IoKind
    precision: Precision
    context_bin_name: str
    context_bin_sha256: str
    context_bin_size: int
    variants: tuple[Variant, ...]
    accuracy: AccuracySummary
    provenance: Provenance
    io_contract: dict[str, Any]
    inference_manifest: dict[str, Any]


# ---------------------------------------------------------------------------
# Commands & results
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class ExportPackCommand:
    """Input to :class:`ExportPackUseCase.execute`.

    Mirrors the legacy auto-export HTTP body documented at
    ``backend/app_builder/api_routes.py:1197-1356``:

    * ``model_workdir`` — required absolute path under ``C:/WoS_AI/``;
    * ``model_name`` — defaults to the workdir directory name;
    * ``precisions`` — multi-variant list, plan or label form;
    * ``default_precision`` — must be in ``precisions`` (or its first item);
    * remaining ``*_override`` fields short-circuit auto-inference.
    """

    model_workdir: Path
    model_name: str | None = None
    precisions: tuple[str, ...] = ()
    default_precision: str | None = None
    pack_id_override: str | None = None
    category_override: str | None = None
    display_name_override: str | None = None
    input_kind_override: str | None = None
    output_kind_override: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PackExportResult:
    """Output of :class:`ExportPackUseCase.execute`.

    ``success`` reflects the legacy ``all_pass`` aggregate over the
    ``checks`` block of ``_candidate.json``. The ``log_lines`` capture
    is a best-effort diagnostic surface — the route turns it into the
    response ``output`` field for the legacy frontend.
    """

    success: bool
    pack_id: str
    display_name: str
    pack_path: Path
    candidate_json_path: Path
    manifest_path: Path
    runner_path: Path
    requirements_path: Path
    examples_dir: Path
    provenance_dir: Path
    weights_dir: Path
    variants: tuple[Variant, ...]
    checks: dict[str, Any]
    failed_checks: tuple[str, ...]
    log_lines: tuple[str, ...]
    errors: tuple[str, ...]
