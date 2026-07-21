# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Asset (labels / vocab / tokenizer) collector for the emitted Pack.

Direct port of the legacy ``Step 9b`` logic from
``features/model-builder/scripts/qai_pack_export.py:export_pack``.

Two-pass collection:

1. **Manifest-declared** — files listed in
   ``inference_manifest.json:assets[]`` (the LLM agent emits this
   when it knows the model needs labels, e.g. for image classifiers
   the ImageNet labels file).
2. **Filename keyword scan** — token-boundary regex over filenames
   under ``<workdir>``, ``<workdir>/output``, ``<workdir>/assets``,
   ``<workdir>/data``, ``<workdir>/labels``. Path-traversal guard
   refuses any ``assets[].file`` resolving outside ``workdir``.

Failures (missing files, copy errors) are logged into the returned
``log_lines`` tuple but never abort the export — the legacy script
swallowed every ``OSError`` here for parity with real-world workflows
where the agent occasionally lists assets that did not actually land
on disk.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

__all__ = ["collect_assets"]


_ASSET_PATTERNS: tuple[str, ...] = ("*.txt", "*.json", "*.names")

_ASSET_NAME_RE = re.compile(
    r"(?:^|[_\-])"
    r"(label|labels|class|classes|vocab|vocabulary|tokenizer|"
    r"synset|synsets|categor(?:y|ies)|imagenet|coco|"
    r"dictionary|names)"
    r"(?:[_\-]|\.|$)"
)

_ASSET_EXCLUDE: frozenset[str] = frozenset({
    "qai_plan.md", "plan.md", "AGENTS.md", "REPORT.md",
    "export_onnx.py", "calibration_list.txt", "requirements.txt",
})

_SCAN_SUBDIRS: tuple[str, ...] = ("", "output", "assets", "data", "labels")

_MAX_ASSET_BYTES = 10 * 1024 * 1024  # 10 MiB cap per asset file


def collect_assets(
    *,
    workdir: Path,
    assets_dir: Path,
    inference_manifest: dict[str, Any] | None,
    taxonomy_task: str | None,
) -> tuple[int, list[str]]:
    """Copy declared + auto-detected assets into ``assets_dir``.

    Returns ``(asset_count, log_lines)``. The ``taxonomy_task`` arg is
    used to tailor the diagnostic when no asset is found and the task
    is one of the label-needing families (image-classification /
    object-detection / ocr) — matching the legacy WARN messages.
    """
    log: list[str] = []
    asset_count = 0
    workdir_resolved = workdir.resolve(strict=False)

    def _is_within_workdir(path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(workdir_resolved)
            return True
        except (ValueError, OSError):
            return False

    def _safe_size_ok(path: Path) -> bool:
        try:
            return path.stat().st_size <= _MAX_ASSET_BYTES
        except OSError:
            return False

    def _safe_copy(src: Path, dest: Path, label: str) -> bool:
        if not _is_within_workdir(src):
            log.append(f"[WARN]   Rejecting asset path outside workdir: {src}")
            return False
        try:
            shutil.copy2(src, dest)
            return True
        except OSError as exc:
            log.append(f"[WARN]   Failed to copy {label}: {exc}")
            return False

    # Priority 1: manifest-declared assets.
    declared_assets = (
        inference_manifest.get("assets") if isinstance(inference_manifest, dict) else None
    )
    if isinstance(declared_assets, list):
        for asset_entry in declared_assets:
            if not isinstance(asset_entry, dict):
                log.append(
                    f"[WARN]   Skipping non-object asset entry in manifest: {asset_entry!r}"
                )
                continue
            asset_file = asset_entry.get("file", "")
            if not isinstance(asset_file, str) or not asset_file:
                continue
            src: Path | None = None
            for base in (workdir, workdir / "output"):
                candidate = base / asset_file
                if candidate.is_file():
                    src = candidate
                    break
            if src is None:
                log.append(f"[WARN]   Declared asset not found: {asset_file}")
                continue
            dest = assets_dir / Path(asset_file).name
            if _safe_copy(src, dest, asset_file):
                log.append(
                    f"[INFO]   Copied declared asset: {asset_file} "
                    f"(type: {asset_entry.get('type', 'unknown')})"
                )
                asset_count += 1

    # Priority 2: filename keyword scan.
    for sub in _SCAN_SUBDIRS:
        base = workdir if sub == "" else workdir / sub
        if not base.is_dir():
            continue
        for pattern in _ASSET_PATTERNS:
            for f in base.glob(pattern):
                if f.name in _ASSET_EXCLUDE:
                    continue
                if not _safe_size_ok(f):
                    continue
                if not _ASSET_NAME_RE.search(f.name.lower()):
                    continue
                dest = assets_dir / f.name
                if dest.exists():
                    if str(base) != str(workdir):
                        log.append(
                            f"[INFO]   Skipping duplicate asset: {f.name} "
                            f"(also present in {sub}/)"
                        )
                    continue
                src_label = f.name if sub == "" else f"{f.name} (from {sub}/)"
                if _safe_copy(f, dest, f.name):
                    log.append(f"[INFO]   Copied asset: {src_label}")
                    asset_count += 1

    if asset_count == 0:
        # Tailor the WARN per task family — same as the legacy script.
        tasks_needing_labels = {
            "image-classification", "object-detection", "ocr",
        }
        if (taxonomy_task or "") in tasks_needing_labels:
            scanned_dirs = ", ".join(
                str(workdir if sub == "" else workdir / sub)
                for sub in _SCAN_SUBDIRS
                if (workdir if sub == "" else workdir / sub).is_dir()
            )
            log.append(
                f"[WARN]   No label files found for task "
                f"'{taxonomy_task}'!"
            )
            log.append(
                "[WARN]   Classification/detection models need a labels "
                "file (e.g., imagenet_classes.txt, coco.names) to "
                "display human-readable class names in App Builder."
            )
            log.append(f"[WARN]   Searched in: {scanned_dirs}")
            log.append(
                "[WARN]   Expected filenames: imagenet_labels.txt, "
                "labels.txt, imagenet_classes.txt, classes.txt, "
                "coco.names"
            )
        else:
            log.append("[INFO]   No label/vocab assets found in workspace")
    else:
        log.append(f"[INFO]   Total assets collected: {asset_count}")

    return asset_count, log
