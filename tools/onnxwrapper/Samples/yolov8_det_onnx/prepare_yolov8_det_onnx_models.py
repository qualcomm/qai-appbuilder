# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""prepare_yolov8_det_onnx_models.py

Export YOLOv8 ONNX model using **Ultralytics official export**:

    yolo export model=yolov8n.pt format=onnx imgsz=640 opset=11

This script therefore:
1) Tries to run the `yolo` CLI (recommended).
2) If `yolo` is not found, falls back to Python API: `from ultralytics import YOLO; model.export(...)`.

Outputs
-------
Moves the generated `<model>.onnx` into `<cwd>/models-onnx/`.

Usage
-----
    python prepare_yolov8_det_onnx_models.py
    python prepare_yolov8_det_onnx_models.py --model yolov8s.pt --imgsz 640 --opset 11 --output-dir models-onnx

"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_MODEL = "yolov8n.pt"
DEFAULT_IMGSZ = 640
DEFAULT_OPSET = 11
DEFAULT_OUTPUT_DIR = "models-onnx"


def _run_export_with_yolo_cli(model: str, imgsz: int, opset: int) -> None:
    """Run Ultralytics export using the `yolo` CLI."""
    cmd = [
        "yolo",
        "export",
        f"model={model}",
        "format=onnx",
        f"imgsz={imgsz}",
        f"opset={opset}",
    ]
    print("Running (CLI):")
    print(" ", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _run_export_with_python_api(model: str, imgsz: int, opset: int) -> None:
    """Fallback: run export via Python API."""
    from ultralytics import YOLO

    print("Running (Python API):")
    print(f"  from ultralytics import YOLO; YOLO('{model}').export(format='onnx', imgsz={imgsz}, opset={opset})")

    m = YOLO(model)
    # Ultralytics Python API accepts imgsz and opset as kwargs for export.
    m.export(format="onnx", imgsz=imgsz, opset=opset)


def _expected_onnx_path(model: str) -> Path:
    return Path(model).with_suffix(".onnx")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export YOLOv8 ONNX using Ultralytics")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="YOLOv8 .pt model (default: yolov8n.pt)")
    parser.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ, help="Input image size (default: 640)")
    parser.add_argument("--opset", type=int, default=DEFAULT_OPSET, help="ONNX opset version (default: 11)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory to place exported ONNX (default: models-onnx)")

    args = parser.parse_args()

    # Try CLI first (recommended by Ultralytics docs)
    try:
        _run_export_with_yolo_cli(args.model, args.imgsz, args.opset)
    except FileNotFoundError:
        # `yolo` command not found
        print("[WARN] 'yolo' CLI not found in PATH. Falling back to Python API.")
        _run_export_with_python_api(args.model, args.imgsz, args.opset)

    onnx_path = _expected_onnx_path(args.model)
    if not onnx_path.exists():
        raise FileNotFoundError(f"Expected ONNX not found after export: {onnx_path}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dst = out_dir / onnx_path.name
    if dst.exists():
        dst.unlink()

    # Move ONNX into output dir
    onnx_path.replace(dst)

    # If Ultralytics created side artifacts (e.g., metadata), keep them in cwd; user can manage as needed.
    print("Export completed successfully")
    print(f" ONNX model: {dst.resolve()}")


if __name__ == "__main__":
    main()