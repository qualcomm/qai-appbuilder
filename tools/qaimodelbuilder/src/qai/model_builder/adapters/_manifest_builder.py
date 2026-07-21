# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Manifest / candidate / accuracy / import-meta JSON builders.

Direct port of:

* ``generate_manifest`` — builds ``manifest.json`` from a workspace +
  classifier result + per-precision metrics + variants list;
* ``generate_accuracy_summary`` — builds ``provenance/accuracy_summary.json``;
* ``generate_import_meta`` — builds ``provenance/import_meta.json``;
* ``generate_candidate_json`` — builds ``_candidate.json`` (schema v1
  for single-variant Packs, schema v2 with a ``variants[]`` array
  for multi-variant Packs).

These functions are pure: they take dicts / VOs in and return
JSON-ready dicts out. Adapters call them and write the result to
disk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qai.model_builder.domain import (
    AccuracySummary,
    ClassifyResult,
    Precision,
    Variant,
    legacy_for,
)
from qai.model_builder.domain.value_objects import (
    COSINE_THRESHOLD_FP,
    COSINE_THRESHOLD_INT,
    PACK_SCHEMA_VERSION,
)

__all__ = [
    "schema_for_kind",
    "build_manifest_dict",
    "build_accuracy_summary",
    "build_accuracy_summary_dict",
    "build_import_meta_dict",
    "build_candidate_dict",
    "long_label_for_label",
]


# ---------------------------------------------------------------------------
# Wire-shape helpers
# ---------------------------------------------------------------------------

def schema_for_kind(kind: str) -> dict[str, Any]:
    """Map an ``input_kind`` / ``output_kind`` to AppBuilder schema."""
    schemas: dict[str, dict[str, Any]] = {
        "image": {
            "kind": "image",
            "constraints": {
                "maxMB": 10,
                "formats": ["png", "jpg", "jpeg", "webp"],
            },
        },
        "audio": {
            "kind": "audio",
            "constraints": {
                "sampleRate": 16000,
                "channels": 1,
                "maxSec": 120,
                "formats": ["wav", "mp3"],
            },
        },
        "text": {"kind": "text", "constraints": {"maxChars": 500}},
        "json": {"kind": "json"},
        "multi": {"kind": "multi"},
    }
    return schemas.get(kind, {"kind": kind})


def long_label_for_label(label: str) -> str:
    """Human-readable longLabel for a precision label."""
    return {
        "fp32":  "FP32 · Reference precision",
        "fp16":  "FP16 · Highest accuracy",
        "int8":  "INT8 · ~4x smaller, slightly less accurate",
        "w8a16": "W8A16 · 8-bit weights / 16-bit activations",
        "w4a16": "W4A16 · 4-bit weights / 16-bit activations",
        "int4":  "INT4 · Smallest, most aggressive quantization",
    }.get(label.lower(), f"{label.upper()} precision")


# ---------------------------------------------------------------------------
# manifest.json builder
# ---------------------------------------------------------------------------

def build_manifest_dict(
    *,
    pack_id: str,
    display_name: str,
    classify_result: ClassifyResult,
    legacy_category: str,
    model_name: str,
    default_precision: Precision,
    input_kind: str,
    output_kind: str,
    default_context_bin_name: str,
    default_context_bin_sha256: str,
    default_context_bin_size: int,
    plan_config: dict[str, str],
    cosine_similarities: tuple[float, ...],
    latencies_ms: tuple[float, ...],
    validation_passed: bool,
    inference_manifest: dict[str, Any] | None,
    io_contract: dict[str, Any] | None,
    variants: list[Variant],
    workdir: Path,
) -> dict[str, Any]:
    """Build the on-disk ``manifest.json`` payload.

    Multi-variant behaviour (``len(variants) >= 1``): the top-level
    ``runtime`` / ``assets`` / ``metrics`` blocks are overridden with
    the default variant's fields and a ``variants[]`` array is
    appended. Single-variant Packs (``variants`` empty) emit the
    legacy single-runtime shape unchanged so existing v1.x consumers
    keep reading the same fields.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Taxonomy block (preferred); legacy ``category`` retained one
    # release for old consumers.
    taxonomy_block = {
        "group": classify_result.group,
        "task": classify_result.task,
        "tags": [default_precision.plan_key, "modelbuilder-export"],
    }

    manifest: dict[str, Any] = {
        "schema_version": PACK_SCHEMA_VERSION,
        "modelId": pack_id,
        "displayName": display_name,
        # Deprecated: kept for one release for old consumers.
        "category": legacy_category,
        "taxonomy": taxonomy_block,
        "taxonomySource": classify_result.source,
        "taxonomyConfidence": classify_result.confidence,
        "version": "1.0.0",
        # ``vendor`` comes from the agent-emitted inference manifest.
        "vendor": (
            (inference_manifest or {}).get("vendor", "")
            if inference_manifest
            else ""
        ),
        "description": (
            f"{display_name} — exported from ModelBuilder "
            f"({default_precision.label.upper()})."
        ),
        "longDescription": (
            f"{display_name} running on Snapdragon QNN HTP backend, "
            f"{default_precision.label.upper()} precision. Exported "
            "from QAI ModelBuilder."
        ),
        "tags": [
            legacy_category.lower() if legacy_category else "uncategorised",
            default_precision.plan_key,
            "modelbuilder-export",
        ],
        "runtime": {
            "backend": "qnn",
            "delegate": "htp",
            "quantization": default_precision.plan_key,
            "modelSizeMB": round(default_context_bin_size / (1024 * 1024)),
            "supportedDevices": ["snapdragon-x-elite", "8gen3"],
        },
        "inputSchema": schema_for_kind(input_kind),
        "outputSchema": schema_for_kind(output_kind),
        "params": [],
        "metrics": {
            "latencyMs": int(latencies_ms[0]) if latencies_ms else 0,
            "memoryMB": 0,
        },
        "examples": [],
        "assets": {
            "weightsUrl": "",
            "checksum": f"sha256:{default_context_bin_sha256}",
            "sizeBytes": default_context_bin_size,
            "installPath": f"models/{pack_id}/{default_context_bin_name}",
        },
        "runner": {
            "type": "python-script",
            "script": "runner.py",
            "venv": "arm64",
            "requirements": "requirements.txt",
            "timeoutMs": 120000,
        },
        "skill": {
            # P4：SKILL.md 由 ``qai_pack_exporter._write_skill`` (Step 8.5) 自动
            # 生成，包含 pack-specific 的 I/O + weight-resolver 指引。默认
            # enabled=True，让 App Builder chat 的 ``ResolveSkillFilesUseCase``
            # 把这份 SKILL 注入到系统提示，Agent 就能直接看到用户 pack 的
            # 用法说明；否则 Step 8.5 生成的文件就成了死代码
            # （``skill_and_schema.py`` 的 ``_resolve_pack_skill`` 会因
            # ``not skill.enabled`` 直接跳过）。用户想控制 prompt budget 可以在导入后手工改回 False。
            "enabled": True,
            "file": "SKILL.md",
        },
        "capabilities": {
            "streaming": False,
            "batch": False,
            "benchmark": False,
            "cancel": True,
        },
        # SSOT for runtime shape / dtype. When the export-time probe could
        # validate the live ``.bin`` it carries the REAL inputs/outputs and the
        # runner cross-checks them. When it could NOT (no ``qai_appbuilder``
        # runtime on the authoring host) we MUST emit ``null`` rather than an
        # empty-lists placeholder: the runtime
        # ``io_validator.assert_contracts_compatible`` skips a falsy contract
        # (``if not static: return`` — legacy Pack path, live-only enforcement)
        # but REJECTS a truthy ``{"inputs": []}`` object against the live
        # ``[[1,3,299,299]]`` shapes with ``CONTRACT_MISMATCH``. An empty-lists
        # placeholder is therefore strictly worse than omitting the contract:
        # it turns an un-validated-but-runnable Pack into an un-runnable one.
        "io_contract": io_contract or None,
        "provenance": {
            "source": "model-builder",
            "sourceProject": plan_config.get("PROJECT_NAME", model_name),
            "sourceWorkdir": str(workdir),
            "precisionLabel": default_precision.plan_key,
            "targetArch": plan_config.get("TARGET_ARCH", "windows-aarch64"),
            "build": {
                "pipeline": "ModelBuilder",
                "exportTool": "qai_pack_exporter",
                "exportTimeUtc": now_iso,
            },
            "validation": {
                "passed": validation_passed,
                "cosineSimilarities": list(cosine_similarities),
                "latenciesMs": list(latencies_ms),
                "endTime": plan_config.get("END_TIME", ""),
                "workTime": plan_config.get("WORK_TIME", ""),
            },
            "import": {
                "importedAt": None,
                "importer": None,
                "checksum": f"sha256:{default_context_bin_sha256}",
                "mode": None,
            },
            "rollback": {
                "previousVersion": None,
            },
        },
    }

    # Multi-variant override: when ``variants`` is non-empty, replace
    # the top-level ``runtime`` / ``assets`` / ``metrics`` blocks with
    # the default variant's content and append a ``variants[]`` array.
    if variants:
        default_variant = next(
            (v for v in variants if v.is_default),
            variants[0],
        )

        manifest["runtime"] = {
            "backend": "qnn",
            "delegate": "htp",
            "quantization": default_variant.precision.plan_key,
            "modelSizeMB": round(default_variant.size_bytes / (1024 * 1024)),
            "contextBins": [default_variant.context_bin_name],
            "supportedDevices": ["snapdragon-x-elite", "8gen3"],
        }
        manifest["assets"] = {
            "weightsUrl": "",
            "installPath": default_variant.install_path,
            "sizeBytes": default_variant.size_bytes,
            "checksum": f"sha256:{default_variant.sha256}",
        }
        manifest["metrics"] = {
            "latencyMs": default_variant.latency_ms,
            "memoryMB": default_variant.memory_mb,
        }
        manifest["variants"] = [_variant_to_dict(v) for v in variants]

    return manifest


def _variant_to_dict(v: Variant) -> dict[str, Any]:
    return {
        "id": v.precision.variant_id,
        "label": v.precision.label.upper(),
        "longLabel": v.precision.long_label(),
        "default": v.is_default,
        "runtime": {
            "backend": "qnn",
            "delegate": "htp",
            "quantization": v.precision.plan_key,
            "modelSizeMB": round(v.size_bytes / (1024 * 1024)),
            "contextBins": [v.context_bin_name],
        },
        "assets": {
            "weightsUrl": "",
            "installPath": v.install_path,
            "sizeBytes": v.size_bytes,
            "checksum": f"sha256:{v.sha256}",
        },
        "metrics": {
            "latencyMs": v.latency_ms,
            "memoryMB": v.memory_mb,
        },
        "createdAt": v.mtime_iso,
    }


# ---------------------------------------------------------------------------
# accuracy_summary.json
# ---------------------------------------------------------------------------

def build_accuracy_summary(
    *,
    cosine_similarities: tuple[float, ...],
    latencies_ms: tuple[float, ...],
    validation_passed: bool,
    precision: Precision,
) -> AccuracySummary:
    """Compute a typed :class:`AccuracySummary` from the raw report metrics."""
    cosines = cosine_similarities
    latencies = latencies_ms
    threshold = (
        COSINE_THRESHOLD_FP if precision.is_floating_point else COSINE_THRESHOLD_INT
    )
    return AccuracySummary(
        precision=precision,
        cosine_threshold=threshold,
        cosine_values=cosines,
        cosine_min=min(cosines) if cosines else None,
        cosine_max=max(cosines) if cosines else None,
        cosine_mean=(sum(cosines) / len(cosines)) if cosines else None,
        cosine_all_pass=all(c >= threshold for c in cosines) if cosines else False,
        latencies_ms=latencies,
        latency_mean_ms=(sum(latencies) / len(latencies)) if latencies else None,
        validation_passed=validation_passed,
    )


def build_accuracy_summary_dict(summary: AccuracySummary) -> dict[str, Any]:
    """Serialise an :class:`AccuracySummary` for ``accuracy_summary.json``."""
    return summary.to_dict()


# ---------------------------------------------------------------------------
# import_meta.json
# ---------------------------------------------------------------------------

def build_import_meta_dict(
    *,
    pack_id: str,
    workdir: Path,
) -> dict[str, Any]:
    """Build the partial ``import_meta.json`` (importer fills the rest)."""
    return {
        "pack_id": pack_id,
        "source_workdir": str(workdir),
        "exported_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "exporter": "qai_pack_exporter",
        "status": "candidate",
        "imported_by": None,
        "import_time_utc": None,
        "target_registry": None,
        "notes": "",
    }


# ---------------------------------------------------------------------------
# _candidate.json
# ---------------------------------------------------------------------------

def build_candidate_dict(
    *,
    pack_id: str,
    display_name: str,
    source_workdir: Path,
    weights_abs_path: Path,
    all_checks_pass: bool,
    checks: dict[str, Any],
    variants: list[Variant],
) -> dict[str, Any]:
    """Build ``_candidate.json``.

    Schema v2 (multi-variant) when ``variants`` is non-empty;
    schema v1 (single-variant) otherwise. The legacy
    ``weightsAbsPath`` field is always written so v1 importers keep
    working with multi-variant Packs (it points at the default
    variant's source ``.bin``).
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if variants:
        candidate_variants: list[dict[str, Any]] = []
        for v in variants:
            candidate_variants.append({
                "id": v.precision.variant_id,
                "default": v.is_default,
                "weightsAbsPath": str(v.context_bin_path),
                "checksum": f"sha256:{v.sha256}",
                "sizeBytes": v.size_bytes,
                "ready": all_checks_pass,
                "checks": {
                    "context_binary_size_ok": (
                        v.size_bytes >= 1024 * 1024
                    ),
                    "context_binary_sha256": v.sha256,
                },
            })
        return {
            "schema_version": 2,
            "packId": pack_id,
            "displayName": display_name,
            "ready": all_checks_pass,
            "generatedAt": generated_at,
            "sourceWorkdir": str(source_workdir),
            "weightsAbsPath": str(weights_abs_path),
            "variants": candidate_variants,
            "checks": checks,
        }

    return {
        "packId": pack_id,
        "displayName": display_name,
        "ready": all_checks_pass,
        "generatedAt": generated_at,
        "sourceWorkdir": str(source_workdir),
        "weightsAbsPath": str(weights_abs_path),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Re-export ``legacy_for`` so adapters can use it without touching the
# domain layer directly.
# ---------------------------------------------------------------------------

__all__ += ["legacy_for_taxonomy"]


def legacy_for_taxonomy(group_id: str, task_id: str) -> str:
    """Adapter-side wrapper around :func:`qai.model_builder.domain.legacy_for`."""
    return legacy_for(group_id, task_id)
