# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Plan / REPORT / inference-manifest parsers used by :class:`WosAiWorkspaceReader`.

Direct port of the standalone helpers in
``features/model-builder/scripts/qai_pack_export.py``:

* ``parse_plan`` — KEY=value pairs from a markdown Config block;
* ``parse_report`` — cosine similarity + latency extraction from
  ``REPORT.md``;
* ``parse_inference_manifest`` — best-effort load of
  ``inference_manifest.json`` (the LLM agent emits it post-inference).

Each helper is a pure function: no I/O beyond ``read_text``, no
side effects. They live in the adapter layer because they encode
the legacy on-disk format which is intentionally not part of the
domain contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "parse_plan",
    "parse_report",
    "parse_inference_manifest",
]


def parse_plan(plan_path: Path) -> dict[str, str]:
    """Extract ``KEY = value`` pairs from ``plan.md`` / ``qai_plan.md``.

    Recognised lines look like::

        MODEL_NAME    = real_esrgan_x4plus
        PRECISION     = fp16
        OUTPUT_DIR    = C:\\WoS_AI\\real_esrgan_x4plus\\output

    Trailing ``# comment`` is stripped. Values surrounded by matching
    single or double quotes have the quotes removed.

    Returns the empty dict when ``plan_path`` is missing — callers
    decide whether to surface that as an error.
    """
    if not plan_path.is_file():
        return {}

    text = plan_path.read_text(encoding="utf-8")

    pattern = re.compile(
        r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.+?)(?:\s*#.*)?$",
        re.MULTILINE,
    )
    result: dict[str, str] = {}
    for m in pattern.finditer(text):
        key = m.group(1).strip()
        val = m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        result[key] = val

    return result


def parse_report(report_path: Path) -> dict[str, Any]:
    """Extract validation metrics from ``REPORT.md``.

    Returns a dict with ``cosine_similarities`` (``list[float]``),
    ``latencies`` (``list[float]``) and ``validation_passed`` (bool).

    Multiple regex passes are tried in priority order so the parser
    tolerates the wide variety of report formats produced by Model
    Builder agents and templates over time.
    """
    if not report_path.is_file():
        return {
            "cosine_similarities": [],
            "latencies": [],
            "validation_passed": False,
        }

    text = report_path.read_text(encoding="utf-8")

    cosines: list[float] = []

    # Pass 1: explicit "Cosine Similarity: 0.999" / "cosine_sim = 0.999".
    cosine_pattern = re.compile(
        r"[Cc]osine[\s_-]*[Ss]imilarity[^:=\n]*[\s:=]+([0-9]+\.[0-9]+)",
        re.IGNORECASE,
    )
    cosines = [float(m.group(1)) for m in cosine_pattern.finditer(text)]

    # Pass 2: code-style "cosine_sim = 0.999" / "cosine: 0.999".
    if not cosines:
        cosine_code_pattern = re.compile(
            r"cosine[\s_-]*(?:sim)?[\s:=]+([0-9]+\.[0-9]+)",
            re.IGNORECASE,
        )
        cosines = [float(m.group(1)) for m in cosine_code_pattern.finditer(text)]

    # Pass 3: "ONNX vs <label> : 0.999" / "ONNX vs <label> | 0.999".
    if not cosines:
        onnx_vs_pattern = re.compile(
            r"ONNX\s+vs\s+\S+[\s|:]+([01]\.\d{4,})",
            re.IGNORECASE,
        )
        cosines = [float(m.group(1)) for m in onnx_vs_pattern.finditer(text)]

    # Pass 4: generic "<label> vs <label> : 0.9xx".
    if not cosines:
        vs_pattern = re.compile(
            r"\b\w+\s+vs\s+\w+[\s|:]+([01]\.\d{4,})",
            re.IGNORECASE,
        )
        cosines = [float(m.group(1)) for m in vs_pattern.finditer(text)]

    # Pass 5: Markdown table row whose first cell is a precision label
    # (FP16/FP32/INT8/W8A8/W8A16/W4A16/W4A8/W8A8B8/A16W8) and whose
    # second cell is a 0.xxx float (optionally **bold**). Anchored to
    # start of line + leading "|" so we don't match inline mentions.
    if not cosines:
        md_table_pattern = re.compile(
            r"(?m)^\s*\|\s*(?:FP16|FP32|INT8|W8A8B8|W8A16|W4A16|W4A8|W8A8|A16W8)\s*\|"
            r"\s*\**\s*([01]\.\d{3,})\s*\**",
            re.IGNORECASE,
        )
        cosines = [float(m.group(1)) for m in md_table_pattern.finditer(text)]

    # Latency: "Latency: 12.3 ms" / "latency_ms = 15.2".
    latency_pattern = re.compile(
        r"[Ll]atency[\s:=_]*([0-9]+\.?[0-9]*)\s*(?:ms)?",
        re.IGNORECASE,
    )
    latencies = [float(m.group(1)) for m in latency_pattern.finditer(text)]

    # Latency fallback: "p50=5.3 ms" / "min=4.4ms".
    if not latencies:
        p_pattern = re.compile(
            r"(?:p50|p95|min|avg)[\s=:]*([0-9]+\.?[0-9]*)\s*ms",
            re.IGNORECASE,
        )
        latencies = [float(m.group(1)) for m in p_pattern.finditer(text)]

    return {
        "cosine_similarities": cosines,
        "latencies": latencies,
        "validation_passed": len(cosines) > 0,
    }


def parse_inference_manifest(workdir: Path) -> dict[str, Any]:
    """Load ``<workdir>/inference_manifest.json`` if present.

    Returns the parsed dict, or ``{}`` when the file is missing or
    not a JSON object. The legacy script logged warnings; we silently
    return empty so the use case can decide whether to emit a
    diagnostic — the workspace reader does not have a logger.
    """
    manifest_path = workdir / "inference_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        content = manifest_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data
