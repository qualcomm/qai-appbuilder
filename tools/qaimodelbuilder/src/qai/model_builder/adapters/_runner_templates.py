# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runner template strings for emitted Packs.

This module is a verbatim port of the ten ``_runner_*`` template
generators from ``features/model-builder/scripts/qai_pack_export.py``
(legacy lines 700-3052). Each function returns a self-contained
``runner.py`` source string for a specific category of model:

* :func:`runner_classify`        -- image classification (softmax + top-K)
* :func:`runner_detect`          -- YOLO-style object detection (NMS)
* :func:`runner_sr`              -- super-resolution (with tiling)
* :func:`runner_segment`         -- semantic segmentation
* :func:`runner_pose`            -- pose estimation (heatmap -> keypoints)
* :func:`runner_depth`           -- depth estimation (turbo colormap)
* :func:`runner_audio_classify`  -- audio classification
* :func:`runner_audio_enhance`   -- audio enhancement / denoising
* :func:`runner_speaker_verify`  -- speaker verification (cosine similarity)
* :func:`runner_generic`         -- generic fallback for unsupported categories

The ``render_runner`` dispatcher routes a model to the right template
based on (category, taxonomy task, output kind, model-name keyword)
in the same order as the legacy ``generate_runner_py``.

The template bodies are kept verbatim so the on-disk ``runner.py``
produced by the new exporter is byte-identical to what the legacy
script would have written, given the same inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = [
    "render_runner",
    "runner_classify",
    "runner_detect",
    "runner_sr",
    "runner_segment",
    "runner_pose",
    "runner_depth",
    "runner_audio_classify",
    "runner_audio_enhance",
    "runner_speaker_verify",
    "runner_generic",
    "is_detection_model",
    "is_segmentation_model",
]


_WELL_KNOWN_INPUT_SIZE: tuple[tuple[str, int, int], ...] = (
    ("inception_v3", 299, 299),
    ("inceptionv3",  299, 299),
    ("inception_v4", 299, 299),
    ("inceptionv4",  299, 299),
    ("xception",     299, 299),
    ("inception_resnet", 299, 299),
    ("efficientnet_b0", 224, 224),
    ("efficientnet_b1", 240, 240),
    ("efficientnet_b2", 260, 260),
    ("efficientnet_b3", 300, 300),
    ("efficientnet_b4", 380, 380),
    ("efficientnet_b5", 456, 456),
    ("efficientnet_b6", 528, 528),
    ("efficientnet_b7", 600, 600),
    ("nasnet_large",   331, 331),
    ("pnasnet",        331, 331),
)


_DETECTION_KEYWORDS: tuple[str, ...] = (
    "yolo", "yolov", "ssd", "fcos", "detr", "faster_rcnn",
    "retinanet", "centernet", "nanodet", "efficientdet",
    "blazeface", "facedet", "face_det",
)

_SEGMENTATION_KEYWORDS: tuple[str, ...] = (
    "deeplab", "deeplabv3", "segformer", "unet", "u_net",
    "fcn", "pspnet", "maskrcnn", "mask_rcnn", "panoptic",
    "semseg", "segnet", "bisenet", "hrnet_seg",
)


def is_detection_model(model_name: str) -> bool:
    """Heuristic: model name suggests an object-detection model."""
    name = (model_name or "").lower().replace("-", "_")
    return any(kw in name for kw in _DETECTION_KEYWORDS)


def is_segmentation_model(model_name: str) -> bool:
    """Heuristic: model name suggests a semantic-segmentation model."""
    name = (model_name or "").lower().replace("-", "_")
    return any(kw in name for kw in _SEGMENTATION_KEYWORDS)


def _resolve_input_hw(
    model_name: str,
    input_shape: list[int] | None,
) -> tuple[int, int]:
    """Pick (H, W) from input_shape or the well-known fallback table."""
    if input_shape and len(input_shape) == 4:
        # NCHW (legacy default).
        return int(input_shape[2]), int(input_shape[3])
    name_lc = (model_name or "").lower()
    for sub, h, w in _WELL_KNOWN_INPUT_SIZE:
        if sub in name_lc:
            return h, w
    return 224, 224


def render_runner(
    *,
    pack_id: str,
    model_name: str,
    input_kind: str,
    output_kind: str,
    category: str,
    weights_filename: str,
    input_shape: list[int] | None = None,
    precision: str = "fp16",
    infer_manifest: dict[str, Any] | None = None,
) -> str:
    """Pick the right per-category template and render the runner source.

    Routing rules (kept identical to the legacy ``generate_runner_py``):

    * ``category == "SR"`` -> super-resolution template;
    * ``category == "CV"`` -> sub-routed by ``infer_manifest.output.type``
      (detection / pose_estimation / segmentation / depth_estimation /
      else classification); model-name keyword fallback for detection +
      segmentation;
    * ``infer_manifest.output.type`` directly addresses the audio /
      depth families when ``category`` is something else;
    * everything else -> generic template.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    input_h, input_w = _resolve_input_hw(model_name, input_shape)

    preproc = (
        ((infer_manifest or {}).get("input") or {}).get("preprocessing") or {}
    )
    use_normalize = bool(preproc.get("normalize", True))
    normalize_mean = preproc.get("mean", [0.485, 0.456, 0.406])
    normalize_std = preproc.get("std", [0.229, 0.224, 0.225])
    resize_method = preproc.get(
        "resize_method", "shortest_edge_then_center_crop"
    )
    subcategory = (
        ((infer_manifest or {}).get("output") or {}).get("type", "")
    ) or ""

    if category == "SR":
        return runner_sr(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
    if category == "CV":
        if subcategory in ("detection", "object_detection") or is_detection_model(model_name):
            return runner_detect(
                pack_id, model_name, weights_filename, input_h, input_w, timestamp,
                normalize_mean, normalize_std, use_normalize, infer_manifest,
            )
        if subcategory == "pose_estimation":
            return runner_pose(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
        if (
            subcategory in ("segmentation", "image_editing")
            or output_kind == "image"
            or is_segmentation_model(model_name)
        ):
            return runner_segment(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
        if subcategory == "depth_estimation":
            return runner_depth(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
        return runner_classify(
            pack_id, model_name, weights_filename, input_h, input_w, timestamp,
            normalize_mean, normalize_std, use_normalize, resize_method,
        )
    if subcategory == "depth_estimation":
        return runner_depth(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
    if subcategory == "audio_classification":
        return runner_audio_classify(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
    if subcategory == "audio_enhancement":
        return runner_audio_enhance(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
    if subcategory == "speaker_verification":
        return runner_speaker_verify(pack_id, model_name, weights_filename, input_h, input_w, timestamp)
    return runner_generic(
        pack_id, model_name, weights_filename, input_kind, output_kind, category,
        input_h, input_w, timestamp, infer_manifest,
    )


# ---------------------------------------------------------------------------
# Per-category template generators (verbatim ports from the legacy script)
# ---------------------------------------------------------------------------

def runner_classify(pack_id, model_name, weights_filename, input_h, input_w, timestamp,
                     normalize_mean=None, normalize_std=None, use_normalize=True, resize_method="shortest_edge_then_center_crop"):
    """Generate a working image classification runner (softmax + top-K).

    NOTE on shape handling:
        The generated runner does NOT trust the ``input_h``/``input_w`` baked
        in here at export time. They are kept only as a *fallback* in case
        the live ``getInputShapes()`` call fails (very old qai_appbuilder
        builds). At runtime, the SSOT is the live qai_appbuilder native API
        — see io_validator.extract_io_contract() and cv_input_hw().
    """
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Image Classification

Shape contract:
    Inputs/outputs are discovered at runtime from the loaded .bin via
    qai_appbuilder native getters. The static manifest.io_contract written
    by qai_pack_export is cross-checked against the live model on load;
    any mismatch raises CONTRACT_MISMATCH instead of crashing at memcpy.

    All tensors handed to ctx.run() pass through io_validator.validate_inputs()
    which guarantees correct dtype, shape, layout, and C-contiguity.
"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer
from image_io import read_image
from io_validator import (
    IOContractError, extract_io_contract, assert_contracts_compatible,
    validate_inputs, cv_input_hw,
)

WEIGHTS_FILENAME = "{weights_filename}"

# Fallback values used ONLY if the live native getter is unavailable. The
# preferred path reads cv_input_hw(live_contract) at runtime.
_FALLBACK_INPUT_H, _FALLBACK_INPUT_W = {input_h}, {input_w}

IMAGENET_MEAN = np.array({normalize_mean or [0.485, 0.456, 0.406]}, dtype=np.float32)
IMAGENET_STD = np.array({normalize_std or [0.229, 0.224, 0.225]}, dtype=np.float32)
USE_NORMALIZE = {use_normalize}
TOP_K = 5

class _UserError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or None

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_image(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("image")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input image not found: {{raw}}")
    return p

def _load_static_contract(pack_dir):
    """Read manifest.io_contract for cross-check against live model."""
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        return m.get("io_contract")
    except (OSError, ValueError):
        return None

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend
    #      (P1: backend/app_builder/runners/python_script.py).
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    # 1. Absolute path injected by backend (preferred).
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    # 2. variants[] lookup by id.
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = ((v.get("assets") or {{}}).get("installPath"))
                if ip:
                    candidates.append((repo_root / ip).resolve()
                                      if not Path(ip).is_absolute()
                                      else Path(ip))
                break
    # 3. legacy top-level assets.installPath.
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append((repo_root / install_rel).resolve()
                              if not Path(install_rel).is_absolute()
                              else Path(install_rel))
    # 4 & 5. legacy hard-coded fallbacks.
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _preprocess(image_hwc_uint8, input_h, input_w):
    """Image preprocessing parameterised by the LIVE input H/W."""
    from PIL import Image
    h, w = image_hwc_uint8.shape[:2]
    if h < w:
        new_h, new_w = input_h, int(round(w * input_h / h))
    else:
        new_w, new_h = input_w, int(round(h * input_w / w))
    pil_img = Image.fromarray(image_hwc_uint8).resize((new_w, new_h), Image.BILINEAR)
    left = (new_w - input_w) // 2
    top = (new_h - input_h) // 2
    pil_img = pil_img.crop((left, top, left + input_w, top + input_h))
    img = np.array(pil_img, dtype=np.float32) / 255.0
    if USE_NORMALIZE:
        img = (img - IMAGENET_MEAN) / IMAGENET_STD
    img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
    return img  # io_validator will enforce contiguity below.

def _softmax(logits):
    x = logits - np.max(logits, axis=-1, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=-1, keepdims=True)

def _postprocess(output, labels=None):
    logits = output.flatten()
    probs = _softmax(logits)
    top_indices = np.argsort(probs)[::-1][:TOP_K]
    predictions = []
    for idx in top_indices:
        entry = {{"class_idx": int(idx), "score": float(probs[idx])}}
        if labels and 0 <= idx < len(labels):
            entry["label"] = labels[idx]
        predictions.append(entry)
    return predictions

def _load_labels(pack_dir):
    search_dirs = [pack_dir, pack_dir / "assets"]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for name in ("imagenet_labels.txt", "labels.txt", "imagenet_classes.txt", "synset_words.txt", "classes.txt", "coco.names"):
            p = d / name
            if p.is_file():
                try:
                    lines = p.read_text(encoding="utf-8").strip().splitlines()
                    labels = []
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        parts = line.split(":", 1)
                        if len(parts) == 2 and parts[0].strip().isdigit():
                            labels.append(parts[1].strip())
                        else:
                            labels.append(line)
                    if len(labels) >= 10:
                        return labels
                except (OSError, ValueError):
                    continue
    return None

def main():
    req = read_request()
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_image(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    timer = StageTimer(device="htp")
    status("preparing")
    labels = _load_labels(pack_dir)
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")

    # ── I/O contract: live + static cross-check ──
    # SSOT is the live qai_appbuilder native API. The static manifest is a
    # secondary check — it catches the case where someone hand-replaced the
    # .bin without updating the manifest.
    try:
        native = getattr(ctx, "_ctx", None) or ctx
        live_contract = extract_io_contract(native)
    except IOContractError as e:
        # Native getters unavailable → fall back to baked H/W; still safe
        # because validate_inputs() below will catch any byte mismatch.
        live_contract = {{
            "schema_version": 1, "graph_name": "unknown",
            "inputs":  [{{"name": "input",  "dtype": "float32",
                         "shape": [1, 3, _FALLBACK_INPUT_H, _FALLBACK_INPUT_W],
                         "layout": "NCHW"}}],
            "outputs": [],
        }}
        emit({{"type": "log", "stream": "stderr",
              "line": f"[runner] WARNING: native getInputShapes() failed ({{e.message}}); using fallback {{_FALLBACK_INPUT_H}}x{{_FALLBACK_INPUT_W}}"}})

    static_contract = _load_static_contract(pack_dir)
    try:
        assert_contracts_compatible(static_contract, live_contract)
    except IOContractError as e:
        raise _UserError(e.code, e.message, detail=e.detail)

    try:
        input_h, input_w = cv_input_hw(live_contract)
    except IOContractError as e:
        raise _UserError(e.code, e.message, detail=e.detail)

    status("running")
    image = read_image(input_path)
    if image.ndim != 3 or image.shape[2] != 3:
        raise _UserError("INVALID_INPUT", f"unsupported image shape {{image.shape}}")
    with timer.stage("preprocess"):
        input_tensor = _preprocess(image, input_h, input_w)
    with timer.stage("infer"):
        # ★ SINGLE chokepoint: every native ctx.run() call is preceded by this.
        try:
            safe_inputs = validate_inputs([input_tensor], live_contract)
        except IOContractError as e:
            raise _UserError(e.code, e.message, detail=e.detail)
        try:
            outputs = ctx.run(safe_inputs)
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        predictions = _postprocess(outputs[0], labels)
    emit({{"type": "metrics", **timer.summary()}})
    result({{"predictions": predictions, "top_k": TOP_K, "num_classes": len(outputs[0].flatten()), "has_labels": labels is not None}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        # detail (when present) reaches DynamicOutput's error.detail and is
        # rendered as a structured shape-diff segment in the UI.
        kwargs = {{}}
        if ue.detail:
            kwargs["detail"] = ue.detail
        fail(code=ue.code, message=ue.message, **kwargs)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_detect(pack_id, model_name, weights_filename, input_h, input_w, timestamp,
                   normalize_mean=None, normalize_std=None, use_normalize=False, infer_manifest=None):
    """Generate a working object detection runner (YOLO-style: NMS + bounding boxes)."""
    # Extract detection-specific params from manifest
    postproc = (infer_manifest or {}).get("postprocessing", {}) if infer_manifest else {}
    conf_threshold = postproc.get("confidence_threshold", 0.45)
    iou_threshold = postproc.get("nms_iou_threshold", 0.7)
    num_classes = (infer_manifest or {}).get("output", {}).get("num_classes", 80) if infer_manifest else 80

    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Object Detection (YOLO-style)"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer
from image_io import read_image, write_image

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}
CONF_THRESHOLD = {conf_threshold}
IOU_THRESHOLD = {iou_threshold}
NUM_CLASSES = {num_classes}
USE_NORMALIZE = {use_normalize}
NORMALIZE_MEAN = np.array({normalize_mean or [0, 0, 0]}, dtype=np.float32)
NORMALIZE_STD = np.array({normalize_std or [1, 1, 1]}, dtype=np.float32)

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_image(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("image")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input image not found: {{raw}}")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _preprocess(image_hwc_uint8):
    """Resize to model input (letterbox/square), normalize, NCHW."""
    from PIL import Image
    pil_img = Image.fromarray(image_hwc_uint8).resize((INPUT_W, INPUT_H), Image.BILINEAR)
    img = np.array(pil_img, dtype=np.float32) / 255.0
    if USE_NORMALIZE:
        img = (img - NORMALIZE_MEAN) / NORMALIZE_STD
    img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on the
    # raw buffer assuming contiguous NCHW layout. A transposed view causes a
    # VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(img, dtype=np.float32)

def _nms(boxes, scores, iou_threshold):
    """Non-Maximum Suppression. boxes: (N,4) as x1,y1,x2,y2; scores: (N,)."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        inds = np.where(iou <= iou_threshold)[0]
        order = order[inds + 1]
    return keep

def _parse_yolo_output(output, orig_h, orig_w):
    """Parse YOLO output format. Handles both [1, 4+nc, num_boxes] and [1, num_boxes, 4+nc]."""
    out = np.array(output).squeeze()  # Remove batch dim
    if out.ndim != 2:
        return []
    # Detect transposed format: if dim0 < dim1 and dim0 == 4+nc
    if out.shape[0] < out.shape[1] and out.shape[0] == 4 + NUM_CLASSES:
        out = out.T  # Now (num_boxes, 4+nc)
    elif out.shape[1] < out.shape[0] and out.shape[1] == 4 + NUM_CLASSES:
        pass  # Already (num_boxes, 4+nc)
    else:
        # Try to infer: if first dim is small, it's likely (4+nc, boxes) transposed
        if out.shape[0] < out.shape[1]:
            out = out.T
    # Extract boxes and scores
    cx, cy, w, h = out[:, 0], out[:, 1], out[:, 2], out[:, 3]
    class_scores = out[:, 4:]
    # Get best class per box
    class_ids = np.argmax(class_scores, axis=1)
    scores = np.max(class_scores, axis=1)
    # Filter by confidence
    mask = scores >= CONF_THRESHOLD
    if not np.any(mask):
        return []
    cx, cy, w, h = cx[mask], cy[mask], w[mask], h[mask]
    class_ids, scores = class_ids[mask], scores[mask]
    # Convert center format to corner format, scale to original image
    x1 = (cx - w / 2) * orig_w / INPUT_W
    y1 = (cy - h / 2) * orig_h / INPUT_H
    x2 = (cx + w / 2) * orig_w / INPUT_W
    y2 = (cy + h / 2) * orig_h / INPUT_H
    boxes = np.stack([x1, y1, x2, y2], axis=1)
    # NMS per class
    keep = _nms(boxes, scores, IOU_THRESHOLD)
    detections = []
    for i in keep:
        detections.append({{
            "box": [float(boxes[i, 0]), float(boxes[i, 1]), float(boxes[i, 2]), float(boxes[i, 3])],
            "class_idx": int(class_ids[i]),
            "score": float(scores[i]),
        }})
    return detections

def _load_labels(pack_dir):
    search_dirs = [pack_dir, pack_dir / "assets"]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for name in ("coco.names", "labels.txt", "classes.txt", "coco_labels.txt", "imagenet_labels.txt"):
            p = d / name
            if p.is_file():
                try:
                    lines = [l.strip() for l in p.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
                    if len(lines) >= 2:
                        return lines
                except (OSError, ValueError):
                    continue
    return None

def main():
    req = read_request()
    run_id = req.get("runId") or "run"
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_image(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    timer = StageTimer(device="htp")
    status("preparing")
    labels = _load_labels(pack_dir)
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    image = read_image(input_path)
    if image.ndim != 3 or image.shape[2] != 3:
        raise _UserError("INVALID_INPUT", f"unsupported image shape {{image.shape}}")
    orig_h, orig_w = image.shape[:2]
    with timer.stage("preprocess"):
        input_tensor = _preprocess(image)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        detections = _parse_yolo_output(outputs[0], orig_h, orig_w)
        # Add labels if available
        if labels:
            for det in detections:
                idx = det["class_idx"]
                if 0 <= idx < len(labels):
                    det["label"] = labels[idx]
    emit({{"type": "metrics", **timer.summary(), "num_detections": len(detections)}})
    result({{"detections": detections, "count": len(detections), "image_size": [orig_w, orig_h], "has_labels": labels is not None}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_sr(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working super-resolution runner with tiling support."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Super Resolution (with tiling)"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer
from image_io import read_image, write_image

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}
TILE_OVERLAP = 16

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_image(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("image")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input image not found: {{raw}}")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _tile_to_nchw(tile_hwc):
    """Convert HWC uint8 tile to NCHW float32 [0,1]."""
    img = tile_hwc.astype(np.float32) / 255.0
    return np.ascontiguousarray(np.transpose(img, (2, 0, 1))[np.newaxis, ...], dtype=np.float32)

def _nchw_to_hwc(output):
    """Convert NCHW float [0,1] output to HWC uint8."""
    out = np.array(output).squeeze()
    if out.ndim == 3 and out.shape[0] in (1, 3, 4):
        out = np.transpose(out, (1, 2, 0))
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)

def _detect_scale(input_shape, output_shape):
    """Detect SR scale from input/output tensor shapes."""
    if len(input_shape) >= 3 and len(output_shape) >= 3:
        ih = input_shape[-2] if input_shape[-3] in (1,3,4) else input_shape[-3]
        oh = output_shape[-2] if output_shape[-3] in (1,3,4) else output_shape[-3]
        if ih > 0:
            return max(1, oh // ih)
    return 4  # default

def main():
    req = read_request()
    run_id = req.get("runId") or "run"
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_image(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    params = req.get("params") or {{}}
    tile_size = int(params.get("tile_size", INPUT_W))
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    with timer.stage("preprocess"):
        image = read_image(input_path)
        if image.ndim != 3 or image.shape[2] != 3:
            raise _UserError("INVALID_INPUT", f"unsupported image shape {{image.shape}}")
        h_in, w_in = image.shape[:2]
        # Decide: single-shot or tiled
        use_tiles = (h_in > tile_size) or (w_in > tile_size)
    tile_count = 0
    with timer.stage("infer"):
        try:
            if not use_tiles:
                # Single-shot
                inp = _tile_to_nchw(image)
                outputs = ctx.run([inp])
                out_image = _nchw_to_hwc(outputs[0])
                scale = _detect_scale(inp.shape, np.array(outputs[0]).shape)
                tile_count = 1
            else:
                # Tiled processing
                overlap = TILE_OVERLAP
                # First tile to detect scale
                first_tile = image[:tile_size, :tile_size, :]
                first_inp = _tile_to_nchw(first_tile)
                first_out = ctx.run([first_inp])
                scale = _detect_scale(first_inp.shape, np.array(first_out[0]).shape)
                out_h = h_in * scale
                out_w = w_in * scale
                out_image = np.zeros((out_h, out_w, 3), dtype=np.uint8)
                step = tile_size - overlap
                for y in range(0, h_in, step):
                    for x in range(0, w_in, step):
                        ye = min(y + tile_size, h_in)
                        xe = min(x + tile_size, w_in)
                        tile = image[y:ye, x:xe, :]
                        # Pad if needed
                        th, tw = tile.shape[:2]
                        if th < tile_size or tw < tile_size:
                            padded = np.zeros((tile_size, tile_size, 3), dtype=np.uint8)
                            padded[:th, :tw, :] = tile
                            tile = padded
                        inp = _tile_to_nchw(tile)
                        outs = ctx.run([inp])
                        out_tile = _nchw_to_hwc(outs[0])
                        # Place in output (scale coordinates)
                        oy, ox = y * scale, x * scale
                        oth = min(th * scale, out_tile.shape[0])
                        otw = min(tw * scale, out_tile.shape[1])
                        out_image[oy:oy+oth, ox:ox+otw, :] = out_tile[:oth, :otw, :]
                        tile_count += 1
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", f"Try reducing tile_size (current: {{tile_size}}). Error: {{e}}")
        except _UserError:
            raise
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        # Save output
        out_dir = repo_root / "data" / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"sr-{{run_id}}.png"
        write_image(out_path, out_image)
    h_out, w_out = out_image.shape[:2]
    emit({{"type": "metrics", **timer.summary(), "scale": scale, "tile_count": tile_count}})
    # NOTE: in_size / out_size = source image and final stitched output (pixel
    # extent the user actually sees). model_in_size / model_out_size = the
    # neural-net's own input/output tensor (tile) shape — useful for the UI
    # to show 256×256→1024×1024 distinct from the source 512×512→2048×2048.
    result({{
        "image_path": str(out_path.relative_to(repo_root)),
        "scale": scale,
        "in_size": [w_in, h_in],
        "out_size": [w_out, h_out],
        "model_in_size": [INPUT_W, INPUT_H],
        "model_out_size": [INPUT_W * scale, INPUT_H * scale],
        "tiled": use_tiles,
    }})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_segment(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working segmentation runner (image → segmented image)."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Semantic Segmentation"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer
from image_io import read_image, write_image

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_image(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("image")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input image not found: {{raw}}")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _preprocess(image_hwc_uint8):
    from PIL import Image
    pil_img = Image.fromarray(image_hwc_uint8).resize((INPUT_W, INPUT_H), Image.BILINEAR)
    img = np.array(pil_img, dtype=np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on the
    # raw buffer assuming contiguous NCHW layout. A transposed view causes a
    # VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(img, dtype=np.float32)

def _postprocess(output, original_hw, run_id, repo_root):
    """Argmax over channels, colorize mask, overlay on input, save."""
    from PIL import Image
    out = np.array(output).squeeze()
    if out.ndim == 3:
        if out.shape[0] < out.shape[1] and out.shape[0] < out.shape[2]:
            mask = np.argmax(out, axis=0)  # CHW -> argmax over C
        else:
            mask = np.argmax(out, axis=2)  # HWC -> argmax over C
    elif out.ndim == 2:
        mask = (out > 0.5).astype(np.uint8)
    else:
        mask = out.reshape(INPUT_H, INPUT_W)
    # Resize mask to original size
    h_orig, w_orig = original_hw
    mask_img = Image.fromarray(mask.astype(np.uint8)).resize((w_orig, h_orig), Image.NEAREST)
    mask_arr = np.array(mask_img)
    # Colorize with simple palette
    palette = np.array([[0,0,0],[128,0,0],[0,128,0],[128,128,0],[0,0,128],
                        [128,0,128],[0,128,128],[128,128,128],[64,0,0],[192,0,0],
                        [64,128,0],[192,128,0],[64,0,128],[192,0,128],[64,128,128],
                        [192,128,128],[0,64,0],[128,64,0],[0,192,0],[128,192,0]], dtype=np.uint8)
    color_mask = palette[mask_arr % len(palette)]
    out_dir = repo_root / "data" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"seg-{{run_id}}.png"
    write_image(out_path, color_mask)
    return out_path, int(mask_arr.max()) + 1

def main():
    req = read_request()
    run_id = req.get("runId") or "run"
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_image(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    image = read_image(input_path)
    if image.ndim != 3 or image.shape[2] != 3:
        raise _UserError("INVALID_INPUT", f"unsupported image shape {{image.shape}}")
    h_in, w_in = image.shape[:2]
    with timer.stage("preprocess"):
        input_tensor = _preprocess(image)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        out_path, num_classes = _postprocess(outputs[0], (h_in, w_in), run_id, repo_root)
    emit({{"type": "metrics", **timer.summary(), "num_classes": num_classes}})
    result({{"image_path": str(out_path.relative_to(repo_root)), "num_classes": num_classes}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_pose(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working pose estimation runner (heatmap → keypoints)."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Pose Estimation"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer
from image_io import read_image

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}

COCO_KEYPOINT_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

COCO_SKELETON = [
    [0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8],
    [8, 10], [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
]

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_image(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("image")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input image not found: {{raw}}")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _preprocess(image_hwc_uint8):
    from PIL import Image
    pil_img = Image.fromarray(image_hwc_uint8).resize((INPUT_W, INPUT_H), Image.BILINEAR)
    img = np.array(pil_img, dtype=np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on the
    # raw buffer assuming contiguous NCHW layout. A transposed view causes a
    # VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(img, dtype=np.float32)

def _postprocess(output, orig_h, orig_w):
    """Extract keypoints from heatmaps [1, num_joints, H, W]."""
    heatmaps = np.array(output).squeeze()  # Remove batch dim -> [num_joints, H, W]
    if heatmaps.ndim == 2:
        # Single joint case: expand to [1, H, W]
        heatmaps = heatmaps[np.newaxis, ...]
    num_joints = heatmaps.shape[0]
    hm_h, hm_w = heatmaps.shape[1], heatmaps.shape[2]
    scale_x = orig_w / hm_w
    scale_y = orig_h / hm_h
    keypoints = []
    for j in range(num_joints):
        hm = heatmaps[j]
        flat_idx = np.argmax(hm)
        y_hm, x_hm = divmod(int(flat_idx), hm_w)
        confidence = float(hm[y_hm, x_hm])
        x_pixel = (x_hm + 0.5) * scale_x
        y_pixel = (y_hm + 0.5) * scale_y
        name = COCO_KEYPOINT_NAMES[j] if j < len(COCO_KEYPOINT_NAMES) else f"joint_{{j}}"
        keypoints.append({{
            "id": j,
            "name": name,
            "x": round(x_pixel, 1),
            "y": round(y_pixel, 1),
            "confidence": round(confidence, 4),
        }})
    return keypoints

def main():
    req = read_request()
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_image(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    image = read_image(input_path)
    if image.ndim != 3 or image.shape[2] != 3:
        raise _UserError("INVALID_INPUT", f"unsupported image shape {{image.shape}}")
    orig_h, orig_w = image.shape[:2]
    with timer.stage("preprocess"):
        input_tensor = _preprocess(image)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        keypoints = _postprocess(outputs[0], orig_h, orig_w)
    emit({{"type": "metrics", **timer.summary()}})
    result({{
        "keypoints": keypoints,
        "skeleton": COCO_SKELETON,
        "image_size": [orig_w, orig_h],
        "num_persons": 1,
    }})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_depth(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working depth estimation runner (image → depth map PNG)."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Depth Estimation"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer
from image_io import read_image

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}

# Turbo colormap LUT (256 entries, RGB uint8) — approximation of matplotlib turbo
TURBO_COLORMAP = np.array([
    [48,18,59],[50,21,67],[51,24,74],[52,27,81],[53,30,88],[54,33,95],[55,36,102],[56,39,109],
    [57,42,115],[58,45,121],[59,47,128],[60,50,134],[61,53,139],[62,56,145],[63,59,151],[63,62,156],
    [64,64,162],[65,67,167],[65,70,172],[66,73,177],[66,75,181],[67,78,186],[68,81,191],[68,84,195],
    [68,86,199],[69,89,203],[69,92,207],[69,94,211],[70,97,214],[70,100,218],[70,102,221],[70,105,224],
    [70,107,227],[71,110,230],[71,113,233],[71,115,235],[71,118,238],[71,120,240],[71,123,242],[70,125,244],
    [70,128,246],[70,130,248],[70,133,250],[70,135,251],[69,138,252],[69,140,253],[68,143,254],[67,145,254],
    [66,148,255],[65,150,255],[64,153,255],[62,155,254],[61,158,254],[59,160,253],[58,163,252],[56,165,251],
    [55,168,250],[53,171,248],[51,173,247],[49,175,245],[47,178,244],[46,180,242],[44,183,240],[42,185,238],
    [40,188,235],[39,190,233],[37,192,231],[35,195,228],[34,197,226],[32,199,223],[31,201,221],[30,203,218],
    [28,205,216],[27,208,213],[26,210,210],[26,212,208],[25,213,205],[24,215,202],[24,217,200],[24,219,197],
    [24,221,194],[24,222,191],[24,224,189],[25,226,186],[25,227,183],[26,228,180],[28,230,177],[29,231,174],
    [31,233,171],[32,234,168],[34,235,165],[36,236,162],[38,238,159],[40,239,156],[42,240,153],[44,241,150],
    [47,242,147],[49,243,144],[52,244,141],[54,245,138],[57,246,135],[60,247,132],[63,248,129],[66,249,126],
    [69,250,123],[72,251,120],[75,252,118],[78,252,115],[82,253,112],[85,254,109],[88,254,107],[92,255,104],
    [95,255,101],[99,255,99],[102,255,96],[106,255,93],[109,255,91],[113,255,88],[116,255,86],[120,255,83],
    [124,254,81],[127,254,78],[131,253,76],[135,253,73],[138,252,71],[142,251,69],[146,250,67],[149,249,65],
    [153,248,63],[156,247,61],[160,246,59],[163,245,57],[167,244,56],[170,243,54],[174,241,53],[177,240,51],
    [181,238,50],[184,237,49],[188,235,47],[191,233,46],[194,232,45],[198,230,44],[201,228,43],[204,226,42],
    [207,224,42],[210,222,41],[213,220,40],[216,218,40],[219,216,39],[222,214,39],[224,212,39],[227,209,38],
    [229,207,38],[232,205,38],[234,203,38],[236,200,38],[238,198,38],[240,195,39],[242,193,39],[244,190,39],
    [245,188,39],[247,185,40],[248,183,40],[250,180,41],[251,178,41],[252,175,42],[253,172,42],[254,170,43],
    [254,167,44],[255,164,44],[255,162,45],[255,159,46],[255,156,47],[255,154,48],[255,151,49],[254,148,50],
    [254,145,51],[253,143,52],[253,140,53],[252,137,54],[251,135,55],[250,132,56],[249,129,58],[248,127,59],
    [247,124,60],[246,121,61],[245,119,63],[244,116,64],[243,113,65],[242,111,67],[241,108,68],[240,105,69],
    [239,103,71],[237,100,72],[236,97,74],[235,95,75],[234,92,77],[232,89,78],[231,87,80],[229,84,81],
    [228,81,83],[226,79,84],[225,76,86],[223,73,87],[221,71,89],[220,68,91],[218,65,92],[216,63,94],
    [214,60,95],[212,57,97],[210,55,98],[208,52,100],[206,50,101],[204,47,103],[201,45,104],[199,42,106],
    [197,40,107],[194,37,109],[192,35,110],[189,33,112],[187,30,113],[184,28,114],[182,26,116],[179,24,117],
    [176,22,118],[174,20,120],[171,18,121],[168,16,122],[165,15,123],[162,13,124],[159,12,126],[156,11,127],
    [153,10,128],[150,9,129],[147,8,130],[144,8,131],[141,7,132],[138,7,133],[135,7,134],[132,7,135],
    [129,7,135],[126,8,136],[123,8,137],[120,9,137],[116,9,138],[113,10,138],[110,11,139],[107,11,139],
    [104,12,139],[101,13,140],[98,13,140],[95,14,140],[92,14,140],[89,15,140],[86,15,140],[83,16,140],
], dtype=np.uint8)

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_image(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("image")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input image not found: {{raw}}")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _preprocess(image_hwc_uint8):
    from PIL import Image
    pil_img = Image.fromarray(image_hwc_uint8).resize((INPUT_W, INPUT_H), Image.BILINEAR)
    img = np.array(pil_img, dtype=np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on the
    # raw buffer assuming contiguous NCHW layout. A transposed view causes a
    # VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(img, dtype=np.float32)

def _postprocess_depth(output, original_hw, model_name, pack_dir):
    """Squeeze depth tensor, normalize to 0-255, apply turbo colormap, save PNG."""
    from PIL import Image
    depth = np.array(output).squeeze()
    if depth.ndim != 2:
        # Handle unexpected shapes: take first channel if 3D
        if depth.ndim == 3:
            depth = depth[0] if depth.shape[0] < depth.shape[-1] else depth[..., 0]
        else:
            depth = depth.reshape(INPUT_H, INPUT_W)
    h_orig, w_orig = original_hw
    min_depth = float(depth.min())
    max_depth = float(depth.max())
    # Normalize to 0-255
    depth_range = max_depth - min_depth
    if depth_range > 0:
        depth_norm = ((depth - min_depth) / depth_range * 255.0).astype(np.uint8)
    else:
        depth_norm = np.zeros_like(depth, dtype=np.uint8)
    # Resize to original image dimensions
    depth_img = Image.fromarray(depth_norm).resize((w_orig, h_orig), Image.BILINEAR)
    depth_arr = np.array(depth_img)
    # Apply turbo colormap via LUT indexing
    colored = TURBO_COLORMAP[depth_arr]
    # Save output PNG
    out_dir = pack_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{{model_name}}_depth.png"
    Image.fromarray(colored, "RGB").save(str(out_path))
    return out_path, min_depth, max_depth

def main():
    req = read_request()
    run_id = req.get("runId") or "run"
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_image(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    image = read_image(input_path)
    if image.ndim != 3 or image.shape[2] != 3:
        raise _UserError("INVALID_INPUT", f"unsupported image shape {{image.shape}}")
    h_in, w_in = image.shape[:2]
    with timer.stage("preprocess"):
        input_tensor = _preprocess(image)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        out_path, min_depth, max_depth = _postprocess_depth(outputs[0], (h_in, w_in), "{model_name}", pack_dir)
    emit({{"type": "metrics", **timer.summary(), "min_depth": min_depth, "max_depth": max_depth}})
    result({{"depth_map_path": str(out_path), "min_depth": min_depth, "max_depth": max_depth, "image_size": [w_in, h_in]}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_audio_classify(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working audio classification runner (e.g., YAMNet, AudioSet models)."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Audio Classification"""
from __future__ import annotations
import sys, traceback, json, wave
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}
TOP_K = 5

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_audio(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("audio")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.audio is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input audio not found: {{raw}}")
    ext = p.suffix.lower()
    if ext not in (".wav", ".mp3", ".flac"):
        raise _UserError("INVALID_INPUT", f"unsupported audio format: {{ext}} (supported: .wav, .mp3, .flac)")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _load_audio_wav(audio_path):
    """Load a WAV file as float32 samples in [-1, 1]."""
    with wave.open(str(audio_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)
    if sample_width == 2:
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise _UserError("INVALID_INPUT", f"unsupported sample width: {{sample_width}} bytes")
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sample_rate

def _preprocess_audio(samples, sample_rate):
    """Compute magnitude spectrogram and reshape to model input.

    NOTE: This is a simplified preprocessing pipeline. For real deployment,
    match the model's training preprocessing (e.g., proper mel filterbank,
    specific window/hop sizes, normalization).
    """
    # STFT parameters for 16kHz audio
    target_sr = 16000
    if sample_rate != target_sr:
        # Simple resampling via linear interpolation
        duration = len(samples) / sample_rate
        target_len = int(duration * target_sr)
        indices = np.linspace(0, len(samples) - 1, target_len)
        samples = np.interp(indices, np.arange(len(samples)), samples)

    window_size = 400  # 25ms at 16kHz
    hop_size = 160     # 10ms at 16kHz
    n_fft = 512

    # Apply windowing and compute STFT
    num_frames = max(1, (len(samples) - window_size) // hop_size + 1)
    window = np.hanning(window_size).astype(np.float32)

    spectrogram = np.zeros((num_frames, n_fft // 2 + 1), dtype=np.float32)
    for i in range(num_frames):
        start = i * hop_size
        frame = samples[start:start + window_size]
        if len(frame) < window_size:
            frame = np.pad(frame, (0, window_size - len(frame)))
        windowed = frame * window
        spectrum = np.fft.rfft(windowed, n=n_fft)
        spectrogram[i] = np.abs(spectrum)

    # Log magnitude
    spectrogram = np.log(spectrogram + 1e-6)

    # Reshape to model input: [1, 1, num_frames, n_fft//2+1]
    input_tensor = spectrogram[np.newaxis, np.newaxis, :, :]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on
    # the raw buffer assuming contiguous layout. A non-contiguous view causes
    # a VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(input_tensor, dtype=np.float32)

def _softmax(logits):
    x = logits - np.max(logits, axis=-1, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=-1, keepdims=True)

def _postprocess(output, labels=None):
    logits = output.flatten()
    probs = _softmax(logits)
    top_indices = np.argsort(probs)[::-1][:TOP_K]
    predictions = []
    for idx in top_indices:
        entry = {{"class_idx": int(idx), "score": float(probs[idx])}}
        if labels and 0 <= idx < len(labels):
            entry["label"] = labels[idx]
        predictions.append(entry)
    return predictions

def _load_labels(pack_dir):
    search_dirs = [pack_dir, pack_dir / "assets"]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for name in ("audio_labels.txt", "audioset_labels.txt", "labels.txt"):
            p = d / name
            if p.is_file():
                try:
                    lines = p.read_text(encoding="utf-8").strip().splitlines()
                    labels = []
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        parts = line.split(":", 1)
                        if len(parts) == 2 and parts[0].strip().isdigit():
                            labels.append(parts[1].strip())
                        else:
                            labels.append(line)
                    if len(labels) >= 2:
                        return labels
                except (OSError, ValueError):
                    continue
    return None

def main():
    req = read_request()
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_audio(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    timer = StageTimer(device="htp")
    status("preparing")
    labels = _load_labels(pack_dir)
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    samples, sample_rate = _load_audio_wav(input_path)
    with timer.stage("preprocess"):
        input_tensor = _preprocess_audio(samples, sample_rate)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        predictions = _postprocess(outputs[0], labels)
    emit({{"type": "metrics", **timer.summary()}})
    result({{"predictions": predictions, "top_k": TOP_K, "num_classes": len(outputs[0].flatten()), "has_labels": labels is not None}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_audio_enhance(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working audio enhancement/denoising runner."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Audio Enhancement"""
from __future__ import annotations
import sys, traceback, json, wave
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_audio(req, repo_root):
    inputs = req.get("inputs") or {{}}
    raw = inputs.get("audio")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.audio is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input audio not found: {{raw}}")
    ext = p.suffix.lower()
    if ext not in (".wav", ".mp3", ".flac"):
        raise _UserError("INVALID_INPUT", f"unsupported audio format: {{ext}} (supported: .wav, .mp3, .flac)")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _load_audio_wav(audio_path):
    """Load a WAV file as float32 samples in [-1, 1]."""
    with wave.open(str(audio_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)
    if sample_width == 2:
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise _UserError("INVALID_INPUT", f"unsupported sample width: {{sample_width}} bytes")
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sample_rate

def _preprocess_audio(samples, sample_rate):
    """Normalize waveform and reshape to model input [1, 1, num_samples]."""
    target_sr = 16000
    if sample_rate != target_sr:
        duration = len(samples) / sample_rate
        target_len = int(duration * target_sr)
        indices = np.linspace(0, len(samples) - 1, target_len)
        samples = np.interp(indices, np.arange(len(samples)), samples)

    # Normalize waveform
    max_val = np.abs(samples).max()
    if max_val > 0:
        samples = samples / max_val

    # Reshape to [1, 1, num_samples]
    input_tensor = samples[np.newaxis, np.newaxis, :]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on
    # the raw buffer assuming contiguous layout. A non-contiguous view causes
    # a VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(input_tensor, dtype=np.float32)

def _save_wav(samples, sample_rate, output_path):
    """Save float32 samples as 16-bit PCM WAV."""
    samples = np.clip(samples, -1.0, 1.0)
    int_samples = (samples * 32767).astype(np.int16)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int_samples.tobytes())

def main():
    req = read_request()
    repo_root = _resolve_repo_root(req)
    input_path = _resolve_input_audio(req, repo_root)
    weights_path = _resolve_weights(req, repo_root)
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    samples, sample_rate = _load_audio_wav(input_path)
    with timer.stage("preprocess"):
        input_tensor = _preprocess_audio(samples, sample_rate)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        enhanced = outputs[0].flatten()
        # Clip to valid range
        enhanced = np.clip(enhanced, -1.0, 1.0)
        duration_s = float(len(enhanced)) / 16000.0
        output_dir = pack_dir / "output"
        output_path = output_dir / f"{model_name}_enhanced.wav"
        _save_wav(enhanced, 16000, output_path)
    emit({{"type": "metrics", **timer.summary(), "duration_s": duration_s}})
    result({{"audio_path": str(output_path), "duration_s": duration_s, "sample_rate": 16000}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_speaker_verify(pack_id, model_name, weights_filename, input_h, input_w, timestamp):
    """Generate a working speaker verification runner (two audio inputs → cosine similarity)."""
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — Speaker Verification"""
from __future__ import annotations
import sys, traceback, json, wave
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}
SIMILARITY_THRESHOLD = 0.7

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_input_audio_field(req, repo_root, field_name):
    """Resolve an audio input field (audio1 or audio2)."""
    inputs = req.get("inputs") or {{}}
    raw = inputs.get(field_name)
    if not raw:
        raise _UserError("INVALID_INPUT", f"inputs.{{field_name}} is required")
    p = Path(raw)
    if not p.is_absolute():
        candidate = (repo_root / p).resolve()
        if candidate.is_file():
            return candidate
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input audio not found: {{raw}}")
    ext = p.suffix.lower()
    if ext not in (".wav", ".mp3", ".flac"):
        raise _UserError("INVALID_INPUT", f"unsupported audio format: {{ext}} (supported: .wav, .mp3, .flac)")
    return p

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _load_audio_wav(audio_path):
    """Load a WAV file as float32 samples in [-1, 1]."""
    with wave.open(str(audio_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)
    if sample_width == 2:
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise _UserError("INVALID_INPUT", f"unsupported sample width: {{sample_width}} bytes")
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sample_rate

def _preprocess_audio(samples, sample_rate):
    """Compute magnitude spectrogram for speaker embedding extraction.

    NOTE: This is a simplified preprocessing pipeline. For real deployment,
    match the model's training preprocessing (e.g., proper mel filterbank,
    specific window/hop sizes, normalization).
    """
    target_sr = 16000
    if sample_rate != target_sr:
        duration = len(samples) / sample_rate
        target_len = int(duration * target_sr)
        indices = np.linspace(0, len(samples) - 1, target_len)
        samples = np.interp(indices, np.arange(len(samples)), samples)

    window_size = 400  # 25ms at 16kHz
    hop_size = 160     # 10ms at 16kHz
    n_fft = 512

    num_frames = max(1, (len(samples) - window_size) // hop_size + 1)
    window = np.hanning(window_size).astype(np.float32)

    spectrogram = np.zeros((num_frames, n_fft // 2 + 1), dtype=np.float32)
    for i in range(num_frames):
        start = i * hop_size
        frame = samples[start:start + window_size]
        if len(frame) < window_size:
            frame = np.pad(frame, (0, window_size - len(frame)))
        windowed = frame * window
        spectrum = np.fft.rfft(windowed, n=n_fft)
        spectrogram[i] = np.abs(spectrum)

    spectrogram = np.log(spectrogram + 1e-6)
    input_tensor = spectrogram[np.newaxis, np.newaxis, :, :]
    # MUST be C-contiguous; qai_appbuilder native Inference() does memcpy on
    # the raw buffer assuming contiguous layout. A non-contiguous view causes
    # a VCRUNTIME140 access violation (segfault) at run time.
    return np.ascontiguousarray(input_tensor, dtype=np.float32)

def _cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    a = a.flatten()
    b = b.flatten()
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a < 1e-8 or norm_b < 1e-8:
        return 0.0
    return float(dot / (norm_a * norm_b))

def main():
    req = read_request()
    repo_root = _resolve_repo_root(req)
    input_path_1 = _resolve_input_audio_field(req, repo_root, "audio1")
    input_path_2 = _resolve_input_audio_field(req, repo_root, "audio2")
    weights_path = _resolve_weights(req, repo_root)
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    samples_1, sr_1 = _load_audio_wav(input_path_1)
    samples_2, sr_2 = _load_audio_wav(input_path_2)
    with timer.stage("preprocess"):
        input_tensor_1 = _preprocess_audio(samples_1, sr_1)
        input_tensor_2 = _preprocess_audio(samples_2, sr_2)
    with timer.stage("infer_1"):
        try:
            outputs_1 = ctx.run([input_tensor_1])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed on audio1: {{e}}")
    with timer.stage("infer_2"):
        try:
            outputs_2 = ctx.run([input_tensor_2])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed on audio2: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        embedding_1 = outputs_1[0].flatten()
        embedding_2 = outputs_2[0].flatten()
        similarity = _cosine_similarity(embedding_1, embedding_2)
        same_speaker = similarity >= SIMILARITY_THRESHOLD
        embedding_dim = len(embedding_1)
    emit({{"type": "metrics", **timer.summary(), "similarity": similarity}})
    result({{"similarity": similarity, "same_speaker": same_speaker, "threshold": SIMILARITY_THRESHOLD, "embedding_dim": embedding_dim}})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''


def runner_generic(pack_id, model_name, weights_filename, input_kind, output_kind, category, input_h, input_w, timestamp, infer_manifest=None):
    """Generate a runnable generic runner that executes inference and returns raw results.

    Unlike the category-specific templates, this does basic preprocessing only:
    - Images: resize + normalize to [0,1] + NCHW
    - Audio: read audio file (requires audio_io from shared/)
    - Text: pass through (model-specific tokenization NOT implemented)

    The result includes raw tensor statistics and saves raw output for manual inspection.
    """
    return f'''#!/usr/bin/env python
# Generated by qai_pack_exporter on {timestamp}
"""{pack_id} · App Builder Pack runner — {category} (generic)

This runner performs basic inference and returns raw model outputs.
Category: {category}, Input: {input_kind}, Output: {output_kind}

For specialized postprocessing (e.g., CTC decode for OCR, attention decode for ASR),
you may need to customize this runner. See existing runners for reference:
  - OCR: features/app-builder/models/ppocrv4/runner.py
  - ASR: features/app-builder/models/whisper-base/runner.py
  - TTS: features/app-builder/models/melotts-zh/runner.py
"""
from __future__ import annotations
import sys, traceback, json
from pathlib import Path
from typing import Any
import numpy as np
from runner_protocol import emit, read_request, status, result, done, fail
from telemetry import measure, StageTimer

WEIGHTS_FILENAME = "{weights_filename}"
INPUT_H, INPUT_W = {input_h}, {input_w}
INPUT_KIND = "{input_kind}"
OUTPUT_KIND = "{output_kind}"
CATEGORY = "{category}"

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

def _resolve_repo_root(req):
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]

def _resolve_weights(req, repo_root):
    # Multi-variant aware weight resolver. Priority order:
    #   1. req["variantInstallPath"]  -- absolute path injected by backend.
    #   2. manifest.variants[i] whose id == req["variantId"].
    #   3. manifest.assets.installPath  -- legacy single-variant fallback.
    #   4. repo_root/models/<pack_id>/<WEIGHTS_FILENAME>  -- legacy hard-coded.
    #   5. pack_dir/weights/<WEIGHTS_FILENAME>  -- staging fallback.
    pack_dir = Path(req.get("packDir") or str(Path(__file__).resolve().parent))
    if not pack_dir.is_absolute():
        pack_dir = (repo_root / pack_dir).resolve()
    abs_inject = req.get("variantInstallPath")
    if abs_inject:
        p = Path(abs_inject)
        if p.is_file():
            return p
    candidates = []
    manifest = None
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = None
    vid = req.get("variantId")
    if manifest and vid:
        for v in (manifest.get("variants") or []):
            if v.get("id") == vid:
                ip = (v.get("assets") or {{}}).get("installPath")
                if ip:
                    candidates.append(Path(ip) if Path(ip).is_absolute()
                                      else (repo_root / ip).resolve())
                break
    if manifest:
        install_rel = (manifest.get("assets") or {{}}).get("installPath")
        if install_rel:
            candidates.append(Path(install_rel) if Path(install_rel).is_absolute()
                              else (repo_root / install_rel).resolve())
    candidates.append((repo_root / "models" / "{pack_id}" / WEIGHTS_FILENAME).resolve())
    candidates.append((pack_dir / "weights" / WEIGHTS_FILENAME).resolve())
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError("WEIGHTS_NOT_INSTALLED", f"{{WEIGHTS_FILENAME}} not found (variantId={{req.get('variantId')!r}})")

def _read_and_preprocess(req, repo_root):
    """Read input based on INPUT_KIND and prepare model-ready tensor."""
    inputs = req.get("inputs") or {{}}

    if INPUT_KIND == "image":
        raw = inputs.get("image")
        if not raw:
            raise _UserError("INVALID_INPUT", "inputs.image is required")
        from image_io import read_image
        p = Path(raw)
        if not p.is_absolute():
            candidate = (repo_root / p).resolve()
            if candidate.is_file():
                p = candidate
            else:
                p = (Path.cwd() / p).resolve()
        if not p.is_file():
            raise _UserError("INVALID_INPUT", f"input file not found: {{raw}}")
        image = read_image(p)
        # Basic resize + normalize + NCHW
        from PIL import Image
        pil_img = Image.fromarray(image).resize((INPUT_W, INPUT_H), Image.BILINEAR)
        img = np.array(pil_img, dtype=np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
        # MUST be C-contiguous (see classify runner for the VCRUNTIME140 segfault rationale).
        return np.ascontiguousarray(img, dtype=np.float32), {{"type": "image", "original_shape": list(image.shape)}}

    elif INPUT_KIND == "audio":
        raw = inputs.get("audio")
        if not raw:
            raise _UserError("INVALID_INPUT", "inputs.audio is required")
        p = Path(raw)
        if not p.is_absolute():
            candidate = (repo_root / p).resolve()
            if candidate.is_file():
                p = candidate
            else:
                p = (Path.cwd() / p).resolve()
        if not p.is_file():
            raise _UserError("INVALID_INPUT", f"input file not found: {{raw}}")
        try:
            from audio_io import read_audio
            samples, sr = read_audio(p, target_sample_rate=16000, mono=True)
            # MUST be C-contiguous (see classify runner rationale).
            return np.ascontiguousarray(samples[np.newaxis, ...], dtype=np.float32), {{"type": "audio", "sample_rate": sr, "duration_s": len(samples)/sr}}
        except ImportError:
            # Fallback: try to read raw
            import wave
            with wave.open(str(p), 'rb') as wf:
                sr = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
                samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            # MUST be C-contiguous (see classify runner rationale).
            return np.ascontiguousarray(samples[np.newaxis, ...], dtype=np.float32), {{"type": "audio", "sample_rate": sr, "duration_s": len(samples)/sr}}

    elif INPUT_KIND == "text":
        raw = inputs.get("text") or ""
        if not raw:
            raise _UserError("INVALID_INPUT", "inputs.text is required")
        # Basic: convert text to a placeholder tensor (model-specific tokenization needed)
        raise _UserError("INFER_ERROR",
            "Text input requires model-specific tokenization. "
            "Please implement _preprocess() in this runner for your tokenizer.")

    else:
        raise _UserError("INVALID_INPUT", f"unsupported input_kind: {{INPUT_KIND}}")

def main():
    req = read_request()
    run_id = req.get("runId") or "run"
    repo_root = _resolve_repo_root(req)
    weights_path = _resolve_weights(req, repo_root)
    timer = StageTimer(device="htp")
    status("preparing")
    try:
        from qnn_helper import QnnContext
    except Exception as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", f"qnn_helper import failed: {{e}}")
    with timer.stage("load_model", model=WEIGHTS_FILENAME):
        try:
            ctx = QnnContext.load(weights_path, runtime="Htp", log_level=1)
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"failed to load model: {{e}}")
    status("running")
    with timer.stage("preprocess"):
        input_tensor, input_meta = _read_and_preprocess(req, repo_root)
    with timer.stage("infer"):
        try:
            outputs = ctx.run([input_tensor])
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", str(e))
        except Exception as e:
            raise _UserError("INFER_ERROR", f"inference failed: {{e}}")
    with timer.stage("release"):
        try: ctx.close()
        except Exception: pass
    with timer.stage("postprocess"):
        # Build generic result from raw output tensors
        output_info = []
        for i, out in enumerate(outputs):
            arr = np.array(out)
            output_info.append({{
                "index": i,
                "shape": list(arr.shape),
                "dtype": str(arr.dtype),
                "min": float(arr.min()),
                "max": float(arr.max()),
                "mean": float(arr.mean()),
            }})
        # Save raw output for inspection
        out_dir = repo_root / "data" / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / f"raw-{{run_id}}.json"
        raw_data = {{"outputs": output_info, "input_meta": input_meta}}
        raw_path.write_text(json.dumps(raw_data, indent=2), encoding="utf-8")
    emit({{"type": "metrics", **timer.summary()}})
    result({{
        "raw_output_path": str(raw_path.relative_to(repo_root)),
        "outputs": output_info,
        "input_meta": input_meta,
        "note": "This is a generic runner. Output contains raw tensor statistics. Implement model-specific postprocessing for meaningful results."
    }})
    done()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(code="INFER_ERROR", message=str(e), traceback=traceback.format_exc(limit=20))
        sys.exit(1)
'''