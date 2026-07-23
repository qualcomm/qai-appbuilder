#!/usr/bin/env python3
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""aihub_to_manifest.py — bridge AI Hub downloads into the model-builder export chain.

AI Hub ships prebuilt QNN/DLC packages whose on-disk layout does NOT match
what ``qai_pack_export.py`` expects: the context binary is named arbitrarily
(``model.bin`` / ``encoder.bin`` / a ``.dlc`` inside a nested folder) and there
is no ``inference_manifest.json``. This script normalises a downloaded AI Hub
workspace so the *existing* model-builder export chain can consume it unchanged:

    ExportPackUseCase -> QaiPackExporter.export
        (src/qai/model_builder/application/use_cases/export_pack.py,
         src/qai/model_builder/adapters/qai_pack_exporter.py)

It does two things and NOTHING else (it never re-implements the exporter):

1. Reads the AI Hub ``metadata.json`` (produced by the AI Hub export/download)
   and derives a standard ``inference_manifest.json`` per the field spec in
   ``factory/chat_features/model-builder/references/pack_export.md``. Layout
   (NHWC vs NCHW) is detected from the tensor shape; AI Hub QNN DLC packages
   are typically NHWC while the model-builder convention is NCHW.

2. Normalises the workspace layout so the exporter's Step 3
   (``_locate_context_binaries`` -> ``find_context_binary``) succeeds: creates
   ``<workdir>/output/`` (if absent) and *copies* (never moves — the original
   download is preserved) the chosen context binary to the exact name
   ``{model_name}_{label}.bin`` that ``find_context_binary`` probes for.

After this script succeeds, the AI Hub skill agent runs the unmodified:

    python factory/chat_features/model-builder/scripts/qai_pack_export.py \
        --workdir <same workdir> --model-name <name> --precision <label>

and gets a full ``app_pack/`` (manifest.json / runner.py / weights / assets /
examples / provenance / _candidate.json) with no changes to the export chain.

Standard library only (json / pathlib / shutil / argparse / hashlib). Py 3.13.
"""

from __future__ import annotations

import sys

# Windows console is cp1252/GBK by default; force UTF-8 so CJK / arrows print.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Precision plan-key <-> filename-label mapping.
#
# MIRRORS (does not import) src/qai/model_builder/domain/value_objects.py so
# this bridge stays a leaf script with no dependency on the src/qai package
# (constraint: do not touch src/qai; keep stdlib-only). ``find_context_binary``
# probes BOTH ``<model>_<label>.bin`` (tried first) and ``<model>_<plan>.bin``,
# so naming the copied binary with the *label* form is what the exporter
# canonically looks up first.
# ---------------------------------------------------------------------------
_PLAN_TO_LABEL: dict[str, str] = {
    "fp16": "fp16",
    "fp32": "fp32",
    "w8a8": "int8",
    "w8a16": "w8a16",
    "w8a8b8": "int8",
    "w4a16": "w4a16",
    "w4a8": "int4",
}
_LABEL_TO_PLAN: dict[str, str] = {
    "fp16": "fp16",
    "fp32": "fp32",
    "int8": "w8a8",
    "w8a16": "w8a16",
    "w4a16": "w4a16",
    "int4": "w4a8",
}

# The exporter rejects context binaries smaller than this (stub guard).
MIN_CONTEXT_BIN_SIZE = 1 * 1024 * 1024  # 1 MiB — mirrors value_objects.py

# AI Hub context-binary candidate extensions, in preference order.
_BIN_EXTENSIONS: tuple[str, ...] = (".bin", ".dlc", ".serialized.bin")


class GuessLog:
    """Collects fields that were inferred (not read from metadata) so we can
    print a clear "please confirm these" block on stdout (State-Truth-First:
    never silently pretend a guessed value is authoritative)."""

    def __init__(self) -> None:
        self._items: list[tuple[str, Any, str]] = []

    def add(self, field: str, value: Any, reason: str) -> None:
        self._items.append((field, value, reason))

    def render(self) -> str:
        if not self._items:
            return "  (none — every emitted field was read from metadata or given on the CLI)"
        lines = []
        for field, value, reason in self._items:
            lines.append(f"  - {field} = {value!r}  ({reason})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Precision helpers
# ---------------------------------------------------------------------------

def precision_label(token: str) -> str:
    """Return the filename-suffix *label* form for a precision token.

    Accepts either plan form (``w8a8``) or label form (``int8``); returns the
    label form used by ``find_context_binary`` as its first probe. Unknown
    tokens pass through unchanged so the downstream exporter surfaces a
    readable "Unknown precision" rather than this script guessing.
    """
    t = token.strip().lower()
    if t in _PLAN_TO_LABEL:
        return _PLAN_TO_LABEL[t]
    if t in _LABEL_TO_PLAN:
        return t
    return t


def precision_plan_key(token: str) -> str:
    """Return the *plan-key* form (``w8a8``) for a precision token."""
    t = token.strip().lower()
    if t in _PLAN_TO_LABEL:
        return t
    if t in _LABEL_TO_PLAN:
        return _LABEL_TO_PLAN[t]
    return t


# ---------------------------------------------------------------------------
# metadata.json discovery + parsing
# ---------------------------------------------------------------------------

def find_metadata(workdir: Path, explicit: Path | None) -> Path:
    """Locate the AI Hub ``metadata.json``.

    Priority: explicit ``--metadata`` > ``<workdir>/metadata.json`` >
    first ``metadata.json`` found in any immediate subdirectory (AI Hub
    zips extract into a nested ``<model>-<soc>-qnn_dlc-<prec>/`` folder).
    """
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"--metadata not found: {explicit}")
        return explicit
    direct = workdir / "metadata.json"
    if direct.is_file():
        return direct
    # Search one level of subdirs (deterministic order).
    for sub in sorted(p for p in workdir.iterdir() if p.is_dir()):
        cand = sub / "metadata.json"
        if cand.is_file():
            return cand
    # Last resort: recursive glob (still deterministic via sorted()).
    hits = sorted(workdir.rglob("metadata.json"))
    if hits:
        return hits[0]
    raise FileNotFoundError(
        f"no metadata.json under {workdir} (pass --metadata explicitly)"
    )


def load_metadata(path: Path) -> dict[str, Any]:
    """Read metadata.json (UTF-8, BOM-tolerant). Returns {} shape-guarded dict.

    Uses ``utf-8-sig`` on read so a metadata.json that happens to carry a
    leading UTF-8 BOM (some Windows-produced JSON does) is accepted instead of
    failing with "Unexpected UTF-8 BOM". A BOM-free file decodes identically.
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"metadata.json is not a JSON object: {path}")
    return data


def _iter_io_entries(section: Any) -> list[dict[str, Any]]:
    """Normalise the many shapes AI Hub uses for input/output tensor specs
    into a flat list of ``{name, shape, dtype}`` dicts.

    Observed shapes across AI Hub packages:
      * dict keyed by tensor name -> {shape, dtype, ...}
      * list of {name, shape, dtype}
      * dict with an ``inputs``/``outputs`` list nested inside
    """
    entries: list[dict[str, Any]] = []
    if section is None:
        return entries
    if isinstance(section, dict):
        # Nested {"inputs": [...]} / {"outputs": [...]}.
        for key in ("inputs", "outputs", "tensors"):
            if isinstance(section.get(key), list):
                return _iter_io_entries(section[key])
        for name, spec in section.items():
            if isinstance(spec, dict):
                e = dict(spec)
                e.setdefault("name", name)
                entries.append(e)
    elif isinstance(section, list):
        for spec in section:
            if isinstance(spec, dict):
                entries.append(dict(spec))
    return entries


def _extract_shape(entry: dict[str, Any]) -> list[int] | None:
    """Pull a shape list out of an IO entry, tolerating key variants."""
    for key in ("shape", "dims", "dimensions"):
        val = entry.get(key)
        if isinstance(val, (list, tuple)) and val:
            try:
                return [int(x) for x in val]
            except (TypeError, ValueError):
                continue
    return None


def _extract_dtype(entry: dict[str, Any]) -> str | None:
    for key in ("dtype", "type", "data_type", "datatype"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_dtype(val.strip())
    return None


def _normalize_dtype(raw: str) -> str:
    """Map AI Hub / QNN dtype strings to numpy-style names used in manifests."""
    t = raw.lower().replace("qnn_datatype_", "").replace("qnn_", "")
    table = {
        "float32": "float32", "float": "float32", "fp32": "float32", "f32": "float32",
        "float_32": "float32", "float16": "float16", "fp16": "float16", "f16": "float16",
        "float_16": "float16", "uint8": "uint8", "ufixed_point_8": "uint8",
        "sfixed_point_8": "int8", "int8": "int8", "uint16": "uint16",
        "ufixed_point_16": "uint16", "sfixed_point_16": "int16", "int16": "int16",
        "int32": "int32", "int_32": "int32", "uint32": "uint32",
    }
    return table.get(t, raw)


def detect_layout(shape: list[int]) -> str:
    """Infer NCHW vs NHWC from a 4-D image tensor shape.

    Heuristic (matches AI Hub QNN DLC = NHWC vs model-builder NCHW):
      * len != 4               -> "NCHW" (default, no spatial layout meaning)
      * [N, C, H, W] where dim1 in {1,3,4} and dim3==dim2 large -> NCHW
      * [N, H, W, C] where dim3 in {1,3,4}                      -> NHWC
    Channel count is the strongest signal (1/3/4 typical for image channels).
    """
    if len(shape) != 4:
        return "NCHW"
    _, d1, d2, d3 = shape
    channel_like = {1, 3, 4}
    d1_is_ch = d1 in channel_like
    d3_is_ch = d3 in channel_like
    if d3_is_ch and not d1_is_ch:
        return "NHWC"
    if d1_is_ch and not d3_is_ch:
        return "NCHW"
    # Ambiguous (e.g. [1,3,3,3]); prefer NHWC for AI Hub QNN DLC packages,
    # which is the documented AI Hub default (SKILL Issue 15).
    return "NHWC"


# ---------------------------------------------------------------------------
# context-binary discovery
# ---------------------------------------------------------------------------

def find_source_binary(
    workdir: Path,
    metadata_dir: Path,
    metadata: dict[str, Any],
) -> Path:
    """Locate the AI Hub context binary to normalise.

    Search order:
      1. explicit metadata field (``context_binary`` / ``model_file`` / ``bin``)
         resolved relative to the metadata dir then the workdir;
      2. any ``*.bin`` / ``*.dlc`` beside metadata.json meeting the min-size
         guard (largest wins — the real weights, not a stub);
      3. recursive search under workdir (largest qualifying file wins).
    """
    # 1. Metadata-declared path.
    for key in ("context_binary", "model_file", "bin", "model", "dlc"):
        val = metadata.get(key)
        if isinstance(val, str) and val.strip():
            for base in (metadata_dir, workdir):
                cand = (base / val).resolve()
                if cand.is_file():
                    return cand

    # 2 & 3. Scan for the largest qualifying binary.
    candidates: list[Path] = []
    for base in (metadata_dir, workdir):
        if base.is_dir():
            for ext in _BIN_EXTENSIONS:
                candidates.extend(base.glob(f"*{ext}"))
    if not candidates:
        for ext in _BIN_EXTENSIONS:
            candidates.extend(workdir.rglob(f"*{ext}"))

    qualifying = [
        p for p in dict.fromkeys(candidates)  # de-dup, preserve order
        if p.is_file() and _safe_size(p) >= MIN_CONTEXT_BIN_SIZE
    ]
    if not qualifying:
        raise FileNotFoundError(
            f"no context binary (*.bin / *.dlc >= {MIN_CONTEXT_BIN_SIZE} bytes) "
            f"found under {workdir}"
        )
    # Largest = the real weights.
    return max(qualifying, key=_safe_size)


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# manifest construction
# ---------------------------------------------------------------------------

_OUTPUT_TYPE_TO_POST = {
    "classification": "softmax_topk",
    "detection": "nms",
    "segmentation": "argmax_mask",
    "super_resolution": "image_rescale",
    "text": "ctc_decode",
    "audio": "none",
    "raw": "none",
}


def build_manifest(
    *,
    model_name: str,
    precision_token: str,
    metadata: dict[str, Any],
    context_binary_rel: str,
    output_type: str | None,
    vendor: str | None,
    num_classes: int | None,
    guesses: GuessLog,
) -> dict[str, Any]:
    """Map AI Hub metadata into the standard inference_manifest.json schema."""
    plan_key = precision_plan_key(precision_token)

    # ---- input ----------------------------------------------------------
    in_entries = _iter_io_entries(metadata.get("input") or metadata.get("inputs"))
    in_shape: list[int] | None = None
    in_dtype: str | None = None
    if in_entries:
        in_shape = _extract_shape(in_entries[0])
        in_dtype = _extract_dtype(in_entries[0])

    if in_shape is None:
        in_shape = [1, 3, 224, 224]
        guesses.add("input.shape", in_shape, "no shape in metadata; assumed 224x224 RGB NCHW")
    if in_dtype is None:
        in_dtype = "float32"
        guesses.add("input.dtype", in_dtype, "no dtype in metadata; assumed float32")

    # Layout: honor explicit metadata layout, else infer from shape.
    layout_raw = metadata.get("layout") or metadata.get("input_layout")
    if isinstance(layout_raw, str) and layout_raw.strip().upper() in ("NCHW", "NHWC"):
        input_format = layout_raw.strip().upper()
    else:
        input_format = detect_layout(in_shape)
        guesses.add(
            "input.format", input_format,
            f"inferred from shape {in_shape} (AI Hub QNN DLC defaults to NHWC)",
        )

    # ---- output ---------------------------------------------------------
    out_entries = _iter_io_entries(metadata.get("output") or metadata.get("outputs"))
    out_shape: list[int] | None = None
    if out_entries:
        out_shape = _extract_shape(out_entries[0])

    resolved_output_type = output_type
    if resolved_output_type is None:
        resolved_output_type = "classification"
        guesses.add(
            "output.type", resolved_output_type,
            "not given via --output-type; defaulted to classification "
            "(pass --output-type to override; drives the runner template)",
        )

    # num_classes: CLI > last output dim (classification) > guess.
    resolved_num_classes = num_classes
    if resolved_num_classes is None and resolved_output_type in ("classification", "detection"):
        if out_shape:
            resolved_num_classes = int(out_shape[-1])
            guesses.add(
                "output.num_classes", resolved_num_classes,
                f"read from output shape last dim {out_shape}",
            )
        else:
            resolved_num_classes = 1000
            guesses.add(
                "output.num_classes", resolved_num_classes,
                "no output shape in metadata; assumed ImageNet-1000",
            )

    postprocessing = _OUTPUT_TYPE_TO_POST.get(resolved_output_type, "none")

    # ---- preprocessing (always partly inferred) -------------------------
    preprocessing: dict[str, Any] = {
        "resize_method": (
            "shortest_edge_then_center_crop"
            if resolved_output_type == "classification"
            else "resize_to_exact"
        ),
        "normalize": False,
        "scale": 255.0,
    }
    # Spatial size from the H/W of the shape (layout-aware).
    if len(in_shape) == 4:
        if input_format == "NHWC":
            resize_size = in_shape[1]
        else:  # NCHW
            resize_size = in_shape[2]
        preprocessing["resize_size"] = int(resize_size)
    guesses.add(
        "input.preprocessing", "resize/normalize/scale defaults",
        "AI Hub metadata rarely carries mean/std; defaulted to divide-by-255, "
        "no mean/std normalization — set manually if the model expects ImageNet "
        "mean/std",
    )

    # ---- assets (labels) — cannot be derived from metadata --------------
    assets: list[dict[str, Any]] = []
    if resolved_output_type in ("classification", "detection"):
        guesses.add(
            "assets", "[] (empty)",
            "label file (e.g. imagenet_classes.txt / labels.txt) is not "
            "described in metadata; the exporter's asset collector will pick "
            "up labels.txt if present in the workspace, otherwise add it "
            "manually to assets[]",
        )

    vendor_val = vendor if vendor is not None else str(metadata.get("vendor") or "")
    if not vendor_val:
        guesses.add("vendor", "", "not in metadata / not given via --vendor")

    manifest: dict[str, Any] = {
        "model_name": model_name,
        "precision": plan_key,
        "inference_script": f"infer_{model_name}.py",
        "context_binary": context_binary_rel,
        "vendor": vendor_val,
        "input": {
            "shape": in_shape,
            "format": input_format,
            "dtype": in_dtype,
            "preprocessing": preprocessing,
        },
        "output": {
            "type": resolved_output_type,
            "postprocessing": postprocessing,
        },
        "assets": assets,
        "notes": (
            "Generated by aihub_to_manifest.py from AI Hub metadata.json. "
            "Fields flagged in the tool's 'inferred fields' report should be "
            "human-verified before production use."
        ),
    }
    if resolved_num_classes is not None:
        manifest["output"]["num_classes"] = resolved_num_classes

    return manifest


# ---------------------------------------------------------------------------
# I/O — always UTF-8, no BOM, LF newlines
# ---------------------------------------------------------------------------

def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Write inference_manifest.json as UTF-8 (no BOM), LF newlines."""
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def normalize_output_dir(
    workdir: Path,
    source_bin: Path,
    model_name: str,
    precision_token: str,
) -> tuple[Path, str]:
    """Create ``output/`` and copy the source weight to the exporter-expected
    ``{model_name}_{label}<ext>`` name, PRESERVING the source extension.
    Returns (dest_path, relative_posix).

    The app_pack contract is format-neutral: a QNN context binary (``.bin``)
    and a QNN DLC (``.dlc``) are both valid NPU weights — ``QNNContext`` loads
    either directly. We therefore keep the download's REAL extension (``.dlc``
    stays ``.dlc``) instead of renaming it to ``.bin``. Renaming a ``.dlc`` to
    ``.bin`` would be "lying about the format" (State-Truth-First violation):
    the runtime tolerates it only because the native SDK sniffs content, but
    every ``.bin``-assuming tool downstream would be misled. The exporter's
    ``find_context_binary`` probes both ``.bin`` and ``.dlc``, so preserving
    the extension still produces the SAME app_pack as a Model-Builder ``.bin``
    (identical manifest schema; only the weight filename suffix differs).

    Uses shutil.copy2 (copy, never move — the original download is preserved).
    Idempotent: if an up-to-date copy already exists it is left in place.
    """
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    label = precision_label(precision_token)
    # Preserve the source weight's real extension (.bin / .dlc). ``.serialized.bin``
    # collapses to ``.bin`` for the canonical single-suffix dest name.
    ext = source_bin.suffix.lower()
    if ext not in (".bin", ".dlc"):
        ext = ".bin"
    dest = output_dir / f"{model_name}_{label}{ext}"
    # Copy unless an identically-sized copy already exists (idempotent re-run).
    if not (dest.is_file() and _safe_size(dest) == _safe_size(source_bin)):
        shutil.copy2(source_bin, dest)
    rel = f"output/{dest.name}"
    return dest, rel


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="aihub_to_manifest.py",
        description=(
            "Normalise an AI Hub download into a model-builder-compatible "
            "workspace (inference_manifest.json + output/<model>_<label>.bin) "
            "so qai_pack_export.py can export it to App Builder unchanged."
        ),
    )
    p.add_argument("--workdir", required=True, type=Path,
                   help="AI Hub download dir, e.g. C:/WoS_AI/<model>")
    p.add_argument("--model-name", required=True,
                   help="Canonical model name (used for filenames + manifest)")
    p.add_argument("--precision", required=True,
                   help="Precision token: w8a8 / float / fp16 / w8a16 / ...")
    p.add_argument("--metadata", type=Path, default=None,
                   help="Path to metadata.json (default: auto-find in workdir)")
    p.add_argument("--output-type", default=None,
                   choices=["classification", "detection", "segmentation",
                            "super_resolution", "text", "audio", "raw"],
                   help="output.type (drives runner template; inferred if omitted)")
    p.add_argument("--vendor", default=None,
                   help="Model author/org (e.g. Google, Meta). Default: metadata/empty")
    p.add_argument("--num-classes", type=int, default=None,
                   help="Override output.num_classes (classification/detection)")
    return p.parse_args(argv)


def _normalize_precision_token(token: str) -> str:
    """Accept the AI Hub 'float' alias -> fp16 (its floating package label)."""
    t = token.strip().lower()
    if t in ("float", "fp", "floating"):
        return "fp16"
    return t


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workdir: Path = args.workdir.resolve()
    model_name: str = args.model_name
    precision_token = _normalize_precision_token(args.precision)

    if not workdir.is_dir():
        print(f"[ERROR] workdir not found: {workdir}")
        return 2

    guesses = GuessLog()

    print("=== aihub_to_manifest ===")
    print(f"[INFO] workdir     : {workdir}")
    print(f"[INFO] model_name  : {model_name}")
    print(f"[INFO] precision   : {precision_token} "
          f"(label={precision_label(precision_token)}, "
          f"plan={precision_plan_key(precision_token)})")

    # 1. metadata.json
    try:
        meta_path = find_metadata(workdir, args.metadata)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 2
    metadata = load_metadata(meta_path)
    print(f"[INFO] metadata    : {meta_path}")

    # 2. locate + normalise context binary
    try:
        source_bin = find_source_binary(workdir, meta_path.parent, metadata)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 2
    dest_bin, context_rel = normalize_output_dir(
        workdir, source_bin, model_name, precision_token,
    )
    print(f"[INFO] source bin  : {source_bin} ({_safe_size(source_bin):,} bytes)")
    print(f"[INFO] copied ->   : {dest_bin} (exporter-expected name)")

    # 3. build + write manifest
    manifest = build_manifest(
        model_name=model_name,
        precision_token=precision_token,
        metadata=metadata,
        context_binary_rel=context_rel,
        output_type=args.output_type,
        vendor=args.vendor,
        num_classes=args.num_classes,
        guesses=guesses,
    )
    manifest_path = workdir / "inference_manifest.json"
    write_manifest(manifest_path, manifest)
    print(f"[INFO] manifest    : {manifest_path}")
    print(f"[INFO] input.format: {manifest['input']['format']} "
          f"shape={manifest['input']['shape']}")
    print(f"[INFO] output.type : {manifest['output']['type']}")

    # 4. inferred-fields disclosure (State-Truth-First)
    print("")
    print("=== The following fields were INFERRED — please human-verify ===")
    print(guesses.render())

    # 5. next step
    print("")
    print("=== Next step (unchanged model-builder export chain) ===")
    print(
        "  python factory/chat_features/model-builder/scripts/qai_pack_export.py "
        f"--workdir \"{workdir}\" --model-name \"{model_name}\" "
        f"--precision \"{precision_plan_key(precision_token)}\""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
