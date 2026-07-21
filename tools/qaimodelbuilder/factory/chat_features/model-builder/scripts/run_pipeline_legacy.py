"""
run_pipeline_legacy.py - LEGACY End-to-End QNN Pipeline (ONNX -> C++/BIN -> DLL -> .bin)

>>> LEGACY / SECONDARY PATH <<<

This is the OLD DLL-based pipeline. The default entry point is now
``run_pipeline.py`` which runs the newer DLC-based path
(``ONNX -> DLC -> .bin`` via qairt-converter + qairt-quantizer +
qai_dev_gen_contextbin.py --model <file>.dlc).

When to use THIS legacy script:
  * The user explicitly asks for the ``ONNX -> C++/BIN -> DLL -> .bin``
    path (or the ``.dll`` inference artifact directly).
  * A regression is suspected in the new pipeline and you need to reproduce
    the older behaviour for A/B comparison.
  * You need a compiled ARM64 ``.dll`` (the .dll is only produced on this
    path; the new DLC path never emits one).

When to use the DEFAULT ``run_pipeline.py`` instead:
  * All other cases (fresh conversions, quantized deployments, CLE fixes,
    cross-SoC .bin, LLM/generation models). The new path is the strategic
    direction and will eventually supersede this legacy one.

Single-file pipeline: no .bat wrapper needed.
Handles VS ARM64 environment initialization internally via subprocess.

Steps:
  1. ONNX -> C++/bin  (x86_64-windows-msvc/qnn-onnx-converter)
  2. C++/bin -> DLL   (aarch64-windows-msvc/qnn-model-lib-generator)
  3. DLL -> .bin      (qai_dev_gen_contextbin.py -> qnn-context-binary-generator)

Usage:
  python run_pipeline_legacy.py --model model.onnx --output output --precision fp16
  python run_pipeline_legacy.py --model model.onnx --output output --precision int8 ^
      --calib_list calib.txt
  python run_pipeline_legacy.py --model model.onnx --output output --precision fp16 ^
      --skip_contextbin

Arguments:
  --model            Path to input ONNX model (required)
  --output           Output directory (default: qairt_output)
  --precision        fp16 | fp32 | w8a16 | w8a8 | w8a8b8 | w4a16 | w4a8  (default: fp16)
                     Mutually exclusive with --act_bw / --weight_bw / --bias_bw.
  --act_bw           Custom activation bitwidth (e.g. 4/8/16). Requires --weight_bw.
  --weight_bw        Custom weight bitwidth (e.g. 4/8). Requires --act_bw.
  --bias_bw          Custom bias bitwidth (e.g. 8). Optional, only valid with --act_bw/--weight_bw.
  --calib_list       Calibration list file (required for all quantized precisions)
  --input_dim        Input dimensions, e.g. "input 1,3,512,512" (optional)
  --config           Path to backend_extensions.json (optional, auto-generated if omitted)
  --skip_contextbin  Skip context binary generation
  --no_simplification  Pass --no_simplification to qnn-onnx-converter (recommended for WoS)
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
# Platform / arch detection
# ---------------------------------------------------------------------------

def _detect_platform() -> str:
    """Return 'windows', 'linux', or 'unsupported'."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _detect_arch() -> str:
    """Return 'x86_64', 'aarch64', or 'unknown'."""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return "unknown"


def _get_linux_arch_dir() -> str:
    """Return the DEFAULT arch-specific SDK subdirectory name (no probing).

    Used only where the SDK root is not available for existence probing (e.g.
    building a first-guess lib/ path). Prefer ``_get_linux_toolchain_dir(sdk)``
    whenever the SDK root is known — it probes for the variant that actually
    exists on disk.
    """
    if _detect_arch() == "aarch64":
        return "aarch64-oe-linux-gcc11.2"
    return "x86_64-linux-clang"


def _get_linux_toolchain_dir(sdk: str) -> str:
    """Return the first SDK bin/ subdirectory that contains qairt-converter.

    Probe the known gcc variants in priority order rather than hardcoding one:
    a real Ubuntu QAIRT SDK may ship gcc9.4 / gcc9.3 / gcc8.2 instead of the
    default gcc11.2, and hardcoding gcc11.2 would point PATH/converter at a
    non-existent directory (silent [WARNING] then converter launch failure).
    """
    if _detect_arch() == "aarch64":
        candidates = [
            "aarch64-oe-linux-gcc11.2",
            "aarch64-ubuntu-gcc9.4",
            "aarch64-oe-linux-gcc9.3",
            "aarch64-oe-linux-gcc8.2",
        ]
    else:
        candidates = ["x86_64-linux-clang"]
    for d in candidates:
        if Path(sdk, "bin", d, "qairt-converter").exists():
            return d
    return candidates[0]  # fallback; caller reports missing tool


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
# VS ARM64 environment initialisation
# ---------------------------------------------------------------------------

def init_vs_arm64(vcvarsall: str, vc_targets_path: str) -> None:
    """
    Run vcvarsall.bat arm64 in a cmd subprocess, capture the resulting
    environment variables, and merge them into the current process env.

    This replaces the need for a .bat wrapper entirely.

    Hard gate: if vcvarsall.bat is missing or the resulting environment does
    not have VSCMD_ARG_TGT_ARCH=arm64, print [ERROR] and sys.exit(1).
    Without a valid VS ARM64 env, Step 2 (DLL compilation) and Step 3
    (context binary generation) will both fail.
    """
    # Bug 2 fix: missing vcvarsall is fatal, not a warning-and-continue.
    if not vcvarsall or not Path(vcvarsall).exists():
        print(f"[ERROR] vcvarsall.bat not found: {vcvarsall!r}")
        print("[ERROR] VS ARM64 environment cannot be initialized.")
        print("[ERROR] Install Visual Studio 2022 Community (not BuildTools) and")
        print("[ERROR] re-run Setup.bat to update data/config/qairt_env.json.")
        sys.exit(1)

    print(f"[INFO] Initializing VS ARM64 env from: {vcvarsall}")

    # Bug 1 fix: quote the vcvarsall path separately so spaces in
    # "C:\Program Files\..." are handled correctly by cmd.exe.
    # Correct shell structure: cmd /c ""<path>" arm64 >nul 2>&1 && set"
    quoted_vcvarsall = '"' + vcvarsall + '"'
    cmd = 'cmd /c "' + quoted_vcvarsall + ' arm64 >nul 2>&1 && set"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[WARNING] vcvarsall.bat returned {result.returncode} (may still be OK)")

    # Merge captured vars into current process environment
    count = 0
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            os.environ[k] = v
            count += 1

    # Override VCTargetsPath to ensure VS 2022 Community (not BuildTools)
    if vc_targets_path:
        os.environ["VCTargetsPath"] = vc_targets_path

    # Hard gate: verify the environment was actually initialised for arm64.
    actual_arch = os.environ.get("VSCMD_ARG_TGT_ARCH", "")
    if actual_arch.lower() != "arm64":
        print(f"[ERROR] VS ARM64 env init FAILED -- "
              f"VSCMD_ARG_TGT_ARCH={actual_arch!r} (expected 'arm64')")
        print(f"[ERROR] vcvarsall.bat merged {count} vars but did not set target arch to arm64.")
        print("[ERROR] Ensure vs_vcvarsall in qairt_env.json points to VS 2022 Community")
        print("[ERROR] (not BuildTools) and that the ARM64 workload is installed.")
        sys.exit(1)

    print(f"[INFO] VS ARM64 env ready ({count} vars merged, arch=arm64)")


# ---------------------------------------------------------------------------
# Apply QAIRT SDK paths
# ---------------------------------------------------------------------------

def _path_list(env_var: str) -> list:
    """Split an env var (PATH / PYTHONPATH) into a list of entries."""
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

    # State-Truth-First: the configured SDK root must actually exist on disk.
    # qairt_env.json pins a versioned path (e.g. .../QAIRT/2.45.x); after an SDK
    # upgrade that was not followed by re-running Setup.bat, the
    # pinned path is stale and every downstream tool path is wrong. Fail FAST
    # with an actionable message (config vs reality) instead of letting each
    # tool fail obscurely later. We validate the directory exists rather than
    # guessing version strings — the truth is whether the path resolves.
    if sdk and not os.path.isdir(sdk):
        print(f"[ERROR] QAIRT SDK root does not exist: {sdk}")
        print("  This path comes from qairt_env.json (qairt_sdk_root) or the")
        print("  QAIRT_SDK_ROOT env var. It likely points at an old SDK version")
        print("  that has since been upgraded/removed.")
        print("  Fix: re-run Setup.bat to refresh")
        print("  data\\config\\qairt_env.json with the installed SDK path.")
        sys.exit(2)

    if sdk:
        # Bug 3 fix: use exact element matching instead of substring check
        # to avoid false positives when one path is a prefix of another.

        if _detect_platform() == "windows":
            # PYTHONPATH: QAIRT Python bindings
            pylib = str(Path(sdk) / "lib" / "python")
            pypath_entries = _path_list("PYTHONPATH")
            if pylib not in pypath_entries:
                pypath_entries.insert(0, pylib)
                os.environ["PYTHONPATH"] = os.pathsep.join(pypath_entries)

            # PATH: aarch64 DLLs needed by qnn-model-lib-generator and context binary generator
            # On WoS, use aarch64-windows-msvc for both generator and lib
            aarch64_lib = str(Path(sdk) / "lib" / "aarch64-windows-msvc")
            path_entries = _path_list("PATH")
            if aarch64_lib not in path_entries:
                path_entries.insert(0, aarch64_lib)
                os.environ["PATH"] = os.pathsep.join(path_entries)
        else:
            # Linux: equivalent to `source $QAIRT_SDK_ROOT/bin/envsetup.sh`
            # Probe the SDK for the gcc variant that actually exists so
            # LD_LIBRARY_PATH / PATH point at real directories (not a hardcoded
            # gcc11.2 that may be absent on this SDK).
            arch_dir = _get_linux_toolchain_dir(sdk)

            # 1. PYTHONPATH: qti / snpe Python modules (same path for x86_64 and aarch64)
            pylib = str(Path(sdk) / "lib" / "python")
            if Path(pylib).is_dir():
                pypath_entries = _path_list("PYTHONPATH")
                if pylib not in pypath_entries:
                    pypath_entries.insert(0, pylib)
                    os.environ["PYTHONPATH"] = os.pathsep.join(pypath_entries)
            else:
                print(f"[WARNING] QAIRT lib/python not found: {pylib}")

            # 2. LD_LIBRARY_PATH: QNN runtime .so libraries
            ldlib = str(Path(sdk) / "lib" / arch_dir)
            if Path(ldlib).is_dir():
                ldpath_entries = _path_list("LD_LIBRARY_PATH")
                if ldlib not in ldpath_entries:
                    ldpath_entries.insert(0, ldlib)
                    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(ldpath_entries)
            else:
                print(f"[WARNING] QAIRT lib/{arch_dir} not found: {ldlib}")

            # 3. PATH: QAIRT tool binaries
            bindir = str(Path(sdk) / "bin" / arch_dir)
            if Path(bindir).is_dir():
                path_entries = _path_list("PATH")
                if bindir not in path_entries:
                    path_entries.insert(0, bindir)
                    os.environ["PATH"] = os.pathsep.join(path_entries)
            else:
                print(f"[WARNING] QAIRT bin/{arch_dir} not found: {bindir}")

    # Resolve x64 Python executable (platform-aware venv layout)
    venv = cfg.get("python_x64_venv", "")
    if venv:
        if sys.platform == "win32":
            python_x64 = str(Path(venv) / "Scripts" / "python.exe")
        else:
            python_x64 = str(Path(venv) / "bin" / "python")
    else:
        python_x64 = ""
    if python_x64 and Path(python_x64).exists():
        pass  # resolved correctly
    else:
        # Bug 6 fix: warn explicitly instead of silently falling back to the
        # current interpreter (which may be ARM64 3.13, wrong for conversion).
        if python_x64:
            print(f"[WARNING] python_x64_venv not found: {python_x64}")
        else:
            print("[WARNING] python_x64_venv not set in qairt_env.json")
        print(f"[WARNING] Falling back to current interpreter: {sys.executable}")
        print("[WARNING] This may be wrong if the current Python is not x64 3.10.")
        python_x64 = sys.executable

    return sdk, python_x64


# ---------------------------------------------------------------------------
# Pipeline steps
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


def step1_convert_linux(python: str, sdk: str, onnx: str, output_dir: str,
                        input_dim: str) -> str:
    """ONNX -> DLC (Linux).  Returns dlc_path."""
    print("\n[STEP 1] ONNX -> DLC (Linux)")
    toolchain = _get_linux_toolchain_dir(sdk)
    converter = str(Path(sdk) / "bin" / toolchain / "qairt-converter")
    model_name = Path(onnx).stem
    dlc_out = str(Path(output_dir) / f"{model_name}.dlc")

    # Use the venv python explicitly so qairt-converter runs under the correct
    # Python version/site-packages, overriding its #!/usr/bin/env python3 shebang
    # which would otherwise pick up whatever python3 is first on PATH.
    cmd = [python, converter,
           "--input_network", onnx,
           "--output_path", dlc_out]
    if input_dim:
        name_part, dim_part = _parse_input_dim(input_dim)
        cmd += ["-d", name_part, dim_part]

    print(f"[CMD] {shlex.join(cmd)}")
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(f"[ERROR] ONNX→DLC conversion failed (exit {r.returncode})")
        sys.exit(r.returncode)

    if not Path(dlc_out).exists():
        print(f"[ERROR] Expected DLC not found: {dlc_out}")
        sys.exit(1)

    print(f"[OK] {dlc_out}")
    return dlc_out


def step2_quantize_linux(python: str, sdk: str, dlc: str, output_dir: str,
                         calib_list: str,
                         act_bw: int = 16, weight_bw: int = 8,
                         bias_bw=None) -> str:
    """DLC quantization (Linux, optional).  Returns quantized_dlc_path.

    On failure, logs [WARN] and returns the original unquantized DLC path
    so the pipeline can continue to Step 3.
    """
    print(f"\n[STEP 2] DLC quantization (Linux, act_bw={act_bw}, weight_bw={weight_bw})")
    toolchain = _get_linux_toolchain_dir(sdk)
    quantizer = str(Path(sdk) / "bin" / toolchain / "qairt-quantizer")
    model_name = Path(dlc).stem
    qdlc_out = str(Path(output_dir) / f"{model_name}_quantized.dlc")

    cmd = [python, quantizer,
           "--input_dlc", dlc,
           "--output_dlc", qdlc_out,
           "--input_list", calib_list,
           "--act_bitwidth", str(act_bw),
           "--weights_bitwidth", str(weight_bw)]
    if bias_bw is not None:
        cmd += ["--bias_bitwidth", str(bias_bw)]

    print(f"[CMD] {shlex.join(cmd)}")
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(f"[WARN] Quantization failed (exit {r.returncode}); using unquantized DLC")
        return dlc

    print(f"[OK] {qdlc_out}")
    return qdlc_out

def step1_convert(python: str, sdk: str, onnx: str, output_dir: str,
                  precision: str, act_bw, weight_bw, bias_bw,
                  calib_list: str, input_dim: str,
                  no_simplification: bool) -> tuple:
    """ONNX -> C++/bin.  Returns (cpp_path, bin_path)."""
    print(f"\n[STEP 1] ONNX -> C++/bin  ({precision})")
    converter = str(Path(sdk) / "bin" / "x86_64-windows-msvc" / "qnn-onnx-converter")
    model_name = Path(onnx).stem
    cpp_out = str(Path(output_dir) / f"{model_name}.cpp")
    bin_out = str(Path(output_dir) / f"{model_name}.bin")

    cmd = [python, converter,
           "--input_network", onnx,
           "--output_path", cpp_out,
           "--preserve_io"]

    if precision in ("fp16", "fp32"):
        cmd += ["--float_bitwidth", "16" if precision == "fp16" else "32"]
    else:
        cmd += ["--act_bitwidth", str(act_bw),
                "--weights_bitwidth", str(weight_bw),
                "--input_list", calib_list]
        if bias_bw:
            cmd += ["--bias_bitwidth", str(bias_bw)]

    if no_simplification:
        cmd.append("--no_simplification")
    if input_dim:
        # Parse input_dim into two args for -d
        # qnn-onnx-converter expects: -d INPUT_NAME INPUT_DIM (two separate args)
        # Supports two formats:
        #   "x 1,3,512,512"  (space between name and dims)
        #   "x,1,3,512,512"  (comma between name and dims)
        s = input_dim.strip()
        if " " in s:
            # space-separated: "x 1,3,512,512"
            name_part, dim_part = s.split(" ", 1)
        else:
            # comma-separated: "x,1,3,512,512"
            parts = s.split(",", 1)
            name_part = parts[0]
            dim_part  = parts[1] if len(parts) == 2 else "1"
        cmd += ["-d", name_part, dim_part]

    # Bug 4 fix: use shlex.join so paths with spaces are quoted in the log,
    # making the printed command directly reproducible in a terminal.
    print(f"[CMD] {shlex.join(cmd)}")
    # Bug 8 fix: set cwd=output_dir so qnn-onnx-converter and any tool it
    # spawns use output_dir as their working directory.  Without this the
    # subprocess inherits the server process CWD (QAIModelBuilder/backend/),
    # which causes qnn-context-binary-generator to create a stray ./output/
    # directory there instead of (or in addition to) the intended output_dir.
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(f"[ERROR] ONNX conversion failed (exit {r.returncode})")
        sys.exit(r.returncode)

    # Converter sometimes omits .cpp extension - fix it
    if not Path(cpp_out).exists():
        no_ext = Path(output_dir) / model_name
        if no_ext.exists():
            no_ext.rename(cpp_out)
            print(f"[FIX] Renamed {no_ext.name} -> {Path(cpp_out).name}")

    if not Path(cpp_out).exists():
        print(f"[ERROR] Expected output not found after conversion: {cpp_out}")
        sys.exit(1)

    print(f"[OK] {cpp_out}")
    return cpp_out, bin_out


def step2_compile_dll(python: str, sdk: str, cpp: str, bin_: str,
                      output_dir: str) -> str:
    """C++/bin -> ARM64 DLL.  Returns dll_path."""
    print("\n[STEP 2] C++/bin -> ARM64 DLL")
    lib_gen = str(Path(sdk) / "bin" / "aarch64-windows-msvc" / "qnn-model-lib-generator")
    cmd = [python, lib_gen,
           "-c", cpp, "-b", bin_,
           "-o", output_dir,
           "-t", "windows-aarch64"]

    # Bug 4 fix: shlex.join for reproducible log output
    print(f"[CMD] {shlex.join(cmd)}")
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        print(f"[ERROR] DLL compilation failed (exit {r.returncode})")
        sys.exit(r.returncode)

    # Find generated DLL, excluding known non-model runtime DLLs.
    # QnnHtp.dll and QnnHtpNetRunExtensions.dll may be present in output_dir
    # as part of the HTP runtime setup; neither is the model DLL we want.
    _EXCLUDED_DLL_STEMS = {"qnnhtp", "qnnhtpnetrunextensions"}
    dlls = [p for p in Path(output_dir).rglob("*.dll")
            if p.stem.lower() not in _EXCLUDED_DLL_STEMS]
    if not dlls:
        print("[ERROR] No model DLL found after compilation")
        sys.exit(1)

    dll = str(sorted(dlls, key=lambda p: p.stat().st_mtime)[-1])  # newest
    print(f"[OK] {dll}")
    return dll


def step3_context_binary(python: str, script_dir: str, sdk: str,
                         dll: str, output_dir: str, bin_name: str,
                         config_file: str, htp_version: str = "v73") -> str:
    """DLL -> context binary (.bin).  Returns bin_path."""
    print("\n[STEP 3] DLL -> context binary (HTP " + htp_version + ")")
    # HTP runtime files (QnnHtp.dll, libqnnhtpv73.cat, libQnnHtpV73Skel.so)
    # are copied and verified by qai_dev_gen_contextbin.py before the generator
    # runs (preflight gate), with cwd=output_dir so the generator finds them.

    ctx_script = str(Path(script_dir) / "qai_dev_gen_contextbin.py")
    cmd = [python, ctx_script,
           "--model", dll,
           "--output_dir", output_dir,
           "--binary_file", bin_name,
           "--htp_version", htp_version]

    if config_file and Path(config_file).exists():
        cmd += ["--config_file", config_file]
        print(f"[INFO] Backend config: {config_file}")
    else:
        cmd.append("--auto-config")
        print("[INFO] Using --auto-config")

    # Bug 4 fix: shlex.join for reproducible log output
    print(f"[CMD] {shlex.join(cmd)}")

    # Bug 8 fix: set cwd=output_dir so qai_dev_gen_contextbin.py (and the
    # qnn-context-binary-generator it spawns) use output_dir as their CWD.
    # The generator defaults --output_dir to ./output and creates that
    # directory at startup regardless of the --output_dir flag; without an
    # explicit cwd it falls back to the server CWD (QAIModelBuilder/backend/)
    # and creates a stray backend/output/ directory.
    # qai_dev_gen_contextbin.py exits with non-zero if either preflight gate
    # fails (VS ARM64 env or HTP files missing).  For the generator itself,
    # non-zero exit is normal -- qai_dev_gen_contextbin.py verifies by file
    # existence and exits 1 only if no valid .bin was produced.
    r = subprocess.run(cmd, cwd=output_dir)
    if r.returncode != 0:
        # HARD FAIL (aligned with V1 run_pipeline.py:326-328 / SKILL.md B8
        # "Blocking Condition"): the user asked to compile a context binary,
        # so we deliver exactly that type or fail loudly — no silent
        # downgrade to the .dll. context binary generation failing is rare
        # and usually indicates a corrupt/incomplete QAIRT SDK; the right
        # response is to surface the error so the user can fix the SDK, not
        # to mask it. Exit with the generator's return code (per product
        # decision: failure must error, never degrade).
        print(
            f"[ERROR] context binary generation failed (exit {r.returncode}). "
            "Check the QAIRT SDK: a 0-byte/corrupt qnn-context-binary-generator.exe "
            "is usually overwritten by a stray write (see SKILL.md B9), not a "
            "shipping defect. Diagnose READ-ONLY; restore that one exe or reinstall."
        )
        sys.exit(r.returncode)

    # -----------------------------------------------------------------------
    # Locate the generated context binary.
    #
    # Priority 1: expected path <output_dir>/<bin_name>.bin
    # Priority 2: recursive search -- exclude Step-1 intermediate files
    #   (root-level .bin files whose stem does NOT match bin_name).
    #   Files in subdirectories are always accepted (generator sometimes
    #   places the binary in a bins/ subfolder).
    #
    # Bug 5 fix: use bin_name for stem matching in fallback search instead
    # of deriving from dll stem, which may not match in all SDK versions.
    # -----------------------------------------------------------------------
    output_dir_resolved = Path(output_dir).resolve()

    expected = Path(output_dir) / f"{bin_name}.bin"
    if expected.exists() and expected.stat().st_size > 1024:
        print(f"[OK] {expected}")
        return str(expected)

    # Fallback: recursive search with intermediate-file exclusion
    candidates = []
    for root, dirs, filenames in os.walk(output_dir):
        root_resolved = Path(root).resolve()
        in_subdir = root_resolved != output_dir_resolved
        for f in filenames:
            if not f.endswith(".bin"):
                continue
            full = Path(root) / f
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size <= 1024:
                continue
            # Root-level files: only accept if stem matches bin_name
            if not in_subdir and Path(f).stem != bin_name:
                print(f"[INFO] Skipping Step-1 intermediate: {full}")
                continue
            candidates.append(full)

    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    if candidates:
        best = candidates[0]
        if len(candidates) > 1:
            print(f"[WARN] Multiple .bin candidates; using largest: {best}")
            for c in candidates[1:]:
                print(f"       Ignored: {c}")
        print(f"[OK] {best}")
        return str(best)

    # No valid .bin produced -- provide diagnostics
    print("[ERROR] No valid context binary (.bin) produced.")
    print(f"  Expected: {expected}")

    htp_files_v73 = ["QnnHtp.dll", "libqnnhtpv73.cat", "libQnnHtpV73Skel.so"]
    htp_files_v81 = ["QnnHtpV81Stub.dll", "libqnnhtpv81.cat", "libQnnHtpV81Skel.so"]
    missing_htp = [n for n in htp_files_v81 if not (Path(output_dir) / n).exists()]
    if missing_htp:
        print(f"  [DIAG] Missing HTP v81 runtime files in {output_dir}:")
        for mf in missing_htp:
            print(f"    MISSING: {mf}")
        print("  [DIAG] These should have been copied by qai_dev_gen_contextbin.py.")
        print("  [DIAG] Verify QAIRT_SDK_ROOT points to a complete SDK installation.")
    elif not any((Path(output_dir) / n).exists() for n in htp_files_v73):
        print(f"  [DIAG] No HTP runtime files found in {output_dir}.")
    else:
        print(f"  [DIAG] HTP runtime files present in {output_dir}: OK")

    actual_arch = os.environ.get("VSCMD_ARG_TGT_ARCH", "")
    if actual_arch.lower() != "arm64":
        print(f"  [DIAG] VSCMD_ARG_TGT_ARCH={actual_arch!r} -- VS ARM64 env NOT active.")
    else:
        print("  [DIAG] VSCMD_ARG_TGT_ARCH=arm64: OK")

    print("  Check generator output above for 'Wrong number of Parameters' or")
    print("  'DspTransport' errors.")
    # HARD FAIL (aligned with V1 run_pipeline.py:406): the generator exited 0
    # but produced no valid .bin. The user asked for a context binary, so we
    # error out rather than silently falling back to the .dll — failure must
    # be surfaced (per product decision: no degradation).
    print(
        "[ERROR] context binary generation produced no valid .bin. "
        "Please check the QAIRT SDK installation (it may be corrupt or "
        "incomplete)."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="End-to-End QNN Pipeline for WoS ARM64 (QAIRT 2.45)",
    )
    parser.add_argument("--model",       required=True)
    parser.add_argument("--output",      default="qairt_output")
    parser.add_argument("--precision",   default=None,
                        choices=["fp16", "fp32", "w8a16", "w8a8", "w8a8b8", "w4a16", "w4a8"])
    parser.add_argument("--act_bw",      type=int, default=None,
                        help="Custom activation bitwidth (4/8/16). Requires --weight_bw.")
    parser.add_argument("--weight_bw",   type=int, default=None,
                        help="Custom weight bitwidth (4/8). Requires --act_bw.")
    parser.add_argument("--bias_bw",     type=int, default=None,
                        help="Custom bias bitwidth (e.g. 8). Optional, only with --act_bw/--weight_bw.")
    parser.add_argument("--calib_list",  default="")
    parser.add_argument("--input_dim",   default="")
    parser.add_argument("--config",      default="")
    parser.add_argument("--skip_contextbin",   action="store_true")
    parser.add_argument("--no_simplification", action="store_true")
    parser.add_argument("--htp_version", default="v73",
                        help="HTP version to use ('v73' or 'v81', default: v73)")
    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Resolve precision: custom --act_bw/--weight_bw takes priority over
    # --precision preset. Exactly one of the two modes must be active.
    # -----------------------------------------------------------------------
    custom_bw = (args.act_bw is not None) or (args.weight_bw is not None)
    preset     = args.precision

    if custom_bw and preset is not None:
        print("[ERROR] --precision and --act_bw/--weight_bw are mutually exclusive.")
        print("  Use --precision for presets, or --act_bw/--weight_bw for custom bitwidths.")
        sys.exit(1)

    if custom_bw:
        # Custom mode: both act_bw and weight_bw are required
        if args.act_bw is None or args.weight_bw is None:
            print("[ERROR] --act_bw and --weight_bw must both be specified for custom mode.")
            sys.exit(1)
        act_bw    = args.act_bw
        weight_bw = args.weight_bw
        bias_bw   = args.bias_bw  # None = bias stays FP32
        # Derive a display label, e.g. w8a8b8 or w4a4
        precision_label = f"w{weight_bw}a{act_bw}" + (f"b{bias_bw}" if bias_bw else "")
        is_float = False
    else:
        # Preset mode: default to fp16 if nothing specified
        if preset is None:
            preset = "fp16"
        precision_bw = {
            "fp16":   (None, None, None),
            "fp32":   (None, None, None),
            "w8a16":  (16,   8,    None),
            "w8a8":   (8,    8,    None),
            "w8a8b8": (8,    8,    8),
            "w4a16":  (16,   4,    None),
            "w4a8":   (8,    4,    None),
        }
        act_bw, weight_bw, bias_bw = precision_bw[preset]
        precision_label = preset
        is_float = preset in ("fp16", "fp32")

    # 1. Load config
    script_dir = Path(__file__).resolve().parent
    cfg = find_qairt_env(script_dir)

    # 1b. Hard gate: an empty cfg means qairt_env.json was never found on this
    # machine. The most common cause is that Setup.bat did not finish Step 8
    # (e.g. the ~2GB QAIRT SDK download was interrupted), so Step 8.6 never ran
    # and data/config/qairt_env.json was never generated. Without this guard the
    # next call surfaces the obscure "vcvarsall.bat not found: ''" (empty string),
    # which hides the real root cause. Give an actionable message instead.
    if not cfg:
        print("[ERROR] 未找到 QAIRT 配置文件 data/config/qairt_env.json。")
        print("[ERROR] 该文件由 Setup.bat 的 Step 8.6 生成。最常见原因是 Setup.bat")
        print("[ERROR] 的 Step 8（含约 2GB 的 QAIRT SDK 下载）未完整执行就中断了，")
        print("[ERROR] 因此从未生成此配置（连 data/config 目录都不存在）。")
        print("[ERROR] 解决办法（任选其一）：")
        print("[ERROR]   1) 重新运行 Setup.bat（不要带 --no-builder），等它跑完整个")
        print("[ERROR]      Step 8（SDK 下载较慢，支持断点续传，请耐心等到打印")
        print("[ERROR]      '[OK] qairt_env.json generated' 为止）；")
        print("[ERROR]   2) 仅生成配置（无需重下 SDK）：")
        print("[ERROR]      <.venv_x64_310>\\Scripts\\python.exe "
              "scripts\\setup\\setup_qairt_env.py --gen-config")
        print("[ERROR] 注意：模型推理本身无需 VS 2022 / QAIRT SDK；若只是想跑推理，")
        print("[ERROR] 请改用 model-hub skill 下载预编译模型直接推理。")
        sys.exit(1)

    # 2. Initialize VS ARM64 environment (Windows only)
    if _detect_platform() == "windows" or cfg.get("platform", "windows") == "windows":
        init_vs_arm64(
            cfg.get("vs_vcvarsall", ""),
            cfg.get("vc_targets_path", ""),
        )

    # 2b. Ensure VS-bundled cmake is available in PATH (Windows only).
    # The QAIRT qnn-model-lib-generator internally invokes cmake. If the system
    # cmake is too old (e.g. 3.21 doesn't recognize VS 18), the build will fail
    # with "No CMAKE_C_COMPILER could be found". Using VS-bundled cmake ensures
    # compatibility with the installed VS version.
    vs_cmake_path = cfg.get("vs_cmake_path", "")
    if vs_cmake_path and os.path.isdir(vs_cmake_path):
        current_path = os.environ.get("PATH", "")
        if vs_cmake_path.lower() not in current_path.lower():
            os.environ["PATH"] = vs_cmake_path + os.pathsep + current_path
            print(f"[INFO] Prepended VS cmake to PATH: {vs_cmake_path}")

    # 3. Apply QAIRT SDK paths
    sdk, python_x64 = apply_qairt_env(cfg)
    if not sdk:
        print("[ERROR] QAIRT_SDK_ROOT not found in config or environment.")
        print("  Run Setup.bat, or ensure data/config/qairt_env.json exists.")
        sys.exit(1)

    print(f"[INFO] SDK    = {sdk}")
    print(f"[INFO] Python = {python_x64}")

    # 4. Validate args
    onnx = str(Path(args.model).resolve())
    if not Path(onnx).exists():
        print(f"[ERROR] Model not found: {onnx}")
        sys.exit(1)

    if not is_float:
        if not args.calib_list and _detect_platform() == "windows":
            # On Windows, calib_list is required for quantized precision.
            # On Linux, quantization is optional (Step 2 can be skipped).
            print(f"[ERROR] --calib_list required for quantized precision ({precision_label})")
            sys.exit(1)
        if args.calib_list and not Path(args.calib_list).exists():
            print(f"[ERROR] Calibration list not found: {args.calib_list}")
            sys.exit(1)

    output_dir = str(Path(args.output).resolve())
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    model_name = Path(onnx).stem

    print(f"\n{'='*60}")
    print(f"  QNN Pipeline: {model_name} [{precision_label}]")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")

    # -----------------------------------------------------------------------
    # 5. Run steps — Linux path (ONNX → DLC → optional quantized DLC → .bin)
    # -----------------------------------------------------------------------
    if _detect_platform() == "linux":
        print("\n[Linux 转换流程] ONNX → DLC → (量化) → context binary")

        dlc = step1_convert_linux(python_x64, sdk, onnx, output_dir, args.input_dim)

        if not is_float and args.calib_list:
            dlc = step2_quantize_linux(
                python_x64, sdk, dlc, output_dir, args.calib_list,
                act_bw or 16, weight_bw or 8, bias_bw,
            )
        elif not is_float and not args.calib_list:
            print("[WARN] Quantized precision requested but --calib_list not provided; "
                  "skipping quantization step")

        bin_name = f"{model_name}_{precision_label}"
        if args.skip_contextbin:
            print("\n[SKIP] Context binary generation skipped")
        else:
            ctx_script = str(Path(script_dir) / "qai_dev_gen_contextbin.py")
            cmd = [python_x64, ctx_script,
                   "--model", dlc,
                   "--output_dir", output_dir,
                   "--binary_file", bin_name,
                   "--htp_version", args.htp_version]
            print(f"[CMD] {shlex.join(cmd)}")
            r = subprocess.run(cmd, cwd=output_dir)
            # qnn-context-binary-generator commonly exits non-zero even on success;
            # success is determined by output file existence (checked inside the script).
            if r.returncode not in (0, 6):
                print(f"[WARN] Context binary generator exited {r.returncode}")

        print(f"\n{'='*60}")
        print(f"  Pipeline complete: {model_name} [{precision_label}]")
        print(f"  Output: {output_dir}")
        print(f"{'='*60}")
        sys.exit(0)

    # -----------------------------------------------------------------------
    # 5. Run steps — Windows path (ONNX → C++/bin → DLL → context binary)
    # -----------------------------------------------------------------------
    cpp, bin_ = step1_convert(
        python_x64, sdk, onnx, output_dir,
        precision_label, act_bw, weight_bw, bias_bw,
        args.calib_list, args.input_dim, args.no_simplification,
    )

    dll = step2_compile_dll(python_x64, sdk, cpp, bin_, output_dir)

    bin_name = f"{model_name}_{precision_label}"
    ctx_bin_path = None
    if args.skip_contextbin:
        print("\n[SKIP] Context binary generation skipped")
    else:
        ctx_bin_path = step3_context_binary(
            python_x64, str(script_dir), sdk,
            dll, output_dir, bin_name, args.config,
            htp_version=args.htp_version,
        )

    # -----------------------------------------------------------------------
    # Final report.
    # Bug 7 fix: use the path returned by step3_context_binary (which may be
    # in a subdirectory) rather than always looking at output_dir/bin_name.bin.
    # Also validate that the binary is plausibly large (>= 1 MB); a valid
    # context binary is typically tens of MB.
    # -----------------------------------------------------------------------
    _ONE_MB = 1 * 1024 * 1024

    print(f"\n{'='*60}")
    print(f"  Pipeline complete: {model_name} [{precision_label}]")
    print(f"  Output: {output_dir}")

    if ctx_bin_path:
        ctx_bin = Path(ctx_bin_path)
        if ctx_bin.exists():
            size = ctx_bin.stat().st_size
            if size >= _ONE_MB:
                print(f"  Binary: {ctx_bin}  ({size:,} bytes)")
            else:
                print(f"  [WARN] Binary exists but may be invalid "
                      f"(size={size} bytes, expected >= 1 MB): {ctx_bin}")
        else:
            print(f"  [WARN] Reported binary path does not exist: {ctx_bin}")
    elif not args.skip_contextbin:
        # step3 returned "" → context binary generation failed/skipped, but the
        # pipeline did NOT abort (graceful degradation). The .dll is the
        # inference artifact. Make the fallback explicit in the final report.
        # L-2 fix: report the REAL dll path returned by step2_compile_dll
        # (located via rglob, filename carries the precision suffix) instead
        # of a hardcoded ``output_dir/ARM64/{model_name}.dll`` guess that did
        # not match the actual artifact.
        dll_display = dll if dll else (
            Path(output_dir) / f"{bin_name}.dll"
        )
        print(f"  Inference artifact: {dll_display} (.dll — no context binary)")
        print("  [NOTE] context binary unavailable; using .dll directly "
              "(numerically identical, higher cold-start latency). If this "
              "recurs, check qnn-context-binary-generator.exe (a 0-byte one is "
              "usually overwritten, not a defect — see SKILL.md B9): restore "
              "that exe or reinstall the QAIRT SDK.")

    print(f"{'='*60}")


if __name__ == "__main__":
    main()
