# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

#!/usr/bin/env python3
"""
QNN Quantized Conversion Script

Converts ONNX models to quantized QNN format.

Supports QAIRT SDK 2.45+ on Windows on Snapdragon (WoS) ARM64 devices.

Supported quantization modes:
  - W8A16:  --act_bw 16 --weight_bw 8           (recommended for vision)
  - W8A8:   --act_bw 8  --weight_bw 8           (bias defaults to FP32)
  - W8A8B8: --act_bw 8  --weight_bw 8 --bias_bw 8  (bias explicitly INT8)
  - W4A16:  --act_bw 16 --weight_bw 4
  - W4A8:   --act_bw 8  --weight_bw 4

QAIRT 2.45 WoS ARM64 Tool Path Rules:
  - qnn-onnx-converter:      bin/x86_64-windows-msvc/  (Python script, x86 emulation)
  - qnn-model-lib-generator: bin/aarch64-windows-msvc/ (NOT x86_64 — compiles ARM64 DLL)

Usage:
  # Simple conversion (static input model)
  python qai_convert_int.py --input_network model.onnx --input_list calibration_list.txt

  # Dynamic input model (specify fixed dimensions)
  python qai_convert_int.py --input_network model.onnx --input_list calib.txt \
    --input-dim input,1,3,64,64

  # W8A16 (default, recommended for vision models)
  python qai_convert_int.py --input_network model.onnx --input_list calib.txt \
    --act_bw 16 --weight_bw 8

  # W8A8 (bias defaults to FP32)
  python qai_convert_int.py --input_network model.onnx --input_list calib.txt \
    --act_bw 8 --weight_bw 8

  # W8A8B8 (bias explicitly quantized to INT8)
  python qai_convert_int.py --input_network model.onnx --input_list calib.txt \
    --act_bw 8 --weight_bw 8 --bias_bw 8

  # W4A16
  python qai_convert_int.py --input_network model.onnx --input_list calib.txt \
    --act_bw 16 --weight_bw 4

  # W4A8
  python qai_convert_int.py --input_network model.onnx --input_list calib.txt \
    --act_bw 8 --weight_bw 4

Known Issues & Solutions:
  - "Missing command line inputs for dynamic inputs": Use --input-dim name,1,3,H,W
  - "Access is denied": Use absolute output path
  - "is not a cpp model file": Script auto-fixes this (same as qai_convert_fp.py)
  - "calibration_list.txt not found": Create calibration list with raw input files
  - WoS ARM64: qnn-model-lib-generator is in aarch64-windows-msvc/, NOT x86_64

Args:
  --input_network: Path to ONNX file
  --input_list: Path to calibration list file (required)
  --act_bw: Activation bitwidth (default: 16)
  --weight_bw: Weight bitwidth (default: 8, use 4 for W4A16/W4A8)
  --bias_bw: Bias bitwidth (optional, only for W8A8B8 mode; omit = bias stays FP32)
  --input-dim: Input dimensions for dynamic models (repeatable)
  --target-arch: Target architecture (default: auto-detected)
  --host-arch: Host toolchain for converter (default: auto-detected)
  --no-simplification: Pass --no_simplification to converter (recommended for WoS)
"""

import argparse
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ─── QAIModelBuilder env_config.json auto-discovery ──────────────────────────

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
                    cfg = json.load(f)
                print(f"[INFO] Loaded QAIRT env config: {candidate}")
                return cfg
            except Exception as e:
                print(f"[WARN] Failed to parse {candidate}: {e}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


def _apply_env_config(cfg: dict) -> None:
    """Apply settings from qairt_env.json to the current process environment."""
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


def _cleanup_tmp_folders(search_dir: str) -> None:
    """Remove tmp_<pid>/ folders created by qnn-model-lib-generator."""
    try:
        for entry in os.scandir(search_dir):
            if entry.is_dir() and entry.name.startswith("tmp_"):
                try:
                    shutil.rmtree(entry.path)
                    print(f"Cleaned up temp folder: {entry.path}")
                except OSError as e:
                    print(f"Warning: could not remove temp folder {entry.path}: {e}")
    except Exception:
        pass


def _get_linux_toolchain_dir_int(sdk_root: str) -> str:
    """Return the first SDK bin/ subdirectory containing qairt-converter (Linux)."""
    import platform as _platform
    machine = _platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        candidates = [
            "aarch64-oe-linux-gcc11.2",
            "aarch64-ubuntu-gcc9.4",
            "aarch64-oe-linux-gcc9.3",
            "aarch64-oe-linux-gcc8.2",
        ]
    else:
        candidates = ["x86_64-linux-clang"]
    from pathlib import Path as _Path
    for d in candidates:
        if _Path(sdk_root, "bin", d, "qairt-converter").exists():
            return d
    return candidates[0]


def _get_lib_generator_arch(qnn_sdk_root: str, host_arch: str):
    """
    Determine the correct arch directory for qnn-model-lib-generator.

    QAIRT 2.45 WoS ARM64 rule:
      - qnn-onnx-converter lives in x86_64-windows-msvc/ (Python script, x86 emulation)
      - qnn-model-lib-generator lives in aarch64-windows-msvc/ (compiles native ARM64 DLL)

    On Linux, there is no model-lib-generator step; returns None.
    """
    if platform.system().lower() != "windows":
        return None   # Linux: no model-lib-generator step

    # On Windows: check if aarch64-windows-msvc/qnn-model-lib-generator exists
    aarch64_gen = os.path.join(qnn_sdk_root, "bin", "aarch64-windows-msvc", "qnn-model-lib-generator")
    if os.path.exists(aarch64_gen):
        print(f"[INFO] Using aarch64-windows-msvc/qnn-model-lib-generator (QAIRT 2.45 WoS mode)")
        return "aarch64-windows-msvc"

    # Fallback: use same arch as converter (older SDK behavior)
    return host_arch


def get_cpu_arch_from_systeminfo():
    try:
        result = subprocess.run(['systeminfo'], capture_output=True, text=True, check=True, encoding='utf-8')
        output = result.stdout

        system_type_match = re.search(r"System Type:\s*(.*?)\r?\n", output, re.IGNORECASE)
        os_name_match = re.search(r"OS Name:\s*(.*?)\r?\n", output, re.IGNORECASE)

        if system_type_match:
            system_type = system_type_match.group(1).strip()
            arch_match = re.search(r'(x86|amd64|arm64|arm)', system_type, re.IGNORECASE)
            if arch_match:
                return arch_match.group(1).lower()

        if os_name_match:
            os_name = os_name_match.group(1).strip()
            arch_match = re.search(r'(x86|amd64|arm64|arm)', os_name, re.IGNORECASE)
            if arch_match:
                return arch_match.group(1).lower()

        for line in output.splitlines():
            if any(tok in line for tok in ('ARM', 'ARM64', 'Intel', 'AMD', 'Qualcomm')):
                arch_match = re.search(r'(arm64|aarch64|arm|amd64|x86_64|x86)', line, re.IGNORECASE)
                if arch_match:
                    val = arch_match.group(1).lower()
                    if val in ('x86_64',):
                        return 'amd64'
                    if val == 'aarch64':
                        return 'arm64'
                    return val

        return None

    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        return None


def detect_host_arch():
    """
    Detects the host architecture and selects the appropriate toolchain for qnn-onnx-converter.

    QAIRT 2.45 WoS ARM64 note:
      On Windows (including WoS ARM64), qnn-onnx-converter is always in x86_64-windows-msvc/
      because it runs under x86 Python emulation.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    # NOTE: On WoS ARM64, platform.machine() returns "AMD64" (x86 emulation) — this is expected.
    # The host arch here is for qnn-onnx-converter only (always x86_64-windows-msvc on Windows).
    # The TARGET arch (for DLL compilation) is detected separately by detect_target_arch().
    print(f"System: {system}, Machine: {machine} (converter host arch - x86 emulation on WoS ARM64 is normal)")

    if system == "windows":
        # On Windows (including WoS ARM64), qnn-onnx-converter uses x86_64-windows-msvc emulation.
        return "x86_64-windows-msvc"
    elif system == "linux":
        if machine in ["amd64", "x86_64"]:
            return "x86_64-linux-clang"
        elif machine in ["arm64", "aarch64"]:
            return "aarch64-ubuntu-gcc9.4"

    # Default fallback
    return "x86_64-windows-msvc"


def detect_target_arch():
    """
    Detect a reasonable QNN target architecture string based on the current device.
    """
    system = platform.system().lower()
    if system == "windows":
        arch = get_cpu_arch_from_systeminfo() or platform.machine().lower()
        if arch and 'arm' in arch:
            return "windows-aarch64"
        return "windows-x86_64"

    if system == "linux":
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            return "aarch64-ubuntu-gcc9.4"
        return "x86_64-linux-clang"

    # fallback
    return "x86_64-linux-clang"


def find_onnx_files(search_dir="."):
    """Find all ONNX files in the specified directory."""
    search_path = Path(search_dir)
    return list(search_path.glob("*.onnx"))


def get_model_info(model_path, act_bw=16, weight_bw=8, bias_bw=None, output_root=None):
    """Extract model information from the ONNX file path."""
    model_path = Path(model_path)
    model_name = model_path.stem
    if bias_bw is not None:
        model_name_quant = f"{model_name}_a{act_bw}_w{weight_bw}_b{bias_bw}"
    else:
        model_name_quant = f"{model_name}_a{act_bw}_w{weight_bw}"
    model_dir = model_path.parent
    abs_model_dir = os.path.abspath(str(model_dir))

    output_dir_name = f"test_libs_{model_name_quant}_aarch64_a{act_bw}_w{weight_bw}"
    base_out_dir = os.path.abspath(output_root) if output_root else abs_model_dir
    output_dir_path = os.path.join(base_out_dir, output_dir_name)
    cpp_path = os.path.join(base_out_dir, f"{model_name_quant}.cpp")
    bin_path = os.path.join(base_out_dir, f"{model_name_quant}.bin")

    return {
        "model_path": str(model_path),
        "model_name": model_name_quant,
        "model_dir": abs_model_dir,
        "output_dir_path": output_dir_path,
        "cpp_path": cpp_path,
        "bin_path": bin_path
    }


def convert_model(model_info, cwd, calibration_list_path, act_bw=16, weight_bw=8,
                  bias_bw=None, qnn_sdk_root=None, host_toolchain="",
                  device_toolchain="", cleanup_intermediate=True,
                  input_dims=None, no_simplification=False):
    """
    Convert ONNX model to quantized QNN format.

    Supports:
      - A8W8  (INT8):  act_bw=8,  weight_bw=8
      - A16W8:         act_bw=16, weight_bw=8  (default, recommended for vision)
      - A8W8B8:        act_bw=8,  weight_bw=8, bias_bw=8

    QAIRT 2.45 WoS ARM64 note:
      - qnn-onnx-converter: x86_64-windows-msvc/ (x86 emulation)
      - qnn-model-lib-generator: aarch64-windows-msvc/ (native ARM64 compiler)

    Args:
        model_info: Dictionary containing model paths and information
        cwd: Current working directory (absolute path)
        calibration_list_path: Path to calibration list file
        act_bw: Activation bit width (default: 16)
        weight_bw: Weight bit width (default: 8)
        bias_bw: Bias bit width (optional, for A8W8B8 mode)
        qnn_sdk_root: QAIRT SDK root path
        host_toolchain: Host toolchain for converter (default: auto-detected)
        device_toolchain: Device toolchain for lib generator (default: auto-detected)
        cleanup_intermediate: Remove intermediate files after conversion
        input_dims: List of (input_name, dims) tuples for dynamic inputs
        no_simplification: Pass --no_simplification to converter (WoS recommended)
    """
    model_path = model_info["model_path"]
    cpp_path = model_info["cpp_path"]
    bin_path = model_info["bin_path"]
    output_dir_path = model_info["output_dir_path"]

    quant_desc = f"a{act_bw}_w{weight_bw}"
    if bias_bw is not None:
        quant_desc += f"_b{bias_bw}"
    print(f"Converting {model_path} to {quant_desc} quantized format...")

    # Auto-detect host toolchain if not provided
    if not host_toolchain:
        host_toolchain = detect_host_arch()
        print(f"Auto-detected host toolchain: {host_toolchain}")

    # Auto-detect device toolchain if not provided
    if not device_toolchain:
        device_toolchain = detect_target_arch()
        print(f"Auto-detected device toolchain: {device_toolchain}")

    # Determine QAIRT SDK root
    # Auto-discover QAIModelBuilder env_config.json
    _apply_env_config(_find_qairt_env_config())

    if qnn_sdk_root is None:
        qnn_sdk_root = os.environ.get('QAIRT_SDK_ROOT')
        if not qnn_sdk_root:
            print("Error: QAIRT_SDK_ROOT not set.", file=sys.stderr)
            print("  Option 1: set QAIRT_SDK_ROOT=<path to QAIRT SDK>", file=sys.stderr)
            print("  Option 2: Run Setup.bat (reads from data\\config\\qairt_env.json)", file=sys.stderr)
            return False

    # Check if QAIRT_SDK_ROOT exists
    if not os.path.exists(qnn_sdk_root):
        print(f"Error: QAIRT_SDK_ROOT path does not exist: {qnn_sdk_root}", file=sys.stderr)
        return False

    # Ensure QNN_AARCH64_UBUNTU_GCC_94 is set (required by QNN tools on Linux)
    if platform.system().lower() == "linux" and 'QNN_AARCH64_UBUNTU_GCC_94' not in os.environ:
        print("Warning: QNN_AARCH64_UBUNTU_GCC_94 environment variable is not set", file=sys.stderr)
        print("Setting it to '/' as default", file=sys.stderr)
        os.environ['QNN_AARCH64_UBUNTU_GCC_94'] = '/'

    python_exe = sys.executable
    if not os.path.exists(python_exe):
        python_exe = "python"

    # Convert paths to absolute paths
    abs_model_path = os.path.abspath(os.path.join(str(cwd), model_path))
    abs_cpp_path = os.path.abspath(os.path.join(str(cwd), cpp_path))
    abs_bin_path = os.path.abspath(os.path.join(str(cwd), bin_path))
    abs_output_dir = os.path.abspath(os.path.join(str(cwd), output_dir_path))
    abs_calibration_list = os.path.abspath(os.path.join(str(cwd), str(calibration_list_path)))

    # ── Linux path: qairt-converter → DLC, qairt-quantizer → quantized DLC ──
    if platform.system().lower() == "linux":
        toolchain = _get_linux_toolchain_dir_int(qnn_sdk_root)
        converter_path = os.path.join(qnn_sdk_root, "bin", toolchain, "qairt-converter")
        if not os.path.exists(converter_path):
            print(f"Error: qairt-converter not found at: {converter_path}", file=sys.stderr)
            return False

        # Step 1: ONNX → DLC
        dlc_path = abs_cpp_path.replace(".cpp", ".dlc")
        conv_cmd = [converter_path,
                    "--input_network", abs_model_path,
                    "--output_path", dlc_path,
                    "--preserve_io"]
        if no_simplification:
            conv_cmd.append("--no_simplification")
        if input_dims:
            for input_name, dims in input_dims:
                conv_cmd.extend(["-d", input_name, dims])
        r = subprocess.run(conv_cmd, cwd=abs_output_dir)
        if r.returncode != 0:
            print(f"Error: qairt-converter failed (exit {r.returncode})", file=sys.stderr)
            return False

        # Step 2 (optional): DLC → quantized DLC
        quantizer_path = os.path.join(qnn_sdk_root, "bin", toolchain, "qairt-quantizer")
        if os.path.exists(quantizer_path) and os.path.exists(abs_calibration_list):
            qdlc_path = dlc_path.replace(".dlc", "_quantized.dlc")
            q_cmd = [quantizer_path,
                     "--input_dlc", dlc_path,
                     "--output_dlc", qdlc_path,
                     "--input_list", abs_calibration_list,
                     "--act_bitwidth", str(act_bw),
                     "--weights_bitwidth", str(weight_bw)]
            if bias_bw is not None:
                q_cmd.extend(["--bias_bitwidth", str(bias_bw)])
            r = subprocess.run(q_cmd, cwd=abs_output_dir)
            if r.returncode != 0:
                print(f"Warning: qairt-quantizer failed (exit {r.returncode}); using unquantized DLC",
                      file=sys.stderr)
        elif not os.path.exists(abs_calibration_list):
            print("Warning: calibration list not found; skipping quantization step", file=sys.stderr)
        return True

    # ── Windows path ──────────────────────────────────────────────────────────
    # Build the qnn-onnx-converter command
    converter_path = os.path.join(qnn_sdk_root, "bin", host_toolchain, "qnn-onnx-converter")
    converter_cmd = [
        python_exe, converter_path,
        "--input_network", abs_model_path,
        "--output_path", abs_cpp_path,
        "--preserve_io",
        "--input_list", abs_calibration_list,
        "--act_bitwidth", str(act_bw),
        "--weights_bitwidth", str(weight_bw)
    ]

    if bias_bw is not None:
        converter_cmd.extend(["--bias_bitwidth", str(bias_bw)])

    if no_simplification:
        converter_cmd.append("--no_simplification")

    if input_dims:
        for input_name, dims in input_dims:
            converter_cmd.extend(["-d", input_name, dims])

    # QAIRT 2.45 WoS: lib generator may be in aarch64-windows-msvc/
    lib_gen_arch = _get_lib_generator_arch(qnn_sdk_root, host_toolchain)
    lib_gen_path = os.path.join(qnn_sdk_root, "bin", lib_gen_arch, "qnn-model-lib-generator")
    lib_gen_cmd = [
        python_exe, lib_gen_path,
        "-c", abs_cpp_path,
        "-b", abs_bin_path,
        "-o", abs_output_dir,
        "-t", device_toolchain
    ]

    # Set up environment with PYTHONPATH
    model_env = os.environ.copy()
    qnn_python_path = os.path.join(qnn_sdk_root, "lib", "python")
    if 'PYTHONPATH' in model_env:
        model_env['PYTHONPATH'] = qnn_python_path + os.pathsep + model_env['PYTHONPATH']
    else:
        model_env['PYTHONPATH'] = qnn_python_path

    try:
        # ── Execute the converter command with real-time progress output ──────────
        # Quantization runs full forward inference on ALL calibration samples on CPU.
        # For large models this can take 20-60+ minutes — real-time output is critical
        # so the caller can see progress and set appropriate timeout (>= 3600s).
        print(f"\nRunning qnn-onnx-converter ({quant_desc})...")
        print(f"Command: {' '.join(converter_cmd)}")
        print(f"[INFO] Quantization calibration in progress - this may take 20-60+ minutes for large models.")
        print(f"[INFO] Each 'input ->' line below = one calibration sample processed.", flush=True)

        # lib_gen_cwd = abs_output_dir so that qnn-model-lib-generator creates
        # its tmp_<pid>/ scratch folders inside the model output directory rather
        # than in the parent (which could be the project root or server CWD).
        # _cleanup_tmp_folders() below removes them after a successful run.
        lib_gen_cwd = abs_output_dir
        os.makedirs(lib_gen_cwd, exist_ok=True)

        # Use Popen for real-time output instead of subprocess.run()
        # This allows progress to be visible immediately rather than buffered.
        import time as _time
        # Bug 8 fix: set cwd to the output root directory so qnn-onnx-converter
        # runs in the intended location rather than inheriting the server process
        # CWD (QAIModelBuilder/backend/), which would cause any tool that
        # defaults to ./output/ to create stray directories there.
        proc = subprocess.Popen(
            converter_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding='utf-8',
            errors='replace',
            env=model_env,
            cwd=lib_gen_cwd
        )
        sample_count = 0
        start_time = _time.time()
        for line in proc.stdout:
            line = line.rstrip()
            # Detect calibration sample progress lines (e.g., "input → sample_0001.raw")
            if 'input \u2192' in line or 'input ->' in line or ('input' in line.lower() and '.raw' in line):
                sample_count += 1
                elapsed = int(_time.time() - start_time)
                print(f"[PROGRESS] Calibration sample {sample_count} ({elapsed}s elapsed): {line}", flush=True)
            elif line:
                print(line, flush=True)
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, converter_cmd)

        elapsed_total = int(_time.time() - start_time)
        print(f"[OK] qnn-onnx-converter complete - {sample_count} calibration samples processed in {elapsed_total}s")
        print(f"Successfully converted ONNX to C++ and binary for {model_info['model_name']}")

        # Fix: Handle case where converter creates file without .cpp extension
        if not os.path.exists(abs_cpp_path):
            cpp_no_ext = abs_cpp_path.replace('.cpp', '')
            if os.path.exists(cpp_no_ext) and not cpp_no_ext.endswith('.bin'):
                os.rename(cpp_no_ext, abs_cpp_path)
                print(f"Fixed: Renamed {cpp_no_ext} to {abs_cpp_path}")

        # Execute the lib generator command
        print(f"\nRunning qnn-model-lib-generator (arch: {lib_gen_arch})...")
        print(f"Command: {' '.join(lib_gen_cmd)}")
        subprocess.run(
            lib_gen_cmd,
            check=True,
            encoding='utf-8',
            errors='replace',
            env=model_env,
            cwd=lib_gen_cwd
        )

        print(f"Successfully converted {model_path} to {output_dir_path}")

        # Cleanup intermediate files if requested
        if cleanup_intermediate:
            intermediate_files = [abs_cpp_path, abs_bin_path, abs_cpp_path.replace('.cpp', '_net.json')]
            for file_path in intermediate_files:
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        print(f"Cleaned up intermediate file: {file_path}")
                    except OSError as e:
                        print(f"Could not remove intermediate file {file_path}: {e}")

            _cleanup_tmp_folders(abs_output_dir)

        return True

    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] INT quantization failed for {model_info['model_name']}")
        print(f"\nTroubleshooting tips:")
        print(f"  1. If error mentions 'dynamic inputs', add: --input-dim name,1,3,H,W")
        print(f"  2. If 'access denied', ensure output path is writable")
        print(f"  3. If 'calibration_list' not found, check --input_list path")
        print(f"  4. If 'unsupported operator', check dry-run first")
        print(f"  5. On WoS ARM64: ensure vcvarsall.bat arm64 was called")
        print(f"  6. On WoS ARM64: try adding --no-simplification flag")
        print(f"  7. If timeout: increase timeout to >= 3600s (quantization is slow for large models)")
        print(f"\nFailed command: {' '.join(converter_cmd)}")
        return False
    except Exception as e:
        print(f"\n[ERROR] Unexpected error converting {model_path}: {e}")
        return False


def main():
    """Main function to process all ONNX files."""
    parser = argparse.ArgumentParser(
        description='Convert ONNX models to quantized QNN format (W8A16/W8A8/W8A8B8/W4A16/W4A8)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quantization Modes:
  W8A16 (default, recommended for vision):
    python qai_convert_int.py --input_network model.onnx --input_list calib.txt

  W8A8 (bias defaults to FP32):
    python qai_convert_int.py --input_network model.onnx --input_list calib.txt \\
      --act_bw 8 --weight_bw 8

  W8A8B8 (bias explicitly quantized to INT8):
    python qai_convert_int.py --input_network model.onnx --input_list calib.txt \\
      --act_bw 8 --weight_bw 8 --bias_bw 8

  W4A16:
    python qai_convert_int.py --input_network model.onnx --input_list calib.txt \\
      --act_bw 16 --weight_bw 4

  W4A8:
    python qai_convert_int.py --input_network model.onnx --input_list calib.txt \\
      --act_bw 8 --weight_bw 4

Prerequisites:
  Before running this script, ensure you have:
  1. QAIRT_SDK_ROOT environment variable set
  2. Calibration data (raw float32 binary files)
  3. Calibration list file (one path per line)
  4. On WoS ARM64: vcvarsall.bat arm64 called, VCTargetsPath set to VS 2022 Community

Calibration Data Format:
  - Raw float32 binary files (.raw)
  - Shape matching model input (e.g., 1x3x64x64 = 49152 floats)
  - 50-200 representative samples recommended
  - Input format: NHWC (for HTP inference)

Calibration List Format (calibration_list.txt):
  input:=calib_data/sample_001.raw
  input:=calib_data/sample_002.raw
  ...
        """
    )

    parser.add_argument(
        '--input_network',
        type=str,
        default=None,
        help='Path to input ONNX model file. If not specified, converts all .onnx files in current directory'
    )

    parser.add_argument(
        '--output_path',
        type=str,
        default=None,
        help='Path for output .cpp file. If not specified, uses <model_name>_a{act_bw}_w{weight_bw}.cpp'
    )

    parser.add_argument(
        '--input_list',
        type=str,
        default='calibration_list.txt',
        help='Path to calibration list file for quantization (default: calibration_list.txt)'
    )

    parser.add_argument(
        '--act_bw',
        type=int,
        default=16,
        help='Activation bit width for quantization (default: 16). Use 8 for W8A8/W4A8, 16 for W8A16/W4A16'
    )

    parser.add_argument(
        '--weight_bw',
        type=int,
        default=8,
        help='Weight bit width for quantization (default: 8). Use 4 for W4A16/W4A8'
    )

    parser.add_argument(
        '--bias_bw',
        type=int,
        default=None,
        help='Bias bit width for quantization (optional). Set to 8 for W8A8B8 only; omit = bias stays FP32'
    )

    parser.add_argument(
        '--qnn_sdk_root',
        type=str,
        default=None,
        help='QAIRT SDK root path (default: from QAIRT_SDK_ROOT env)'
    )

    parser.add_argument(
        '--host-arch',
        type=str,
        default='',
        help='Host toolchain for qnn-onnx-converter (default: auto-detected). On WoS: x86_64-windows-msvc'
    )

    parser.add_argument(
        '--target-arch',
        type=str,
        default='',
        help='Device toolchain for model compilation (default: auto-detected). On WoS: windows-aarch64'
    )

    parser.add_argument(
        '--output-root',
        type=str,
        default=None,
        dest='output_root',
        help='Optional root folder for generated test_libs_* output (default: alongside the .onnx).'
    )

    parser.add_argument(
        '--no-cleanup',
        action='store_false',
        dest='cleanup_intermediate',
        help="Don't cleanup intermediate files (.bin, .cpp, .json) after successful conversion."
    )

    parser.add_argument(
        '--input-dim',
        action='append',
        default=[],
        dest='input_dims',
        metavar=("INPUT_NAME,DIMS"),
        help="Explicit input dimensions for dynamic inputs. Format: input_name,1,3,224,224 (repeatable)."
    )

    parser.add_argument(
        '--no-simplification',
        action='store_true',
        dest='no_simplification',
        help="Pass --no_simplification to qnn-onnx-converter. Recommended for WoS ARM64 (QAIRT 2.45)."
    )

    args = parser.parse_args()

    parsed_input_dims = None
    if args.input_dims:
        parsed_input_dims = []
        for item in args.input_dims:
            if ',' in item:
                parts = item.split(',', 1)
                input_name = parts[0]
                dims = parts[1]
                parsed_input_dims.append((input_name, dims))
            else:
                print(f"Warning: Invalid input-dim format: {item}. Expected: input_name,dim1,dim2,...")
                parsed_input_dims.append((item, "1"))

    cwd = os.getcwd()

    # Check if calibration list exists
    calibration_list_path = args.input_list
    calib_path_full = os.path.join(cwd, calibration_list_path)
    if not os.path.exists(calib_path_full):
        print(f"\n[WARNING] Calibration list not found: {calib_path_full}", file=sys.stderr)
        print(f"\nCalibration list is REQUIRED for quantization.")
        print(f"Format: One raw input file path per line.")
        print(f"Example calibration_list.txt:")
        print(f"  input:=calib_data/sample_001.raw")
        print(f"  input:=calib_data/sample_002.raw")
        print(f"  ...")
        print(f"\nEach .raw file should be:")
        print(f"  - Float32 binary data")
        print(f"  - Shape matching model input (e.g., 1x3x64x64 = 49152 floats)")
        print(f"  - Input format: NHWC (for HTP inference)")
        print(f"  - 50-200 representative samples recommended")
        response = input("\nContinue anyway without calibration? (y/n): ")
        if response.lower() != 'y':
            sys.exit(1)

    # Determine which ONNX files to process
    if args.input_network:
        if not os.path.exists(args.input_network):
            print(f"Error: Input file '{args.input_network}' not found", file=sys.stderr)
            sys.exit(1)
        onnx_files = [Path(args.input_network)]
    else:
        onnx_files = find_onnx_files()
        if not onnx_files:
            print("No ONNX files found in the current directory")
            sys.exit(1)

    quant_desc = f"act_bw={args.act_bw}, weight_bw={args.weight_bw}"
    if args.bias_bw is not None:
        quant_desc += f", bias_bw={args.bias_bw}"
    print(f"Found {len(onnx_files)} ONNX file(s) to convert")
    print(f"Quantization: {quant_desc}")

    success_count = 0
    fail_count = 0

    for onnx_file in onnx_files:
        model_info = get_model_info(onnx_file, args.act_bw, args.weight_bw, args.bias_bw, args.output_root)

        # Override output path if specified
        if args.output_path and len(onnx_files) == 1:
            model_info['cpp_path'] = args.output_path
            model_info['bin_path'] = os.path.splitext(args.output_path)[0] + '.bin'

        if convert_model(
            model_info, cwd, calibration_list_path,
            args.act_bw, args.weight_bw, args.bias_bw,
            args.qnn_sdk_root, args.host_arch, args.target_arch,
            args.cleanup_intermediate, parsed_input_dims,
            args.no_simplification
        ):
            success_count += 1
        else:
            fail_count += 1

    print("\n" + "="*50)
    print(f"Conversion Summary:")
    print(f"  Total files: {len(onnx_files)}")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {fail_count}")
    print("="*50)

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
