# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
run_pipeline.py - End-to-End QNN DLC Pipeline for WoS ARM64 (QAIRT 2.45+)

>>> DEFAULT PIPELINE (strategic direction) <<<

Steps:
  1. ONNX -> DLC          (qairt-converter)                    -- FP model
  2. DLC  -> quant DLC    (qairt-quantizer, optional)          -- W8A8 / W8A16 / ...
  3. DLC  -> .bin         (qai_dev_gen_contextbin.py --model <file>.dlc)

Compared to the legacy DLL pipeline (see ``run_pipeline_legacy.py``):
  * No VS ARM64 ``.dll`` compile step -- immune to VCTargetsPath / MSBuild
    breakage on the developer's box.
  * Uniform DLC intermediate is portable across HTP v73 / v81 by default
    (SoC optimisation is opt-in via ``--soc_optimized``), and across the SNPE
    runtime.
  * Full CLE / percentile / per-channel / per-row / bf16 / w16a16 support
    at the qairt-quantizer stage.

Backwards compatibility:
  Every CLI flag of the legacy ``run_pipeline.py`` (``--model``, ``--output``,
  ``--precision``, ``--act_bw``, ``--weight_bw``, ``--bias_bw``, ``--calib_list``,
  ``--input_dim``, ``--config``, ``--htp_version``, ``--skip_contextbin``,
  ``--no_simplification``) is preserved with identical semantics. In particular:
  * ``--no_simplification`` is now transparently translated to the qairt-converter
    equivalent ``--onnx_skip_simplification`` (legacy behaviour was to warn and
    ignore).
  * ``--bias_bw`` when unspecified keeps the legacy semantics: bias stays FP32
    (the script auto-adds ``--float_bias_bitwidth 32`` so it does not silently
    change to INT8 which is qairt-quantizer's default).
  * ``--calib_list`` accepts the legacy raw-path format; a warning is emitted
    if the file uses the ``input:=`` prefix (that prefix belongs to
    qnn-onnx-converter, not qairt-quantizer).

Quick usage:
  python run_pipeline.py --model model.onnx --output output --precision fp16
  python run_pipeline.py --model model.onnx --output output --precision w8a8 ^
      --calib_list calib.txt
  python run_pipeline.py --model model.onnx --output output --precision w8a8 ^
      --calib_list calib.txt --cle --per_channel --dump_encoding
  python run_pipeline.py --model model.onnx --output output --precision bf16
  python run_pipeline.py --model model.onnx --output output --precision w8a16 ^
      --calib_list calib.txt                             # default = cross-platform DLC
  python run_pipeline.py --model model.onnx --output output --precision w8a16 ^
      --calib_list calib.txt --soc_optimized             # SoC-optimized for --htp_version

CLI reference: see argparse --help below or references/qnn_conversion.md.
"""

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Precision preset table
# ---------------------------------------------------------------------------
# (act_bw, weight_bw, bias_bw); None entries mean "not applicable at that stage".
# ``is_float`` derived from the precision name below.
_PRECISION_TABLE = {
    "fp32":   (None, None, None),
    "fp16":   (None, None, None),
    "bf16":   (None, None, None),
    "w4a8":   (8,    4,    None),
    "w4a16":  (16,   4,    None),
    "w8a8":   (8,    8,    None),
    "w8a8b8": (8,    8,    8),
    "w8a16":  (16,   8,    None),
    "w16a16": (16,   16,   None),
}


def _is_float_precision(p: str) -> bool:
    return p in ("fp32", "fp16", "bf16")


def _needs_restrict_steps(act_bw, weight_bw) -> bool:
    """16-bit activation or 16-bit weight requires --restrict_quantization_steps."""
    return (act_bw == 16) or (weight_bw == 16)


# ---------------------------------------------------------------------------
# Platform / arch detection
# ---------------------------------------------------------------------------

def _detect_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _detect_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return "unknown"


def _get_host_toolchain_dir(sdk_root: str) -> str:
    """Return the SDK bin/ subdirectory that contains qairt-converter for the current host.

    On Windows-on-Snapdragon (ARM64) the host tools live in ``arm64x-windows-msvc``;
    on x64 Windows they live in ``x86_64-windows-msvc``. Linux hosts probe several
    gcc variants until the converter is found.
    """
    system = platform.system().lower()
    arch = _detect_arch()

    if system == "windows":
        if arch == "aarch64":
            candidates = ["arm64x-windows-msvc", "x86_64-windows-msvc"]
        else:
            candidates = ["x86_64-windows-msvc"]
        ext = ".exe"
    else:
        if arch == "aarch64":
            candidates = [
                "aarch64-oe-linux-gcc11.2",
                "aarch64-ubuntu-gcc9.4",
                "aarch64-oe-linux-gcc9.3",
                "aarch64-oe-linux-gcc8.2",
                "aarch64-linux-clang",
            ]
        else:
            candidates = ["x86_64-linux-clang"]
        ext = ""

    for d in candidates:
        # QAIRT 2.48+ ships qairt-converter as a bare Python launcher (no .exe).
        # Earlier SDKs had qairt-converter.exe.  Accept either.
        if (Path(sdk_root, "bin", d, f"qairt-converter{ext}").exists()
                or Path(sdk_root, "bin", d, "qairt-converter").exists()):
            return d
    # Fallback: first candidate; caller will error out with a clear "tool not found" message.
    return candidates[0]


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------

def find_qairt_env(start: Path) -> dict:
    """Walk up directory tree from start to find data/config/qairt_env.json."""
    current = start.resolve()
    for _ in range(10):
        candidate = current / "data" / "config" / "qairt_env.json"
        if not candidate.exists():
            candidate = current / "config" / "qairt_env.json"
        if candidate.exists():
            with open(candidate, encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"[INFO] Config: {candidate}")
            return cfg
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


# ---------------------------------------------------------------------------
# QAIRT SDK path setup (no VS ARM64 init required for DLC path!)
# ---------------------------------------------------------------------------
# Note: The legacy pipeline needed VS ARM64 env for Step 2 (DLL compile). The
# DLC pipeline SKIPS DLL compilation entirely, so vcvarsall.bat and
# VCTargetsPath are NOT needed for the converter / quantizer stages. The
# generator (Step 3) still needs the aarch64 QNN runtime DLLs on PATH; those
# are copied into the output_dir by qai_dev_gen_contextbin.py, so we do NOT
# spawn a cmd /c "vcvarsall.bat arm64 && set" subprocess here.
# ---------------------------------------------------------------------------

def _path_list(env_var: str) -> list:
    val = os.environ.get(env_var, "")
    return [p for p in val.split(os.pathsep) if p] if val else []


def apply_qairt_env(cfg: dict) -> tuple:
    """
    Set QAIRT_SDK_ROOT, PYTHONPATH, PATH from config.
    Returns (sdk_root, python_x64_exe).
    """
    sdk = cfg.get("qairt_sdk_root", "") or os.environ.get("QAIRT_SDK_ROOT", "")
    if sdk and not os.environ.get("QAIRT_SDK_ROOT"):
        os.environ["QAIRT_SDK_ROOT"] = sdk

    if sdk and not os.path.isdir(sdk):
        print(f"[ERROR] QAIRT SDK root does not exist: {sdk}")
        print("  This path comes from qairt_env.json (qairt_sdk_root) or the")
        print("  QAIRT_SDK_ROOT env var. It likely points at an old SDK version")
        print("  that has since been upgraded/removed.")
        print("  Fix: re-run Setup.bat to refresh")
        print("  data\\config\\qairt_env.json with the installed SDK path.")
        sys.exit(2)

    if sdk:
        if _detect_platform() == "windows":
            pylib = str(Path(sdk) / "lib" / "python")
            pypath_entries = _path_list("PYTHONPATH")
            if pylib not in pypath_entries:
                pypath_entries.insert(0, pylib)
                os.environ["PYTHONPATH"] = os.pathsep.join(pypath_entries)

            aarch64_lib = str(Path(sdk) / "lib" / "aarch64-windows-msvc")
            path_entries = _path_list("PATH")
            if aarch64_lib not in path_entries:
                path_entries.insert(0, aarch64_lib)
                os.environ["PATH"] = os.pathsep.join(path_entries)
        else:
            arch_dir = _get_host_toolchain_dir(sdk)
            pylib = str(Path(sdk) / "lib" / "python")
            if Path(pylib).is_dir():
                pypath_entries = _path_list("PYTHONPATH")
                if pylib not in pypath_entries:
                    pypath_entries.insert(0, pylib)
                    os.environ["PYTHONPATH"] = os.pathsep.join(pypath_entries)
            ldlib = str(Path(sdk) / "lib" / arch_dir)
            if Path(ldlib).is_dir():
                ldpath_entries = _path_list("LD_LIBRARY_PATH")
                if ldlib not in ldpath_entries:
                    ldpath_entries.insert(0, ldlib)
                    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(ldpath_entries)
            bindir = str(Path(sdk) / "bin" / arch_dir)
            if Path(bindir).is_dir():
                path_entries = _path_list("PATH")
                if bindir not in path_entries:
                    path_entries.insert(0, bindir)
                    os.environ["PATH"] = os.pathsep.join(path_entries)

    # Resolve x64 Python executable (platform-aware venv layout)
    venv = cfg.get("python_x64_venv", "")
    if venv:
        if sys.platform == "win32":
            python_x64 = str(Path(venv) / "Scripts" / "python.exe")
        else:
            python_x64 = str(Path(venv) / "bin" / "python")
    else:
        python_x64 = ""
    if not (python_x64 and Path(python_x64).exists()):
        if python_x64:
            print(f"[WARNING] python_x64_venv not found: {python_x64}")
        else:
            print("[WARNING] python_x64_venv not set in qairt_env.json")
        print(f"[WARNING] Falling back to current interpreter: {sys.executable}")
        python_x64 = sys.executable

    return sdk, python_x64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_input_dim(input_dim: str) -> tuple:
    """Parse 'name dims' or 'name,dims' into (name_part, dim_part)."""
    s = input_dim.strip()
    if " " in s:
        name_part, dim_part = s.split(" ", 1)
    else:
        parts = s.split(",", 1)
        name_part = parts[0]
        dim_part = parts[1] if len(parts) == 2 else "1"
    return name_part, dim_part


def _check_calib_list_format(calib_list: str) -> None:
    """qairt-quantizer expects raw paths (no ``input:=`` prefix).

    The legacy qnn-onnx-converter used ``input_name:=path`` format inside
    calibration list files. Passing such a file to qairt-quantizer causes
    silent misbehaviour (file not found, or wrong tensor binding). Warn the
    user explicitly so they can strip the prefix.
    """
    try:
        with open(calib_list, encoding="utf-8") as f:
            first_line = ""
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    first_line = s
                    break
    except OSError:
        return
    if ":=" in first_line:
        print("[WARN] Calibration list appears to use the legacy 'input_name:=path'")
        print("       prefix format (from qnn-onnx-converter). qairt-quantizer")
        print("       expects raw paths. Strip the 'name:=' prefix or use a")
        print("       plain list of file paths.")


def _htp_version_to_target_soc_model(htp_version: str) -> str:
    """Map --htp_version {v73,v81} to qairt-converter/quantizer --target_soc_model.

    Values follow the QAIRT 2.48 identifier convention:
      * v73 (HTP v73) -> SC8380XP -- X Elite
      * v81 (HTP v81) -> SM8750    -- X2 Elite / Snapdragon 8 Elite
    Returns "" if the mapping is unknown (caller should skip --target_soc_model).
    """
    return {
        "v73": "SC8380XP",
        "v81": "SM8750",
    }.get(htp_version.lower(), "")


# ---------------------------------------------------------------------------
# Step 1: qairt-converter (ONNX -> FP DLC)
# ---------------------------------------------------------------------------

def step1_convert(python: str, sdk: str, host_arch: str,
                  onnx: str, output_dir: str,
                  precision: str,
                  input_dim: str,
                  no_simplification: bool,
                  io_config: str,
                  quant_overrides: str,
                  export_strip_quant: bool,
                  target_soc_model: str) -> str:
    """ONNX -> FP DLC. Returns dlc_path.

    ``precision`` here determines the *float* representation stored in the DLC
    (fp32 / fp16 / bf16). For quantized outputs the converter still emits a
    FP DLC first (usually fp32); the quantizer stage then converts it to the
    requested quantized precision.
    """
    print(f"\n[STEP 1] ONNX -> DLC  (precision={precision})")

    ext = ".exe" if sys.platform == "win32" else ""
    converter = str(Path(sdk) / "bin" / host_arch / f"qairt-converter{ext}")
    if not Path(converter).exists():
        # QAIRT 2.48+: bare Python launcher (no .exe)
        alt = str(Path(sdk) / "bin" / host_arch / "qairt-converter")
        if Path(alt).exists():
            converter = alt
    if not Path(converter).exists():
        print(f"[ERROR] qairt-converter not found: {converter}")
        print(f"  Checked host toolchain: {host_arch}")
        print("  Verify QAIRT_SDK_ROOT points to a complete SDK installation.")
        sys.exit(1)

    model_name = Path(onnx).stem
    dlc_out = str(Path(output_dir) / f"{model_name}.dlc")

    cmd = [python, converter,
           "--input_network", onnx,
           "--output_path", dlc_out]

    # Float bitwidth mapping: fp32 -> 32, fp16 -> 16, bf16 -> bf16.
    # For quantized precisions the converter defaults to fp32 (safe intermediate).
    if precision == "fp16":
        cmd += ["--float_bitwidth", "16"]
    elif precision == "bf16":
        cmd += ["--float_bitwidth", "bf16"]
    elif precision == "fp32":
        cmd += ["--float_bitwidth", "32"]
    # For quantized paths: leave --float_bitwidth default; quantizer handles precision.

    if input_dim:
        name_part, dim_part = _parse_input_dim(input_dim)
        # qairt-converter uses --source_model_input_shape NAME DIMS (two args).
        cmd += ["--source_model_input_shape", name_part, dim_part]

    if no_simplification:
        # Legacy --no_simplification maps to qairt-converter --onnx_skip_simplification.
        cmd.append("--onnx_skip_simplification")

    if io_config:
        cmd += ["--config", io_config]

    if quant_overrides:
        cmd += ["--quantization_overrides", quant_overrides]

    if export_strip_quant:
        # DLC_STRIP_QUANT produces an additional FP-only DLC alongside the quantized one.
        # Used for CPU/GPU baseline comparison during accuracy debugging.
        cmd += ["--export_format", "DLC_STRIP_QUANT"]

    if target_soc_model:
        # SoC-specific graph optimisation. Omitted by default (cross-platform DLC).
        # Only added when the user opted in with --soc_optimized.
        # (cross-device deployment).
        cmd += ["--target_backend", "HTP", "--target_soc_model", target_soc_model]

    print(f"[CMD] {shlex.join(cmd)}")
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(f"[ERROR] qairt-converter failed (exit {r.returncode})")
        sys.exit(r.returncode)

    if not Path(dlc_out).exists():
        print(f"[ERROR] Expected DLC not found: {dlc_out}")
        sys.exit(1)

    print(f"[OK] {dlc_out}")
    return dlc_out


# ---------------------------------------------------------------------------
# Step 2: qairt-quantizer (FP DLC -> quantized DLC, optional)
# ---------------------------------------------------------------------------

def step2_quantize(python: str, sdk: str, host_arch: str,
                   dlc: str, output_dir: str, calib_list: str,
                   act_bw: int, weight_bw: int, bias_bw,
                   precision_label: str,
                   per_channel: bool, per_row: bool,
                   calib_method: str, percentile_value,
                   cle: bool,
                   dump_encoding: bool,
                   target_soc_model: str) -> str:
    """FP DLC -> quantized DLC. Returns quantized_dlc_path."""
    print(f"\n[STEP 2] qairt-quantizer  (act_bw={act_bw}, weight_bw={weight_bw})")

    ext = ".exe" if sys.platform == "win32" else ""
    quantizer = str(Path(sdk) / "bin" / host_arch / f"qairt-quantizer{ext}")
    if not Path(quantizer).exists():
        # QAIRT 2.48+: bare Python launcher (no .exe)
        alt = str(Path(sdk) / "bin" / host_arch / "qairt-quantizer")
        if Path(alt).exists():
            quantizer = alt
    if not Path(quantizer).exists():
        print(f"[ERROR] qairt-quantizer not found: {quantizer}")
        sys.exit(1)

    model_name = Path(dlc).stem
    qdlc_out = str(Path(output_dir) / f"{model_name}_{precision_label}.dlc")

    cmd = [python, quantizer,
           "--input_dlc", dlc,
           "--output_dlc", qdlc_out,
           "--input_list", calib_list,
           "--act_bitwidth", str(act_bw),
           "--weights_bitwidth", str(weight_bw)]

    # ---- bias width: preserve legacy semantics ----
    # Legacy pipeline: --bias_bw unspecified => bias stays FP32.
    # qairt-quantizer default: --bias_bitwidth unspecified => INT8.
    # To keep legacy behaviour, we auto-add --float_bias_bitwidth 32 when the
    # user did NOT explicitly pass --bias_bw. When they did, pass through.
    if bias_bw is not None:
        cmd += ["--bias_bitwidth", str(bias_bw)]
    else:
        cmd += ["--float_bias_bitwidth", "32"]

    # ---- 16-bit paths need --restrict_quantization_steps ----
    if _needs_restrict_steps(act_bw, weight_bw):
        # int16 range in hex, symmetric.
        cmd += ["--restrict_quantization_steps", "-0x8000 0x7FFF"]

    # ---- Structural quantization switches ----
    if per_channel:
        cmd.append("--use_per_channel_quantization")
    if per_row:
        cmd.append("--use_per_row_quantization")

    # ---- Calibration method ----
    if calib_method and calib_method != "min-max":
        cmd += ["--act_quantizer_calibration", calib_method,
                "--param_quantizer_calibration", calib_method]
        if calib_method == "percentile" and percentile_value is not None:
            cmd += ["--percentile_calibration_value", str(percentile_value)]

    # ---- CLE ----
    if cle:
        cmd += ["--algorithms", "cle"]

    # ---- Encoding dump (diagnostic) ----
    if dump_encoding:
        cmd.append("--dump_encoding_json")

    # ---- SoC-specific optimisation ----
    if target_soc_model:
        cmd += ["--target_backend", "HTP", "--target_soc_model", target_soc_model]

    print(f"[CMD] {shlex.join(cmd)}")
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(f"[ERROR] qairt-quantizer failed (exit {r.returncode})")
        sys.exit(r.returncode)

    if not Path(qdlc_out).exists():
        print(f"[ERROR] Expected quantized DLC not found: {qdlc_out}")
        sys.exit(1)

    print(f"[OK] {qdlc_out}")
    return qdlc_out


# ---------------------------------------------------------------------------
# Step 3: qai_dev_gen_contextbin.py (DLC -> .bin)
# ---------------------------------------------------------------------------

def step3_context_binary(python: str, script_dir: str, sdk: str,
                         dlc: str, output_dir: str, bin_name: str,
                         config_file: str, htp_version: str,
                         profiling: bool) -> str:
    """DLC -> context binary (.bin). Returns bin_path."""
    print(f"\n[STEP 3] DLC -> context binary (HTP {htp_version})")

    ctx_script = str(Path(script_dir) / "qai_dev_gen_contextbin.py")
    if not Path(ctx_script).exists():
        print(f"[ERROR] qai_dev_gen_contextbin.py not found: {ctx_script}")
        sys.exit(1)

    cmd = [python, ctx_script,
           "--model", dlc,
           "--output_dir", output_dir,
           "--binary_file", bin_name,
           "--htp_version", htp_version]

    # DLC mode: qai_dev_gen_contextbin.py internally uses --soc_model (not
    # --config_file, because DLCs have unpredictable graph_names). We still
    # honour user-supplied --config for HTP backend extensions (rare but valid).
    if config_file and Path(config_file).exists():
        cmd += ["--config_file", config_file]
        print(f"[INFO] Backend config: {config_file}")

    if profiling:
        cmd.append("--profiling")

    print(f"[CMD] {shlex.join(cmd)}")
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(
            f"[ERROR] context binary generation failed (exit {r.returncode}). "
            "Check the QAIRT SDK: a 0-byte/corrupt qnn-context-binary-generator.exe "
            "is usually overwritten by a stray write (see SKILL.md B9), not a "
            "shipping defect. Diagnose READ-ONLY; restore that one exe or reinstall."
        )
        sys.exit(r.returncode)

    # Locate the generated .bin
    expected = Path(output_dir) / f"{bin_name}.bin"
    if expected.exists() and expected.stat().st_size > 1024:
        print(f"[OK] {expected}")
        return str(expected)

    # Fallback: recursive search for a non-trivial .bin
    candidates = []
    for root, _, filenames in os.walk(output_dir):
        for f in filenames:
            if not f.endswith(".bin"):
                continue
            full = Path(root) / f
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > 1024:
                candidates.append(full)
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    if candidates:
        best = candidates[0]
        print(f"[OK] {best}")
        return str(best)

    print("[ERROR] No valid context binary (.bin) produced.")
    print(f"  Expected: {expected}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="End-to-End QNN DLC Pipeline: ONNX -> DLC -> .bin (QAIRT 2.45+)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Legacy-compatible flags ---
    p.add_argument("--model", required=True,
                   help="Path to input ONNX model")
    p.add_argument("--output", default="qairt_output",
                   help="Output directory (default: qairt_output)")
    p.add_argument("--precision", default=None,
                   choices=list(_PRECISION_TABLE.keys()),
                   help="Precision preset. Default: fp16. "
                        "Mutually exclusive with --act_bw / --weight_bw / --bias_bw.")
    p.add_argument("--act_bw", type=int, default=None,
                   help="Custom activation bitwidth (4/8/16). Requires --weight_bw.")
    p.add_argument("--weight_bw", type=int, default=None,
                   help="Custom weight bitwidth (4/8/16). Requires --act_bw.")
    p.add_argument("--bias_bw", type=int, default=None,
                   help="Custom bias bitwidth (8/32). "
                        "Unspecified => bias stays FP32 (legacy semantics).")
    p.add_argument("--calib_list", default="",
                   help="Calibration list file (required for quantized precisions).")
    p.add_argument("--input_dim", default="",
                   help='Input dimensions, e.g. "input 1,3,512,512".')
    p.add_argument("--config", default="",
                   help="Path to HTP backend_extensions.json (optional).")
    p.add_argument("--htp_version", default="v73", choices=["v73", "v81"],
                   help="HTP version for context binary (default: v73).")
    p.add_argument("--skip_contextbin", action="store_true",
                   help="Skip context binary generation (produce DLC only).")
    p.add_argument("--no_simplification", action="store_true",
                   help="Skip ONNX simplification "
                        "(maps to qairt-converter --onnx_skip_simplification).")

    # --- New: IO / config ---
    p.add_argument("--io_config", default="",
                   help="qairt-converter YAML I/O config file "
                        "(layout / dtype / color-encoding per tensor).")

    # --- New: quantization overrides ---
    p.add_argument("--quant_overrides", default="",
                   help="AIMET-style JSON encoding overrides "
                        "(--quantization_overrides). Manual mixed precision / "
                        "layer protection.")

    # --- New: dual FP DLC export ---
    p.add_argument("--strip_quant", action="store_true",
                   help="Also emit a stripped FP DLC (--export_format DLC_STRIP_QUANT). "
                        "Useful for CPU/GPU accuracy baseline.")

    # --- New: structural quantization ---
    p.add_argument("--per_channel", action="store_true",
                   help="Enable per-channel weight quantization (Conv). "
                        "Strongly recommended for W8A8.")
    p.add_argument("--per_row", action="store_true",
                   help="Enable per-row weight quantization (Matmul/FC).")

    # --- New: calibration method ---
    p.add_argument("--calib_method", default="min-max",
                   choices=["min-max", "sqnr", "entropy", "mse", "percentile"],
                   help="Calibration method (default: min-max).")
    p.add_argument("--percentile_value", type=float, default=None,
                   help="Percentile value (90-100) when --calib_method=percentile.")

    # --- New: CLE ---
    p.add_argument("--cle", action="store_true",
                   help="Apply Cross-Layer Equalization "
                        "(qairt-quantizer --algorithms cle). "
                        "Recommended when W8A8 accuracy drops below threshold.")

    # --- New: diagnostic ---
    p.add_argument("--dump_encoding", action="store_true",
                   help="Dump per-tensor quantization encodings to JSON "
                        "(diagnostic; useful for B6 accuracy debug).")

    # --- New: SoC-specific optimisation (default OFF = cross-platform DLC) ---
    p.add_argument("--soc_optimized", action="store_true",
                   help="Enable SoC-specific graph optimisation "
                        "(passes --target_backend HTP --target_soc_model <soc>). "
                        "When OFF (default), the DLC is CROSS-PLATFORM: the same DLC "
                        "can generate .bin for both HTP v73 and v81. "
                        "Enable ONLY when the user explicitly asked for optimisation "
                        "toward one specific SoC.")
    # Legacy alias: keep --generic_graph accepted but as a no-op (cross-platform is
    # now the default). If the user passes both, --soc_optimized wins.
    p.add_argument("--generic_graph", action="store_true",
                   help=argparse.SUPPRESS)

    # --- New: profiling ---
    p.add_argument("--profiling", action="store_true",
                   help="Enable HTP optrace profiling during context binary generation.")

    return p


def main():
    parser = _build_argparser()
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 1. Resolve precision mode
    # ------------------------------------------------------------------
    custom_bw = (args.act_bw is not None) or (args.weight_bw is not None)
    preset = args.precision

    if custom_bw and preset is not None:
        print("[ERROR] --precision and --act_bw/--weight_bw are mutually exclusive.")
        sys.exit(1)

    if custom_bw:
        if args.act_bw is None or args.weight_bw is None:
            print("[ERROR] --act_bw and --weight_bw must both be specified for custom mode.")
            sys.exit(1)
        act_bw = args.act_bw
        weight_bw = args.weight_bw
        bias_bw = args.bias_bw
        precision_label = f"w{weight_bw}a{act_bw}" + (f"b{bias_bw}" if bias_bw else "")
        is_float = False
    else:
        if preset is None:
            preset = "fp16"
        act_bw, weight_bw, bias_bw_default = _PRECISION_TABLE[preset]
        # For preset mode, --bias_bw override wins if user set it.
        bias_bw = args.bias_bw if args.bias_bw is not None else bias_bw_default
        precision_label = preset
        is_float = _is_float_precision(preset)

    # ------------------------------------------------------------------
    # 2. Discover config and set up env
    # ------------------------------------------------------------------
    script_dir = Path(__file__).resolve().parent
    cfg = find_qairt_env(script_dir)
    if not cfg:
        print("[ERROR] 未找到 QAIRT 配置文件 data/config/qairt_env.json。")
        print("[ERROR] 该文件由 Setup.bat 的 Step 8.6 生成。请重新运行 Setup.bat,")
        print("[ERROR] 或仅生成配置(无需重下 SDK):")
        print("[ERROR]   <.venv_x64_310>\\Scripts\\python.exe "
              "scripts\\setup\\setup_qairt_env.py --gen-config")
        sys.exit(1)

    sdk, python_x64 = apply_qairt_env(cfg)
    if not sdk:
        print("[ERROR] QAIRT_SDK_ROOT not found in config or environment.")
        sys.exit(1)

    host_arch = _get_host_toolchain_dir(sdk)
    print(f"[INFO] SDK          = {sdk}")
    print(f"[INFO] Host arch    = {host_arch}")
    print(f"[INFO] Python (x64) = {python_x64}")

    # ------------------------------------------------------------------
    # 3. Validate inputs
    # ------------------------------------------------------------------
    onnx = str(Path(args.model).resolve())
    if not Path(onnx).exists():
        print(f"[ERROR] Model not found: {onnx}")
        sys.exit(1)

    if not is_float:
        if not args.calib_list:
            print(f"[ERROR] --calib_list required for quantized precision ({precision_label})")
            sys.exit(1)
        if not Path(args.calib_list).exists():
            print(f"[ERROR] Calibration list not found: {args.calib_list}")
            sys.exit(1)
        _check_calib_list_format(args.calib_list)

    if args.calib_method == "percentile" and args.percentile_value is None:
        args.percentile_value = 99.99  # QAIRT default

    output_dir = str(Path(args.output).resolve())
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    model_name = Path(onnx).stem

    # ------------------------------------------------------------------
    # 4. Resolve SoC target
    # ------------------------------------------------------------------
    # Default = cross-platform DLC (no --target_soc_model). Only when the user
    # explicitly asks for SoC-specific optimisation via --soc_optimized do we
    # inject --target_backend HTP --target_soc_model <id>.
    #
    # --generic_graph is a legacy alias (now a no-op) kept so existing scripts
    # don't break. It maps to the default cross-platform behaviour.
    if args.soc_optimized and args.generic_graph:
        print("[WARN] --soc_optimized and --generic_graph both given; "
              "--soc_optimized wins (SoC-specific graph will be built).")

    if args.soc_optimized:
        target_soc_model = _htp_version_to_target_soc_model(args.htp_version)
        if not target_soc_model:
            print(f"[ERROR] --soc_optimized set but htp_version={args.htp_version} "
                  "has no known SoC mapping.")
            sys.exit(1)
    else:
        target_soc_model = ""

    print(f"\n{'='*60}")
    print(f"  QNN DLC Pipeline: {model_name} [{precision_label}]")
    print(f"  Output    : {output_dir}")
    if args.soc_optimized:
        print(f"  HTP       : {args.htp_version}  (SoC-optimized: {target_soc_model})")
    else:
        print(f"  HTP       : {args.htp_version}  (cross-platform DLC — default)")
    print(f"{'='*60}")

    # ------------------------------------------------------------------
    # 5. Step 1 -- ONNX -> DLC
    # ------------------------------------------------------------------
    # For quantized precisions, the converter emits fp32 DLC; the quantizer
    # then produces the requested quantized DLC. Pass fp32 to keep things
    # unambiguous. For float precisions, pass the precision through directly.
    converter_precision = precision_label if is_float else "fp32"

    dlc = step1_convert(
        python=python_x64,
        sdk=sdk,
        host_arch=host_arch,
        onnx=onnx,
        output_dir=output_dir,
        precision=converter_precision,
        input_dim=args.input_dim,
        no_simplification=args.no_simplification,
        io_config=args.io_config,
        quant_overrides=args.quant_overrides,
        export_strip_quant=args.strip_quant,
        target_soc_model=target_soc_model,
    )

    # ------------------------------------------------------------------
    # 6. Step 2 -- quantize (optional)
    # ------------------------------------------------------------------
    if not is_float:
        dlc = step2_quantize(
            python=python_x64,
            sdk=sdk,
            host_arch=host_arch,
            dlc=dlc,
            output_dir=output_dir,
            calib_list=args.calib_list,
            act_bw=act_bw,
            weight_bw=weight_bw,
            bias_bw=bias_bw,
            precision_label=precision_label,
            per_channel=args.per_channel,
            per_row=args.per_row,
            calib_method=args.calib_method,
            percentile_value=args.percentile_value,
            cle=args.cle,
            dump_encoding=args.dump_encoding,
            target_soc_model=target_soc_model,
        )
    else:
        # Sanity-check: several quantizer-only flags are meaningless for FP paths.
        _fp_only_warn = []
        if args.cle:              _fp_only_warn.append("--cle")
        if args.per_channel:      _fp_only_warn.append("--per_channel")
        if args.per_row:          _fp_only_warn.append("--per_row")
        if args.dump_encoding:    _fp_only_warn.append("--dump_encoding")
        if args.calib_method != "min-max":
            _fp_only_warn.append(f"--calib_method={args.calib_method}")
        if _fp_only_warn:
            print(f"[WARN] The following flags are ignored for float precision "
                  f"({precision_label}): {', '.join(_fp_only_warn)}")

    # ------------------------------------------------------------------
    # 7. Step 3 -- context binary
    # ------------------------------------------------------------------
    bin_name = f"{model_name}_{precision_label}"
    ctx_bin = None
    if args.skip_contextbin:
        print("\n[SKIP] Context binary generation skipped")
    else:
        ctx_bin = step3_context_binary(
            python=python_x64,
            script_dir=str(script_dir),
            sdk=sdk,
            dlc=dlc,
            output_dir=output_dir,
            bin_name=bin_name,
            config_file=args.config,
            htp_version=args.htp_version,
            profiling=args.profiling,
        )

    # ------------------------------------------------------------------
    # 8. Report
    # ------------------------------------------------------------------
    _ONE_MB = 1 * 1024 * 1024
    print(f"\n{'='*60}")
    print(f"  Pipeline complete: {model_name} [{precision_label}]")
    print(f"  Output   : {output_dir}")
    print(f"  DLC      : {dlc}")
    if ctx_bin:
        size = Path(ctx_bin).stat().st_size
        if size >= _ONE_MB:
            print(f"  Binary   : {ctx_bin}  ({size:,} bytes)")
        else:
            print(f"  [WARN] Binary exists but may be invalid (size={size} bytes): {ctx_bin}")
    elif args.skip_contextbin:
        print(f"  Binary   : (skipped, DLC-only)")
    print(f"{'='*60}")


if __name__ == "__main__":
    # Ensure Unicode-safe stdout/stderr on Windows terminals.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
