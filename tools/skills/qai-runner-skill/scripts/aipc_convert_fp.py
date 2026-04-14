# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
QNN FP16/FP32 Conversion Script

Converts ONNX models to QNN format (FP16 or FP32 precision).

Usage:
  # Simple conversion (static input model)
  python aipc_convert_fp.py --onnx model.onnx --output_dir output --precision 16

  # Dynamic input model (specify fixed dimensions)
  python aipc_convert_fp.py --onnx model.onnx --output_dir output --precision 16 \\
    --input-dims input:1,3,64,64

  # Multiple models
  python aipc_convert_fp.py --onnx *.onnx --output_dir output --precision 16

  # FP32 precision
  python aipc_convert_fp.py --onnx model.onnx --output_dir output --precision 32

Known Issues & Solutions:
  - "Missing command line inputs for dynamic inputs": Use --input-dims name:1,3,H,W
  - "Access is denied": Use absolute output path
  - "is not a cpp model file": Script auto-fixes this now
  - Windows ARM64: Context binary generation required after conversion

Args:
  --onnx: Path(s) to ONNX file(s)
  --output_dir: Output directory for generated libraries
  --precision: 16 (FP16) or 32 (FP32)
  --input-dims: Input dimensions for dynamic models (name:dim1,dim2,...)
  --target_arch: Target architecture (default: windows-aarch64)
"""

import argparse
import glob
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

def _find_first_so(output_dir: str) -> str | None:
    # Try to find a generated library file. Prefer .dll on Windows, .so elsewhere,
    # but fall back to the other extension if necessary.
    try:
        if platform.system().lower() == "windows":
            for p in sorted(Path(output_dir).rglob("*.dll")):
                return str(p)
            for p in sorted(Path(output_dir).rglob("*.so")):
                return str(p)
        else:
            for p in sorted(Path(output_dir).rglob("*.so")):
                return str(p)
            for p in sorted(Path(output_dir).rglob("*.dll")):
                return str(p)
    except Exception:
        return None
    return None


def _cleanup_tmp_folders(search_dir: str) -> None:
    """Remove tmp_<pid>/ folders created by qnn-model-lib-generator.

    The generator creates temporary folders (e.g. tmp_1279/) containing
    hundreds of .o object files that are never cleaned up automatically.
    """
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


def convert_onnx_to_qnn(
    qnn_sdk_root: str,
    host_arch: str,
    target_arch: str,
    precision: int,
    onnx_paths: list[str],
    output_root: str | None,
    cleanup_intermediate: bool = True,
    input_dims: list[tuple[str, str]] | None = None,
) -> int:
    """
    Converts one or more ONNX models to QNN model libraries (.so / .dll).

    Runs two steps per model:
      1. ``qnn-onnx-converter``      -> ONNX -> C++ + binary weights
      2. ``qnn-model-lib-generator`` -> C++ + binary -> compiled shared library

    Args:
        qnn_sdk_root (str): Absolute path to the QAIRT SDK root directory
            (value of ``QAIRT_SDK_ROOT``).
        host_arch (str): Host toolchain folder under ``{qnn_sdk_root}/bin/``
            (e.g. ``"x86_64-linux-clang"``, ``"x86_64-windows-msvc"``).
        target_arch (str): Target architecture for the compiled library
            (e.g. ``"aarch64-ubuntu-gcc9.4"``, ``"x86_64-linux-clang"``).
        precision (int): Float bitwidth -> ``16`` (FP16) or ``32`` (FP32).
        onnx_paths (list[str]): Paths to ONNX files to convert. If empty,
            all ``*.onnx`` files under the current directory are used.
        output_root (str | None): Root directory for ``test_libs_*`` output
            folders. Defaults to the directory containing each ``.onnx`` file.
        cleanup_intermediate (bool): Remove intermediate ``.cpp``, ``.bin``,
            and ``_net.json`` files after a successful build. Default ``True``.
        input_dims (list[tuple[str, str]] | None): Explicit input dimensions
            for models with dynamic shapes. Each tuple is
            ``(input_name, "dim1,dim2,...")``, e.g.
            ``[("images", "1,3,640,640")]``.

    Returns:
        int: ``0`` if all models converted successfully, ``1`` if any failed.
    """
    if not qnn_sdk_root:
        raise ValueError("QAIRT_SDK_ROOT environment variable not set. Please source the QAIRT environment script.")
    #print host arch and target arch
    print(f"Host architecture: {host_arch}")
    print(f"Target architecture: {target_arch}")
    qnn_onnx_converter = os.path.join(qnn_sdk_root, "bin", host_arch, "qnn-onnx-converter")
    qnn_model_lib_generator = os.path.join(qnn_sdk_root, "bin", host_arch, "qnn-model-lib-generator")

    if platform.system().lower() == "windows":
        # Prefer parsing `systeminfo` for more accurate architecture detection on Windows.

        # qairt Windows SDK toolchains only support x86_64 for now. using emulation for ARM64 Windows.
        arch = get_cpu_arch_from_systeminfo()

        if arch:
            if 'arm' in arch:
                #qnn_onnx_converter = os.path.join(qnn_sdk_root, "bin", 'arm64x-windows-msvc', "qnn-onnx-converter")
                #qnn_model_lib_generator = os.path.join(qnn_sdk_root, "bin", 'aarch64-windows-msvc', "qnn-model-lib-generator")
                pass


    if not os.path.exists(qnn_onnx_converter):
        raise FileNotFoundError(f"qnn-onnx-converter not found at: {qnn_onnx_converter}")
    if not os.path.exists(qnn_model_lib_generator):
        raise FileNotFoundError(f"qnn-model-lib-generator not found at: {qnn_model_lib_generator}")

    env = os.environ.copy()
    fp_tag = f"fp{precision}"

    converted = 0
    failed = 0

    # Convert provided ONNX paths (or default: all .onnx under cwd)
    if not onnx_paths:
        onnx_paths = glob.glob("**/*.onnx", recursive=True)

    for model_path in onnx_paths:
        if not os.path.exists(model_path):
            print(f"Skipping missing ONNX: {model_path}")
            failed += 1
            continue

        model_name = os.path.basename(model_path).replace(".onnx", "")
        abs_model_path = os.path.abspath(model_path)
        abs_model_dir = os.path.dirname(abs_model_path)

        if target_arch == "aarch64-ubuntu-gcc9.4" and "QNN_AARCH64_UBUNTU_GCC_94" not in env:
            # qnn-model-lib-generator's ubuntu toolchain Makefile uses this as a sysroot path.
            env["QNN_AARCH64_UBUNTU_GCC_94"] = "/"

        output_dir_name = f"test_libs_{model_name}_{fp_tag}_{target_arch}"
        base_out_dir = os.path.abspath(output_root) if output_root else abs_model_dir
        abs_output_dir_path = os.path.join(base_out_dir, output_dir_name)
        abs_cpp_path = os.path.join(abs_model_dir, f"{model_name}.cpp")
        abs_bin_path = os.path.join(abs_model_dir, f"{model_name}.bin")

        print(f"Converting {abs_model_path} to {fp_tag} for {target_arch}...")

        # Step 1: Convert ONNX to C++ and binary
        #python_exe = os.path.join(qnn_sdk_root, "bin", "venv", "Scripts", "python.exe")
        python_exe = sys.executable
        if not os.path.exists(python_exe):
            python_exe = "python"  # fallback to system python if venv python not found

        # Update the environment to include the QNN SDK's Python path
        model_env = os.environ.copy()
        qnn_python_path = os.path.join(qnn_sdk_root, "lib", "python")
        if 'PYTHONPATH' in model_env:
            model_env['PYTHONPATH'] = qnn_python_path + os.pathsep + model_env['PYTHONPATH']
        else:
            model_env['PYTHONPATH'] = qnn_python_path
        print("python exe",python_exe)
        # Also preserve the original target architecture environment variable
        if target_arch == "aarch64-ubuntu-gcc9.4" and "QNN_AARCH64_UBUNTU_GCC_94" in env:
            model_env["QNN_AARCH64_UBUNTU_GCC_94"] = env["QNN_AARCH64_UBUNTU_GCC_94"]

        converter_command = [
            python_exe, qnn_onnx_converter,
            "--input_network", abs_model_path,
            "--output_path", abs_cpp_path,
            "--float_bitwidth", str(precision),
            "--preserve_io"
        ]

        if input_dims:
            for input_name, dims in input_dims:
                converter_command.extend(["-d", input_name, dims])

        print(f"Running converter command: {' '.join(converter_command)}")
        try:
            subprocess.run(converter_command, check=True, env=model_env, encoding='utf-8', errors='replace')
            print(f"Successfully converted ONNX to C++ and binary for {model_name}")
            
            # Fix: Handle case where converter creates file without .cpp extension
            # This is a known QAIRT SDK bug on Windows where qnn-onnx-converter sometimes
            # outputs the model graph file without the .cpp extension (e.g., "model" instead
            # of "model.cpp"). This causes qnn-model-lib-generator to fail with:
            #   "ValueError: <path> is not a cpp model file"
            # 
            # Root cause: The converter writes to --output_path directly and may strip the
            # extension on certain Windows configurations. The _net.json file gets its
            # extension correctly, but the main .cpp file does not.
            #
            # Workaround: Check if file exists without extension and rename it.
            if not os.path.exists(abs_cpp_path):
                cpp_no_ext = abs_cpp_path.replace('.cpp', '')
                if os.path.exists(cpp_no_ext) and not cpp_no_ext.endswith('.bin'):
                    os.rename(cpp_no_ext, abs_cpp_path)
                    print(f"Fixed: Renamed {cpp_no_ext} to {abs_cpp_path}")
        except subprocess.CalledProcessError as e:
            print(f"\n[ERROR] QNN conversion failed for {model_name}")
            print(f"\nTroubleshooting tips:")
            print(f"  1. If error mentions 'dynamic inputs', add: --input-dims name:1,3,H,W")
            print(f"  2. If 'access denied', ensure output path is writable")
            print(f"  3. If 'unsupported operator', check dry-run first")
            print(f"\nFailed command: {' '.join(converter_command)}")
            failed += 1
            continue

        # Step 2: Generate model library
        generator_command = [
            python_exe, qnn_model_lib_generator,
            "-c", abs_cpp_path,
            "-b", abs_bin_path,
            "-o", abs_output_dir_path,
            "-t", target_arch
        ]

        print(f"Running generator command: {' '.join(generator_command)}")
        try:
            subprocess.run(generator_command, check=True, env=model_env, encoding='utf-8', errors='replace')
            print(f"Successfully generated model library for {model_name} at {abs_output_dir_path}")
            converted += 1
        except subprocess.CalledProcessError as e:
            print(f"Error during model library generation for {model_name}: {e}")
            failed += 1
            continue

        so_path = _find_first_so(abs_output_dir_path)
        if so_path:
            ext = Path(so_path).suffix.lower()
            print(f"QNN model library ({ext}): {so_path}")
        else:
            print("Warning: did not find a .so or .dll under the output directory; check qnn-model-lib-generator output.")

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

            # Clean up temp folders created by qnn-model-lib-generator
            # These are named tmp_<pid>/ and contain hundreds of .o object files
            _cleanup_tmp_folders(abs_model_dir)

        print(f"Successfully converted {model_path} to {abs_output_dir_path}")

    print(f"\nDone. Converted: {converted}, Failed: {failed}")
    return 0 if failed == 0 else 1

def get_cpu_arch_from_systeminfo():
    try:
        result = subprocess.run(['systeminfo'], capture_output=True, text=True, check=True, encoding='utf-8')
        output = result.stdout

        # Try to extract architecture from the System Type or OS Name lines
        system_type_match = re.search(r"System Type:\s*(.*?)\r?\n", output, re.IGNORECASE)
        os_name_match = re.search(r"OS Name:\s*(.*?)\r?\n", output, re.IGNORECASE)

        # Look for common architecture keywords
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

        # As a fallback, scan for processor lines that may contain "ARM" or "Intel" indicators
        for line in output.splitlines():
            if any(tok in line for tok in ('ARM', 'ARM64', 'Intel', 'AMD', 'Qualcomm')):
                arch_match = re.search(r'(arm64|aarch64|arm|amd64|x86_64|x86)', line, re.IGNORECASE)
                if arch_match:
                    val = arch_match.group(1).lower()
                    # normalize aarch64 -> arm64, x86_64 -> amd64
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
    Detects the host architecture and selects the appropriate toolchain from the available options.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    print(system, machine)
    # Mapping detected system/machine to available toolchains
    if system == "windows":
        # Prefer parsing `systeminfo` for more accurate architecture detection on Windows.

        # qairt Windows SDK toolchains only support x86_64 for now. using emulation for ARM64 Windows.
        return "x86_64-windows-msvc"
        arch = None
        try:
            arch = get_cpu_arch_from_systeminfo()
        except Exception:
            arch = None

        if arch:
            if 'arm' in arch:
                return "aarch64-windows-msvc"
            if arch in ("amd64", "x86_64", "x64", "x86"):
                return "x86_64-windows-msvc"

        # Fall back to platform.machine() when systeminfo parsing fails
        if machine in ["amd64", "x86_64"]:
            return "x86_64-windows-msvc"
        elif machine in ["arm64", "aarch64"]:
            return "aarch64-windows-msvc"
        # Default fallback for Windows
        return "x86_64-windows-msvc"
    elif system == "linux":
        if machine in ["amd64", "x86_64"]:
            return "x86_64-linux-clang"
        elif machine in ["arm64", "aarch64"]:
            return "aarch64-ubuntu-gcc9.4" 
        """
        use aarch64-oe-linux-gcc11.2 if we meet issue.
        """

    # Default fallback
    return "x86_64-windows-msvc"


def detect_target_arch():
    """
    Detect a reasonable QNN target architecture string based on the current device.
    Uses `get_cpu_arch_from_systeminfo()` on Windows for more accurate detection.
    Returns one of: windows-aarch64, windows-x86_64, aarch64-ubuntu-gcc9.4, x86_64-linux-clang, etc.
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


if __name__ == "__main__":
    # Print all environment variables for debugging
    #print("Environment Variables:")
    #print("-" * 30)
    #for key, value in os.environ.items():
    #    print(f"{key}: {value}")
    #print("-" * 30)

    # Print Python executable path
    import sys
    print(f"Python executable: {sys.executable}")
    print("-" * 30)

    parser = argparse.ArgumentParser(description="Convert ONNX models to QNN model libraries.")
    parser.add_argument(
        "--onnx",
        action="append",
        default=[],
        help="ONNX path to convert (repeatable). If omitted, converts all .onnx under the current directory.",
    )
    parser.add_argument("--precision", type=int, default=16, choices=(16, 32), help="Float bitwidth (16 or 32).")
    parser.add_argument("--host-arch", default="", help="QNN host arch folder under bin/. If not specified, auto-detected.")

    #target arch: aarch64-oe-linux-gcc11.2, aarch64-android, aarch64-ubuntu-gcc9.4, x86_64-linux-clang,
    #         windows-aarch64, windows-x86_64.
    parser.add_argument(
        "--target-arch",
        default="",
        help="QNN target arch (also used for generated library).",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Optional root folder for generated test_libs_* output (default: alongside the .onnx).",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_false",
        dest="cleanup_intermediate",
        help="Don't cleanup intermediate files (.bin, .cpp, .json) after successful conversion.",
    )
    parser.add_argument(
        "--input-dim",
        action="append",
        default=[],
        dest="input_dims",
        metavar=("INPUT_NAME,DIMS"),
        help="Explicit input dimensions for dynamic inputs. Format: input_name,1,3,224,224 (repeatable). Example: --input-dim input,1,3,64,64",
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

    qnn_sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not qnn_sdk_root:
        print("Error: QAIRT_SDK_ROOT is not set. Source your QAIRT SDK environment first.")
        sys.exit(2)

    # Auto-detect host architecture if not provided
    if not args.host_arch :
        args.host_arch = detect_host_arch()
        print(f"Auto-detected host architecture: {args.host_arch}")

    # Auto-detect target architecture if not provided
    if not args.target_arch:
        args.target_arch = detect_target_arch()
        print(f"Auto-detected target architecture: {args.target_arch}")

    sys.exit(
        convert_onnx_to_qnn(
            qnn_sdk_root,
            args.host_arch,
            args.target_arch,
            args.precision,
            args.onnx,
            args.output_root,
            args.cleanup_intermediate,
            parsed_input_dims,
        )
    )
