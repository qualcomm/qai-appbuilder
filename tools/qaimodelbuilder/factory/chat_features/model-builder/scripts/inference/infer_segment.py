# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Image Segmentation QNN Inference Script (model-builder)

Specialized for semantic/instance segmentation models (e.g., UNet, DeepLab).
Input: image file → Output: segmentation mask overlay image.

Verified on: Windows on Snapdragon (WoS) ARM64, Snapdragon X Elite (V73)
QAIRT SDK: <QAIRT SDK version> (see data/config/qairt_env.json "_version")

Supported output formats:
  - Semantic: [1, num_classes, H, W] or [1, H, W, num_classes] → argmax mask
  - Binary:   [1, 1, H, W] or [1, H, W, 1] → threshold mask

Usage:
  # Basic segmentation
  python inference/infer_segment.py --model unet.bin --input image.jpg

  # With custom alpha blend and output
  python inference/infer_segment.py --model unet.bin --input image.jpg \\
    --output segmented.png --alpha 0.5

  # With class labels and color palette
  python inference/infer_segment.py --model model.bin --input image.jpg \\
    --labels labels.txt --alpha 0.6

  # Batch segmentation
  python inference/infer_segment.py --model model.bin \\
    --input_dir ./images --output_dir ./results

Args:
  --model:       Path to QNN context binary (.bin)
  --input:       Path to input image
  --input_dir:   Directory of input images (batch mode)
  --output:      Path to save segmentation overlay image
  --output_dir:  Directory to save output images (batch mode)
  --mask_output: Also save raw mask image (default: False)
  --labels:      Path to class labels file (.txt or .json)
  --input_size:  Input image size HxW, e.g. 640x1280 or 640 (default: auto)
  --alpha:       Blend alpha for overlay (0=original, 1=mask only, default: 0.5)
  --threshold:   Threshold for binary segmentation (default: 0.5)
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


# ─── Color palette (20 classes, Pascal VOC style) ────────────────────────────

PALETTE = np.array([
    [0,   0,   0  ], [128, 0,   0  ], [0,   128, 0  ], [128, 128, 0  ],
    [0,   0,   128], [128, 0,   128], [0,   128, 128], [128, 128, 128],
    [64,  0,   0  ], [192, 0,   0  ], [64,  128, 0  ], [192, 128, 0  ],
    [64,  0,   128], [192, 0,   128], [64,  128, 128], [192, 128, 128],
    [0,   64,  0  ], [128, 64,  0  ], [0,   192, 0  ], [128, 192, 0  ],
], dtype=np.uint8)


def load_labels(labels_path: str) -> list:
    p = Path(labels_path)
    if p.suffix == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else list(data.values())
    else:
        with open(p, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]


def parse_input_size(size_str):
    """Parse '640' or '640x1280' into (H, W)."""
    if isinstance(size_str, int):
        return size_str, size_str
    if "x" in str(size_str):
        h, w = str(size_str).split("x")
        return int(h), int(w)
    s = int(size_str)
    return s, s


def preprocess_image(path: str, input_h: int, input_w: int):
    """Load and resize image, return NHWC float32 tensor and original size."""
    img = Image.open(path).convert("RGB")
    orig_w, orig_h = img.size
    img_resized = img.resize((input_w, input_h), Image.LANCZOS)
    arr = np.array(img_resized, dtype=np.float32) / 255.0
    return arr[np.newaxis, ...], orig_w, orig_h  # (1, H, W, 3) NHWC


def parse_segmentation_output(output, orig_h, orig_w, threshold=0.5):
    """
    Parse segmentation output to class mask.
    Handles: [1,C,H,W], [1,H,W,C], [1,1,H,W], [1,H,W,1]
    Returns: (H, W) uint8 class index mask
    """
    out = np.array(output)
    # Remove batch dim
    if out.ndim == 4:
        out = out[0]  # (C,H,W) or (H,W,C)

    if out.ndim == 3:
        # Determine if channels-first or channels-last
        if out.shape[0] < out.shape[1] and out.shape[0] < out.shape[2]:
            # (C, H, W) → channels first
            if out.shape[0] == 1:
                # Binary segmentation
                mask = (out[0] > threshold).astype(np.uint8)
            else:
                # Multi-class: argmax over channels
                mask = out.argmax(axis=0).astype(np.uint8)
        else:
            # (H, W, C) → channels last
            if out.shape[2] == 1:
                mask = (out[:, :, 0] > threshold).astype(np.uint8)
            else:
                mask = out.argmax(axis=2).astype(np.uint8)
    elif out.ndim == 2:
        mask = out.astype(np.uint8)
    else:
        return None

    # Resize mask to original image size
    mask_img = Image.fromarray(mask, mode="L")
    mask_img = mask_img.resize((orig_w, orig_h), Image.NEAREST)
    return np.array(mask_img)


def colorize_mask(mask: np.ndarray) -> np.ndarray:
    """Convert class index mask to RGB color image."""
    h, w = mask.shape
    color_mask = np.zeros((h, w, 3), dtype=np.uint8)
    num_colors = len(PALETTE)
    for cls_id in np.unique(mask):
        color_mask[mask == cls_id] = PALETTE[cls_id % num_colors]
    return color_mask


def create_overlay(original_path: str, mask: np.ndarray, alpha: float = 0.5) -> Image.Image:
    """Blend original image with colorized mask."""
    orig = Image.open(original_path).convert("RGB")
    color_mask = Image.fromarray(colorize_mask(mask))
    # Resize mask to match original if needed
    if color_mask.size != orig.size:
        color_mask = color_mask.resize(orig.size, Image.NEAREST)
    overlay = Image.blend(orig, color_mask, alpha=alpha)
    return overlay


class SegmentModel(QNNContext):
    def Inference(self, input_data):
        return super().Inference(input_data)


# ─── Core inference ───────────────────────────────────────────────────────────

def run_segment(model_path, input_path, output_path=None, mask_output=False,
                labels_path=None, input_size=None, alpha=0.5, threshold=0.5,
                runtime="Htp", log_level=1):
    """Run segmentation on a single image."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = SegmentModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    output_shapes = model.getOutputShapes()
    print(f"[INFO] Input  shapes: {input_shapes}")
    print(f"[INFO] Output shapes: {output_shapes}")

    # Detect input format: NCHW (1,C,H,W) or NHWC (1,H,W,C)
    input_is_nchw = False
    if input_size is not None:
        input_h, input_w = parse_input_size(input_size)
    elif input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] in (1, 3, 4) and shape[1] < shape[2]:
                # NCHW: (1, C, H, W)
                input_is_nchw = True
                input_h, input_w = shape[2], shape[3]
            else:
                # NHWC: (1, H, W, C)
                input_h, input_w = shape[1], shape[2]
        else:
            input_h = input_w = 640
    else:
        input_h = input_w = 640
    print(f"[INFO] Input format: {'NCHW' if input_is_nchw else 'NHWC'}")

    labels = load_labels(labels_path) if labels_path else []

    print(f"[INFO] Processing: {input_path}  (input: {input_h}x{input_w})")
    inp, orig_w, orig_h = preprocess_image(input_path, input_h, input_w)
    if input_is_nchw:
        inp = np.transpose(inp, (0, 3, 1, 2))  # (1,H,W,C) -> (1,C,H,W)
    print(f"[INFO] Input tensor: {inp.shape}  original: {orig_w}x{orig_h}")

    print("[INFO] Running inference...")
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        outputs = model.Inference([inp])
    finally:
        PerfProfile.RelPerfProfileGlobal()
    raw_out = np.array(outputs[0])
    print(f"[INFO] Raw output shape: {raw_out.shape}")

    mask = parse_segmentation_output(raw_out, orig_h, orig_w, threshold)
    if mask is None:
        print("[ERROR] Could not parse segmentation output.")
        return None

    unique_classes = np.unique(mask)
    print(f"\n[Results] Detected {len(unique_classes)} classes: {unique_classes.tolist()}")
    if labels:
        for cls_id in unique_classes:
            label = labels[cls_id] if cls_id < len(labels) else f"class_{cls_id}"
            pct = (mask == cls_id).sum() / mask.size * 100
            print(f"  class {cls_id:3d} ({label:<20s}): {pct:.1f}% of image")

    # Save overlay
    if output_path is None:
        p = Path(input_path)
        output_path = str(p.parent / f"{p.stem}_seg{p.suffix}")

    overlay = create_overlay(input_path, mask, alpha=alpha)
    overlay.save(output_path)
    print(f"\n[DONE] Overlay saved: {output_path}")

    # Optionally save raw mask
    if mask_output:
        mask_path = output_path.replace("_seg.", "_mask.")
        Image.fromarray(colorize_mask(mask)).save(mask_path)
        print(f"[DONE] Mask saved: {mask_path}")

    return mask


def run_batch_segment(model_path, input_dir, output_dir, labels_path=None,
                      input_size=None, alpha=0.5, threshold=0.5,
                      runtime="Htp", log_level=1):
    """Batch segmentation."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem
    os.makedirs(output_dir, exist_ok=True)

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = SegmentModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    input_is_nchw = False
    if input_size is not None:
        input_h, input_w = parse_input_size(input_size)
    elif input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] in (1, 3, 4) and shape[1] < shape[2]:
                input_is_nchw = True
                input_h, input_w = shape[2], shape[3]
            else:
                input_h, input_w = shape[1], shape[2]
        else:
            input_h = input_w = 640
    else:
        input_h = input_w = 640

    labels = load_labels(labels_path) if labels_path else []

    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    images = sorted([f for f in Path(input_dir).iterdir() if f.suffix.lower() in exts])
    print(f"[INFO] Found {len(images)} images\n")

    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        for i, img_path in enumerate(images):
            out_path = str(Path(output_dir) / f"{img_path.stem}_seg{img_path.suffix}")
            print(f"[{i+1}/{len(images)}] {img_path.name}")
            try:
                inp, orig_w, orig_h = preprocess_image(str(img_path), input_h, input_w)
                if input_is_nchw:
                    inp = np.transpose(inp, (0, 3, 1, 2))
                outputs = model.Inference([inp])
                raw_out = np.array(outputs[0])
                mask = parse_segmentation_output(raw_out, orig_h, orig_w, threshold)
                if mask is not None:
                    overlay = create_overlay(str(img_path), mask, alpha=alpha)
                    overlay.save(out_path)
                    print(f"  -> {len(np.unique(mask))} classes, saved: {out_path}")
            except Exception as e:
                print(f"  [ERROR] {e}")
    finally:
        PerfProfile.RelPerfProfileGlobal()

    print(f"\n[DONE] Batch segmentation complete. Results in: {output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Image Segmentation QNN Inference (model-builder)")
    parser.add_argument("--model", required=True, help="Path to QNN context binary (.bin)")
    parser.add_argument("--input", default=None, help="Input image path")
    parser.add_argument("--input_dir", default=None, help="Input directory (batch mode)")
    parser.add_argument("--output", default=None, help="Output overlay image path")
    parser.add_argument("--output_dir", default=None, help="Output directory (batch mode)")
    parser.add_argument("--mask_output", action="store_true", help="Also save raw mask image")
    parser.add_argument("--labels", default=None, help="Labels file (.txt or .json)")
    parser.add_argument("--input_size", default=None,
                        help="Input size: '640' or '640x1280' (default: auto from model)")
    parser.add_argument("--alpha", type=float, default=0.5,
                        help="Blend alpha for overlay (default: 0.5)")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Threshold for binary segmentation (default: 0.5)")
    parser.add_argument("--runtime", default="Htp", choices=["Htp", "Cpu"])
    parser.add_argument("--log_level", type=int, default=1)
    args = parser.parse_args()

    if not HAS_PIL:
        print("[ERROR] Pillow required. Install: pip install Pillow")
        sys.exit(1)

    if args.input_dir:
        out_dir = args.output_dir or (args.input_dir + "_seg")
        run_batch_segment(args.model, args.input_dir, out_dir, args.labels,
                          args.input_size, args.alpha, args.threshold,
                          args.runtime, args.log_level)
    elif args.input:
        run_segment(args.model, args.input, args.output, args.mask_output,
                    args.labels, args.input_size, args.alpha, args.threshold,
                    args.runtime, args.log_level)
    else:
        parser.error("Provide --input (single image) or --input_dir (batch mode)")


if __name__ == "__main__":
    main()
