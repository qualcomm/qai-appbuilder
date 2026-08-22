# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Image Classification QNN Inference Script (model-builder)

Specialized for image classification models (e.g., GoogLeNet, ResNet, MobileNet).
Input: image file → Output: Top-K class predictions with confidence scores.

Verified on: Windows on Snapdragon (WoS) ARM64, Snapdragon X Elite (V73)
QAIRT SDK: <QAIRT SDK version> (see data/config/qairt_env.json "_version")

Usage:
  # Basic classification (random labels)
  python inference/infer_classify.py --model googlenet.bin --input dog.jpg

  # With ImageNet labels file
  python inference/infer_classify.py --model googlenet.bin --input dog.jpg \\
    --labels imagenet_labels.json --topk 5

  # With custom input size
  python inference/infer_classify.py --model model.bin --input image.jpg \\
    --input_size 224 --normalize

  # Batch classification
  python inference/infer_classify.py --model model.bin \\
    --input_dir ./images --labels labels.json

Args:
  --model:       Path to QNN context binary (.bin)
  --input:       Path to input image
  --input_dir:   Directory of input images (batch mode)
  --labels:      Path to labels file (.json list or .txt one-per-line)
  --topk:        Number of top predictions to show (default: 5)
  --input_size:  Resize input to this size (default: auto from model)
  --normalize:   Apply ImageNet normalization (mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225])
  --runtime:     QNN runtime: Htp (default) or Cpu
  --log_level:   Log level 0-5 (default: 1)
"""

import argparse
import json
import os
import sys

# UTF-8 safe stdout/stderr (model-builder): Windows consoles default to a
# legacy code page (GBK/cp1252) and crash with UnicodeEncodeError when a
# print() contains non-ASCII (e.g. emoji ✅ / 中文). Reconfigure here so this
# script is "copy-and-run" safe regardless of the console code page; this also
# removes any need for the fragile `set PYTHONUTF8=1 &&` cmd workaround.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from pathlib import Path

import numpy as np

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, QNNConfig, PerfProfile


# ─── env_config.json auto-discovery ──────────────────────────────────────────

def _find_qairt_env_config() -> dict:
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "data" / "config" / "qairt_env.json"
        if not candidate.exists():
            candidate = current / "config" / "qairt_env.json"
        if candidate.exists():
            try:
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


def _apply_env_config(cfg: dict) -> None:
    if not cfg:
        return
    sdk_root = cfg.get("qairt_sdk_root", "")
    if sdk_root and not os.environ.get("QAIRT_SDK_ROOT"):
        os.environ["QAIRT_SDK_ROOT"] = sdk_root


# ─── Helpers ──────────────────────────────────────────────────────────────────

# ImageNet normalization constants
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def load_labels(labels_path: str) -> list:
    """Load labels from JSON list or TXT file."""
    p = Path(labels_path)
    if p.suffix == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Handle {idx: label} or {label: idx} dicts
            return [data[str(i)] for i in range(len(data))]
    else:
        with open(p, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return []


def preprocess_image(path: str, input_size: int = 224, normalize: bool = False) -> np.ndarray:
    """Load and preprocess image as float32 NHWC tensor."""
    img = Image.open(path).convert("RGB")
    # Resize with center crop (standard ImageNet preprocessing)
    w, h = img.size
    short = min(w, h)
    scale = input_size / short
    new_w, new_h = int(w * scale), int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # Center crop
    left = (new_w - input_size) // 2
    top  = (new_h - input_size) // 2
    img = img.crop((left, top, left + input_size, top + input_size))

    arr = np.array(img, dtype=np.float32) / 255.0  # (H, W, 3)
    if normalize:
        arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    return arr[np.newaxis, ...]  # (1, H, W, 3) NHWC


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()


class ClassifyModel(QNNContext):
    def Inference(self, input_data):
        return super().Inference(input_data)


# ─── Core inference ───────────────────────────────────────────────────────────

def classify_image(model, input_path: str, labels: list, topk: int,
                   input_size: int, normalize: bool) -> list:
    """Classify a single image. Returns list of (label, score) tuples."""
    inp = preprocess_image(input_path, input_size=input_size, normalize=normalize)
    outputs = model.Inference([inp])
    logits = np.array(outputs[0]).flatten()
    probs = softmax(logits)
    top_indices = np.argsort(probs)[::-1][:topk]
    results = []
    for idx in top_indices:
        label = labels[idx] if labels and idx < len(labels) else f"class_{idx}"
        results.append((label, float(probs[idx]), int(idx)))
    return results


def run_classify(model_path, input_path, labels_path=None, topk=5,
                 input_size=None, normalize=False, runtime="Htp", log_level=1):
    """Run classification on a single image."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = ClassifyModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    print(f"[INFO] Input shapes: {input_shapes}")

    # Detect input format: NCHW (1,C,H,W) or NHWC (1,H,W,C)
    input_is_nchw = False
    if input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] in (1, 3, 4) and shape[1] < shape[2]:
                # NCHW: (1, C, H, W) where C is small (1/3/4)
                input_is_nchw = True
                if input_size is None:
                    input_size = shape[2]
            else:
                # NHWC: (1, H, W, C)
                if input_size is None:
                    input_size = shape[1]
    if input_size is None:
        input_size = 224
    print(f"[INFO] Input format: {'NCHW' if input_is_nchw else 'NHWC'}")

    labels = load_labels(labels_path) if labels_path else []
    if labels:
        print(f"[INFO] Loaded {len(labels)} labels from {labels_path}")

    print(f"[INFO] Classifying: {input_path}")
    inp = preprocess_image(input_path, input_size=input_size, normalize=normalize)
    if input_is_nchw:
        inp = np.transpose(inp, (0, 3, 1, 2))  # (1,H,W,C) -> (1,C,H,W)
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        outputs = model.Inference([inp])
    finally:
        PerfProfile.RelPerfProfileGlobal()
    logits = np.array(outputs[0]).flatten()
    probs = softmax(logits)
    top_indices = np.argsort(probs)[::-1][:topk]
    results = []
    for idx in top_indices:
        label = labels[idx] if labels and idx < len(labels) else f"class_{idx}"
        results.append((label, float(probs[idx]), int(idx)))

    print(f"\n[Results] Top-{topk} predictions:")
    for rank, (label, score, idx) in enumerate(results, 1):
        print(f"  {rank}. [{idx:4d}] {label:<40s}  {score*100:6.2f}%")

    print(f"\n[DONE] Top prediction: {results[0][0]} ({results[0][1]*100:.2f}%)")
    return results


def run_batch_classify(model_path, input_dir, labels_path=None, topk=3,
                       input_size=None, normalize=False, runtime="Htp", log_level=1):
    """Batch classification on a directory of images."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = ClassifyModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    input_is_nchw = False
    if input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] in (1, 3, 4) and shape[1] < shape[2]:
                input_is_nchw = True
                if input_size is None:
                    input_size = shape[2]
            else:
                if input_size is None:
                    input_size = shape[1]
    if input_size is None:
        input_size = 224

    labels = load_labels(labels_path) if labels_path else []

    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    images = sorted([f for f in Path(input_dir).iterdir() if f.suffix.lower() in exts])
    print(f"[INFO] Found {len(images)} images in {input_dir}\n")

    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        for i, img_path in enumerate(images):
            print(f"[{i+1}/{len(images)}] {img_path.name}")
            try:
                inp = preprocess_image(str(img_path), input_size=input_size, normalize=normalize)
                if input_is_nchw:
                    inp = np.transpose(inp, (0, 3, 1, 2))
                outputs = model.Inference([inp])
                logits = np.array(outputs[0]).flatten()
                probs = softmax(logits)
                top_indices = np.argsort(probs)[::-1][:topk]
                for rank, idx in enumerate(top_indices, 1):
                    label = labels[idx] if labels and idx < len(labels) else f"class_{idx}"
                    print(f"  {rank}. {label} ({probs[idx]*100:.2f}%)")
            except Exception as e:
                print(f"  [ERROR] {e}")
            print()
    finally:
        PerfProfile.RelPerfProfileGlobal()

    print(f"[DONE] Batch classification complete.")


def main():
    parser = argparse.ArgumentParser(
        description="Image Classification QNN Inference (model-builder)")
    parser.add_argument("--model", required=True, help="Path to QNN context binary (.bin)")
    parser.add_argument("--input", default=None, help="Input image path")
    parser.add_argument("--input_dir", default=None, help="Input directory (batch mode)")
    parser.add_argument("--labels", default=None,
                        help="Labels file (.json list or .txt one-per-line)")
    parser.add_argument("--topk", type=int, default=5, help="Top-K predictions (default: 5)")
    parser.add_argument("--input_size", type=int, default=None,
                        help="Input image size (default: auto from model)")
    parser.add_argument("--normalize", action="store_true",
                        help="Apply ImageNet normalization (mean/std)")
    parser.add_argument("--runtime", default="Htp", choices=["Htp", "Cpu"])
    parser.add_argument("--log_level", type=int, default=1)
    args = parser.parse_args()

    if not HAS_PIL:
        print("[ERROR] Pillow required. Install: pip install Pillow")
        sys.exit(1)

    if args.input_dir:
        run_batch_classify(args.model, args.input_dir, args.labels, args.topk,
                           args.input_size, args.normalize, args.runtime, args.log_level)
    elif args.input:
        run_classify(args.model, args.input, args.labels, args.topk,
                     args.input_size, args.normalize, args.runtime, args.log_level)
    else:
        parser.error("Provide --input (single image) or --input_dir (batch mode)")


if __name__ == "__main__":
    main()
