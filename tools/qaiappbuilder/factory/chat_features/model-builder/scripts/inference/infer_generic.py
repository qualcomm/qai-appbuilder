# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Generic QNN Model Inference Script (model-builder)

Runs inference on any QNN context binary (.bin) using qai_appbuilder.
Based on qnn_model_verify_v3.py pattern.

Supports:
  - Random input (default, for quick model verification)
  - Raw binary input files (--raw_paths)
  - Multiple inputs (semicolon-separated)
  - Native or float I/O data types
  - Output saved as .raw binary files
  - Auto-detects input format: NCHW (1,3,H,W) or NHWC (1,H,W,3)

Usage:
  # Quick verification with random input
  python inference/infer_generic.py --model model.bin

  # With raw input files (multiple inputs separated by ;)
  python inference/infer_generic.py --model model.bin --raw_paths "input0.raw;input1.raw"

  # Native data type (for quantized models)
  python inference/infer_generic.py --model model.bin --io_data_type native

  # Specify raw input dtype explicitly
  python inference/infer_generic.py --model model.bin --raw_paths input.raw --raw_dtype float16

  # Keep tensor shapes (don't flatten to 1D)
  python inference/infer_generic.py --model model.bin --keep_shape

  # Save outputs to specific directory
  python inference/infer_generic.py --model model.bin --output_dir ./outputs

Args:
  --model:          Path to QNN context binary (.bin)
  --model_name:     Model name for QNNContext (default: derived from filename)
  --raw_paths:      Semicolon-separated paths to raw input files
  --io_data_type:   'float' (default) or 'native' (for quantized models)
  --raw_dtype:      Numpy dtype for raw inputs: float32/float16/int8/uint8/int32 etc.
  --keep_shape:     Keep tensors in model shape (default: flatten to 1D)
  --output_dir:     Directory to save output .raw files (default: same directory as model)
  --runtime:        QNN runtime: Htp (default) or Cpu
  --log_level:      Log level 0-5 (default: 1=WARN)
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

from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    QNNConfig,
    DataType,
    PerfProfile,
)


# ─── env_config.json auto-discovery ──────────────────────────────────────────

def _find_qairt_env_config() -> dict:
    """Auto-discover data/config/qairt_env.json by traversing up the directory tree."""
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
        print(f"[INFO] Set QAIRT_SDK_ROOT from env_config: {sdk_root}")
    vc_targets = cfg.get("vc_targets_path", "")
    if vc_targets and not os.environ.get("VCTargetsPath"):
        os.environ["VCTargetsPath"] = vc_targets
    if sdk_root:
        qairt_pylib = os.path.join(sdk_root, "lib", "python")
        pythonpath = os.environ.get("PYTHONPATH", "")
        if qairt_pylib not in pythonpath:
            os.environ["PYTHONPATH"] = qairt_pylib + os.pathsep + pythonpath if pythonpath else qairt_pylib


# ─── Format detection ─────────────────────────────────────────────────────────

def _detect_input_format(shape) -> str:
    """Detect input tensor format from shape.

    Returns 'NCHW', 'NHWC', or '' (unknown/non-image).
    Rules (4D tensors only):
      - (1, 3, H, W) where shape[1] == 3  -> NCHW
      - (1, H, W, 3) where shape[3] == 3  -> NHWC
      - other 4D shapes                   -> NCHW (assume, by convention)
    """
    if not shape or len(shape) != 4:
        return ""
    if shape[1] == 3:
        return "NCHW"
    if shape[3] == 3:
        return "NHWC"
    # Fallback: if second dim is small (<=4) treat as NCHW, else NHWC
    if shape[1] <= 4:
        return "NCHW"
    return "NHWC"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _numpy_dtype_from_str(s: str):
    s = (s or "").strip().lower()
    mapping = {
        "float32": np.float32, "float": np.float32, "fp32": np.float32,
        "float16": np.float16, "half": np.float16, "fp16": np.float16,
        "int8": np.int8, "uint8": np.uint8,
        "int16": np.int16, "uint16": np.uint16,
        "int32": np.int32, "uint32": np.uint32,
        "int64": np.int64, "uint64": np.uint64,
    }
    return mapping.get(s, None)


def _numpy_dtype_from_qnn_dtype(qnn_dt):
    """Map QAI AppBuilder DataType to numpy dtype."""
    if isinstance(qnn_dt, (list, tuple)) and len(qnn_dt) > 0:
        qnn_dt = qnn_dt[0]
    try:
        if qnn_dt == DataType.FLOAT:
            return np.float32
        if hasattr(DataType, "HALF") and qnn_dt == DataType.HALF:
            return np.float16
        if hasattr(DataType, "FLOAT16") and qnn_dt == DataType.FLOAT16:
            return np.float16
        if hasattr(DataType, "INT32") and qnn_dt == DataType.INT32:
            return np.int32
        if hasattr(DataType, "INT64") and qnn_dt == DataType.INT64:
            return np.int64
        if hasattr(DataType, "INT16") and qnn_dt == DataType.INT16:
            return np.int16
        if hasattr(DataType, "INT8") and qnn_dt == DataType.INT8:
            return np.int8
        if hasattr(DataType, "UINT8") and qnn_dt == DataType.UINT8:
            return np.uint8
    except Exception:
        pass
    s = str(qnn_dt).lower()
    if "float16" in s or "fp16" in s or "half" in s:
        return np.float16
    if "float32" in s or "fp32" in s or "float" in s:
        return np.float32
    if "int32" in s:
        return np.int32
    if "int64" in s:
        return np.int64
    if "int16" in s:
        return np.int16
    if "int8" in s:
        return np.int8
    if "uint8" in s:
        return np.uint8
    return None


class QNNModel(QNNContext):
    def Inference(self, input_data):
        return super().Inference(input_data)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_inference(
    model_path: str,
    model_name: str = None,
    raw_paths: str = None,
    io_data_type: str = "float",
    raw_dtype: str = None,
    keep_shape: bool = False,
    output_dir: str = None,
    runtime: str = "Htp",
    log_level: int = 1,
):
    # Auto-discover QAIModelBuilder env config
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    if not os.path.exists(model_path):
        print(f"[ERROR] Model not found: {model_path}")
        sys.exit(1)

    if model_name is None:
        model_name = Path(model_path).stem

    # Default output_dir to the directory containing the model binary,
    # so output_*.raw files land next to the model rather than in CWD.
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(model_path))
    os.makedirs(output_dir, exist_ok=True)

    # Configure QNN runtime
    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"\n{'='*60}")
    print(f"  QNN Generic Inference")
    print(f"  Model: {model_path}")
    print(f"  Runtime: {runtime.upper()}")
    print(f"{'='*60}\n")

    # Load model
    if io_data_type == "native":
        model = QNNModel(model_name, model_path,
                         input_data_type=DataType.NATIVE,
                         output_data_type=DataType.NATIVE)
    else:
        model = QNNModel(model_name, model_path)

    # Print model I/O info
    input_shapes = model.getInputShapes()
    input_dtypes = model.getInputDataType()
    output_shapes = model.getOutputShapes()
    output_dtypes = model.getOutputDataType()

    print("[Inputs]")
    dtypes_in = input_dtypes if isinstance(input_dtypes, list) else [input_dtypes]
    for i, (shape, dtype) in enumerate(zip(input_shapes, dtypes_in)):
        fmt = _detect_input_format(shape)
        fmt_str = f"  format={fmt}" if fmt else ""
        print(f"  [{i}] shape={shape}  dtype={dtype}{fmt_str}")

    print("\n[Outputs]")
    dtypes_out = output_dtypes if isinstance(output_dtypes, list) else [output_dtypes]
    for i, (shape, dtype) in enumerate(zip(output_shapes, dtypes_out)):
        print(f"  [{i}] shape={shape}  dtype={dtype}")
    print()

    # Determine numpy dtype for inputs
    np_dtype = _numpy_dtype_from_str(raw_dtype) if raw_dtype else _numpy_dtype_from_qnn_dtype(input_dtypes)
    if np_dtype is None:
        np_dtype = np.float32
        print("[WARN] Cannot infer input dtype from model; using float32.")

    # Prepare input data
    input_data = []
    if raw_paths:
        paths = [p.strip() for p in raw_paths.split(";") if p.strip()]
        print(f"[INFO] Loading {len(paths)} raw input file(s)...")
        for i, path in enumerate(paths):
            data = np.fromfile(path, dtype=np_dtype)
            if keep_shape:
                try:
                    exp_shape = input_shapes[i]
                    exp_size = int(np.prod(exp_shape))
                    if data.size == exp_size:
                        data = data.reshape(exp_shape)
                    else:
                        print(f"[WARN] Shape mismatch for input[{i}]: raw={data.size}, expected={exp_size}. Keeping 1D.")
                except Exception as e:
                    print(f"[WARN] Reshape failed for input[{i}]: {e}. Keeping 1D.")
            else:
                data = data.reshape(data.size)
            print(f"  input[{i}]: {data.shape}  dtype={data.dtype}  from={path}")
            input_data.append(data)
    else:
        print("[INFO] No raw inputs provided — using random data for verification.")
        for i, shape in enumerate(input_shapes):
            if np.issubdtype(np_dtype, np.floating):
                data = np.random.rand(*shape).astype(np_dtype)
            else:
                data = np.random.randint(0, 4, size=shape, dtype=np_dtype)
            if not keep_shape:
                data = data.reshape(data.size)
            print(f"  input[{i}]: {data.shape}  dtype={data.dtype}  (random)")
            input_data.append(data)

    # Run inference (with BURST performance mode — must be after model load)
    print("\n[INFO] Running inference...")
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        outputs = model.Inference(input_data)
    finally:
        PerfProfile.RelPerfProfileGlobal()

    # Save outputs
    print("\n[Outputs]")
    for i, out in enumerate(outputs):
        out_arr = np.array(out)
        try:
            out_arr = out_arr.reshape(output_shapes[i])
        except Exception:
            pass
        out_path = os.path.join(output_dir, f"output_{i}.raw")
        out_arr.tofile(out_path)
        print(f"  output[{i}]: shape={out_arr.shape}  dtype={out_arr.dtype}  "
              f"range=[{out_arr.min():.4f}, {out_arr.max():.4f}]  saved={out_path}")

    print(f"\n[DONE] Inference complete. Outputs saved to: {output_dir}")
    return outputs


def main():
    parser = argparse.ArgumentParser(
        description="Generic QNN Model Inference (model-builder)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick verification with random input
  python inference/infer_generic.py --model model.bin

  # With raw input files
  python inference/infer_generic.py --model model.bin --raw_paths "input0.raw;input1.raw"

  # Quantized model (native dtype)
  python inference/infer_generic.py --model model.bin --io_data_type native

  # Save outputs to specific directory
  python inference/infer_generic.py --model model.bin --output_dir ./outputs
        """
    )
    parser.add_argument("--model", required=True, help="Path to QNN context binary (.bin)")
    parser.add_argument("--model_name", default=None, help="Model name for QNNContext")
    parser.add_argument("--raw_paths", default=None,
                        help="Semicolon-separated paths to raw input files")
    parser.add_argument("--io_data_type", choices=["float", "native"], default="float",
                        help="I/O data type: 'float' (default) or 'native' (quantized models)")
    parser.add_argument("--raw_dtype", default=None,
                        help="Numpy dtype for raw inputs: float32/float16/int8/uint8/int32 etc.")
    parser.add_argument("--keep_shape", action="store_true",
                        help="Keep tensors in model shape (default: flatten to 1D)")
    parser.add_argument("--output_dir", default=None,
                        help="Directory to save output .raw files (default: same dir as model)")
    parser.add_argument("--runtime", default="Htp", choices=["Htp", "Cpu"],
                        help="QNN runtime: Htp (default) or Cpu")
    parser.add_argument("--log_level", type=int, default=1,
                        help="Log level 0-5 (default: 1=WARN)")

    args = parser.parse_args()
    run_inference(
        model_path=args.model,
        model_name=args.model_name,
        raw_paths=args.raw_paths,
        io_data_type=args.io_data_type,
        raw_dtype=args.raw_dtype,
        keep_shape=args.keep_shape,
        output_dir=args.output_dir,
        runtime=args.runtime,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
