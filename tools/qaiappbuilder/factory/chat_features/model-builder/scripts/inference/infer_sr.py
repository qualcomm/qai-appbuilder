# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Super-Resolution QNN Inference Script (model-builder)

Specialized for image super-resolution models (e.g., Real-ESRGAN x4plus).
Input: image file → Output: upscaled image file.

Verified on: Windows on Snapdragon (WoS) ARM64, Snapdragon X Elite (V73)
QAIRT SDK: <QAIRT SDK version> (see data/config/qairt_env.json "_version")

Usage:
  # Single image
  python inference/infer_sr.py --model real_esrgan_x4plus.bin --input image.jpg

  # With explicit scale factor and output path
  python inference/infer_sr.py --model real_esrgan_x4plus.bin --input image.jpg \\
    --output result_4x.jpg --scale 4

  # Batch processing
  python inference/infer_sr.py --model real_esrgan_x4plus.bin \\
    --input_dir ./images --output_dir ./results --scale 4

  # Custom input size (model was compiled for 512x512)
  python inference/infer_sr.py --model model.bin --input image.jpg --input_size 512

Args:
  --model:       Path to QNN context binary (.bin)
  --input:       Path to input image (PNG/JPG/BMP etc.)
  --input_dir:   Directory of input images (batch mode)
  --output:      Path to save output image (default: <input>_sr.<ext>)
  --output_dir:  Directory to save output images (batch mode)
  --input_size:  Input image size (default: auto-detect from model)
  --scale:       Upscale factor label (default: 4, for display only)
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


# ─── Image helpers ────────────────────────────────────────────────────────────

def load_image_nhwc(path: str, target_size: int = None) -> np.ndarray:
    """Load image as float32 NHWC tensor in [0, 1]."""
    img = Image.open(path).convert("RGB")
    if target_size:
        img = img.resize((target_size, target_size), Image.LANCZOS)
    arr = np.array(img, dtype=np.float32) / 255.0
    return arr[np.newaxis, ...]  # (1, H, W, 3) NHWC


def save_image_nhwc(arr: np.ndarray, path: str):
    """Save float32 NCHW or NHWC tensor to image file."""
    out = arr[0] if arr.ndim == 4 else arr  # (C, H, W) or (H, W, C)
    # Detect NCHW: shape (3, H, W) where first dim is 3 (channels)
    if out.ndim == 3 and out.shape[0] == 3:
        out = np.transpose(out, (1, 2, 0))  # (C, H, W) → (H, W, C)
    out = np.clip(out * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(out).save(path, quality=95)


class SRModel(QNNContext):
    def Inference(self, input_data):
        return super().Inference(input_data)


# ─── Core inference ───────────────────────────────────────────────────────────

def run_sr(model_path, input_path, output_path=None, input_size=None,
           scale=4, runtime="Htp", log_level=1):
    """Run super-resolution inference on a single image."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = SRModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    output_shapes = model.getOutputShapes()
    print(f"[INFO] Input  shapes: {input_shapes}")
    print(f"[INFO] Output shapes: {output_shapes}")

    # Detect input format: NCHW (1,3,H,W) or NHWC (1,H,W,3)
    input_is_nchw = False
    if input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] == 3:
                # NCHW: (1, 3, H, W)
                input_is_nchw = True
                if input_size is None:
                    input_size = shape[2]
            else:
                # NHWC: (1, H, W, 3)
                if input_size is None:
                    input_size = shape[1]
    print(f"[INFO] Input format: {'NCHW' if input_is_nchw else 'NHWC'}")

    print(f"[INFO] Loading input: {input_path}")
    inp = load_image_nhwc(input_path, target_size=input_size)  # always (1, H, W, 3)
    h_in, w_in = inp.shape[1], inp.shape[2]

    if input_is_nchw:
        inp = np.transpose(inp, (0, 3, 1, 2))  # (1,H,W,3) -> (1,3,H,W)
        print(f"[INFO] Input tensor (NCHW): {inp.shape}  range=[{inp.min():.3f}, {inp.max():.3f}]")
    else:
        print(f"[INFO] Input tensor (NHWC): {inp.shape}  range=[{inp.min():.3f}, {inp.max():.3f}]")

    print("[INFO] Running inference...")
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        outputs = model.Inference([inp])
    finally:
        PerfProfile.RelPerfProfileGlobal()
    out = np.array(outputs[0])

    if output_shapes:
        try:
            out = out.reshape(output_shapes[0])
        except Exception:
            pass

    # Detect NCHW (1,C,H,W) vs NHWC (1,H,W,C): if shape[1]==3 it's NCHW
    if out.ndim == 4 and out.shape[1] == 3:
        h_out, w_out = out.shape[2], out.shape[3]  # NCHW
    elif out.ndim == 4:
        h_out, w_out = out.shape[1], out.shape[2]  # NHWC
    else:
        h_out, w_out = out.shape[0], out.shape[1]
    print(f"[INFO] Output tensor: {out.shape}  range=[{out.min():.4f}, {out.max():.4f}]")

    if output_path is None:
        p = Path(input_path)
        output_path = str(p.parent / f"{p.stem}_sr{p.suffix}")

    save_image_nhwc(out, output_path)
    print(f"[DONE] Saved: {output_path}  ({w_in}x{h_in} -> {w_out}x{h_out}, {scale}x SR)")
    return out


def run_batch(model_path, input_dir, output_dir, input_size=None,
              scale=4, runtime="Htp", log_level=1):
    """Batch super-resolution inference."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem
    os.makedirs(output_dir, exist_ok=True)

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = SRModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    if input_size is None and input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            input_size = shape[1]

    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    images = sorted([f for f in Path(input_dir).iterdir() if f.suffix.lower() in exts])
    print(f"[INFO] Found {len(images)} images in {input_dir}")

    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        for i, img_path in enumerate(images):
            out_path = str(Path(output_dir) / f"{img_path.stem}_sr{img_path.suffix}")
            print(f"[{i+1}/{len(images)}] {img_path.name} -> {out_path}")
            try:
                inp = load_image_nhwc(str(img_path), target_size=input_size)
                outputs = model.Inference([inp])
                out = np.array(outputs[0])
                try:
                    out = out.reshape(model.getOutputShapes()[0])
                except Exception:
                    pass
                save_image_nhwc(out, out_path)
            except Exception as e:
                print(f"  [ERROR] {e}")
    finally:
        PerfProfile.RelPerfProfileGlobal()

    print(f"\n[DONE] Batch complete. Results in: {output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Super-Resolution QNN Inference (model-builder)")
    parser.add_argument("--model", required=True, help="Path to QNN context binary (.bin)")
    parser.add_argument("--input", default=None, help="Input image path")
    parser.add_argument("--input_dir", default=None, help="Input directory (batch mode)")
    parser.add_argument("--output", default=None, help="Output image path")
    parser.add_argument("--output_dir", default=None, help="Output directory (batch mode)")
    parser.add_argument("--input_size", type=int, default=None,
                        help="Resize input to this size (default: auto from model)")
    parser.add_argument("--scale", type=int, default=4, help="Upscale factor label (default: 4)")
    parser.add_argument("--runtime", default="Htp", choices=["Htp", "Cpu"])
    parser.add_argument("--log_level", type=int, default=1)
    args = parser.parse_args()

    if not HAS_PIL:
        print("[ERROR] Pillow required. Install: pip install Pillow")
        sys.exit(1)

    if args.input_dir:
        out_dir = args.output_dir or (args.input_dir + "_sr")
        run_batch(args.model, args.input_dir, out_dir,
                  args.input_size, args.scale, args.runtime, args.log_level)
    elif args.input:
        run_sr(args.model, args.input, args.output,
               args.input_size, args.scale, args.runtime, args.log_level)
    else:
        parser.error("Provide --input (single image) or --input_dir (batch mode)")


if __name__ == "__main__":
    main()
