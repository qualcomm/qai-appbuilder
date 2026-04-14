# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

#!/usr/bin/env python3
"""
QNN INT8/A16W8 Quantized Conversion Script

Converts ONNX models to quantized QNN format (INT8 or A16W8 precision).

Usage:
  # Simple conversion (static input model)
  python aipc_convert_int.py --input_network model.onnx --input_list calibration_list.txt

  # Dynamic input model (specify fixed dimensions)
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt \\
    --input-dim input,1,3,64,64

  # Custom bit widths (A16W8)
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt \\
    --act_bw 16 --weight_bw 8

  # INT8 (A8W8)
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt \\
    --act_bw 8 --weight_bw 8

Known Issues & Solutions:
  - "Missing command line inputs for dynamic inputs": Use --input-dim name:1,3,H,W
  - "Access is denied": Use absolute output path
  - "is not a cpp model file": Script auto-fixes this (same as aipc_convert_fp.py)
  - "calibration_list.txt not found": Create calibration list with raw input files

Args:
  --input_network: Path to ONNX file
  --input_list: Path to calibration list file (required)
  --act_bw: Activation bitwidth (default: 16)
  --weight_bw: Weight bitwidth (default: 8)
  --input-dim: Input dimensions for dynamic models (repeatable)
  --target-arch: Target architecture (default: auto-detected)
"""

import os
import sys
import subprocess
import glob
import argparse
import platform
import re
import shutil
from pathlib import Path


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


def find_onnx_files(search_dir="."):
    """Find all ONNX files in the specified directory."""
    search_path = Path(search_dir)
    return list(search_path.glob("*.onnx"))


def get_model_info(model_path, act_bw=16, weight_bw=8, output_root=None):
    """Extract model information from the ONNX file path."""
    model_path = Path(model_path)
    model_name = model_path.stem
    model_name_quant = f"{model_name}_a{act_bw}_w{weight_bw}"
    model_dir = model_path.parent
    abs_model_dir = os.path.abspath(str(model_dir))

    # Use os.path.join for cross-platform compatibility
    output_dir_name = f"test_libs_{model_name_quant}_aarch64_a{act_bw}_w{weight_bw}"
    base_out_dir = os.path.abspath(output_root) if output_root else abs_model_dir
    output_dir_path = os.path.join(base_out_dir, output_dir_name)
    cpp_path = os.path.join(abs_model_dir, f"{model_name_quant}.cpp")
    bin_path = os.path.join(abs_model_dir, f"{model_name_quant}.bin")
    
    return {
        "model_path": str(model_path),
        "model_name": model_name_quant,
        "model_dir": abs_model_dir,
        "output_dir_path": output_dir_path,
        "cpp_path": cpp_path,
        "bin_path": bin_path
    }


def convert_model(model_info, cwd, calibration_list_path, act_bw=16, weight_bw=8,
                  qnn_sdk_root=None, host_toolchain="",
                  device_toolchain="", cleanup_intermediate=True,
                  input_dims=None):
    """
    Convert ONNX model to quantized QNN format.
    
    Args:
        model_info: Dictionary containing model paths and information
        cwd: Current working directory (absolute path)
        calibration_list_path: Path to calibration list file
        act_bw: Activation bit width (default: 16)
        weight_bw: Weight bit width (default: 8)
        qnn_sdk_root: QAIRT SDK root path (default: from env or /local/mnt/workspace/project/qnn/qairt/2.41.0)
        host_toolchain: Host toolchain (default: auto-detected using detect_host_arch())
        device_toolchain: Device toolchain (default: auto-detected using detect_target_arch())
    
    Prerequisites:
        - QAIRT_SDK_ROOT environment variable must be set (or provided via parameter)
        - QNN_AARCH64_UBUNTU_GCC_94 environment variable should be set
        - User must have sourced the QNN environment setup script before running
    """
    model_path = model_info["model_path"]
    cpp_path = model_info["cpp_path"]
    bin_path = model_info["bin_path"]
    output_dir_path = model_info["output_dir_path"]
    
    print(f"Converting {model_path} to a{act_bw}_w{weight_bw} quantized format for aarch64...")
    
    # Auto-detect host toolchain if not provided
    if not host_toolchain:
        host_toolchain = detect_host_arch()
        print(f"Auto-detected host toolchain: {host_toolchain}")
    
    # Auto-detect device toolchain if not provided
    if not device_toolchain:
        device_toolchain = detect_target_arch()
        print(f"Auto-detected device toolchain: {device_toolchain}")
    
    # Determine QAIRT SDK root
    if qnn_sdk_root is None:
        qnn_sdk_root = os.environ.get('QAIRT_SDK_ROOT', '/local/mnt/workspace/project/qnn/qairt/2.41.0')
    
    # Check if QAIRT_SDK_ROOT exists
    if not os.path.exists(qnn_sdk_root):
        print(f"Error: QAIRT_SDK_ROOT path does not exist: {qnn_sdk_root}", file=sys.stderr)
        print("Please set QAIRT_SDK_ROOT environment variable or ensure the default path exists", file=sys.stderr)
        return False
    
    # Ensure QNN_AARCH64_UBUNTU_GCC_94 is set (required by QNN tools)
    if 'QNN_AARCH64_UBUNTU_GCC_94' not in os.environ:
        print("Warning: QNN_AARCH64_UBUNTU_GCC_94 environment variable is not set", file=sys.stderr)
        print("Setting it to '/' as default", file=sys.stderr)
        os.environ['QNN_AARCH64_UBUNTU_GCC_94'] = '/'
    
    python_exe = sys.executable
    if not os.path.exists(python_exe):
        python_exe = "python"  # fallback to system python if venv python not found    
    # Convert paths to absolute paths using os.path for portability
    abs_model_path = os.path.abspath(os.path.join(str(cwd), model_path))
    abs_cpp_path = os.path.abspath(os.path.join(str(cwd), cpp_path))
    abs_bin_path = os.path.abspath(os.path.join(str(cwd), bin_path))
    abs_output_dir = os.path.abspath(os.path.join(str(cwd), output_dir_path))
    abs_calibration_list = os.path.abspath(os.path.join(str(cwd), str(calibration_list_path)))
    
    # Build the qnn-onnx-converter command
    converter_path = os.path.join(qnn_sdk_root, "bin", host_toolchain, "qnn-onnx-converter")
    converter_cmd = [
        python_exe, converter_path,
        "--input_network", abs_model_path,
        "--output_path", abs_cpp_path,
        "--preserve_io",
        "--input_list", abs_calibration_list,
        "--act_bw", str(act_bw),
        "--weight_bw", str(weight_bw)
    ]

    if input_dims:
        for input_name, dims in input_dims:
            converter_cmd.extend(["-d", input_name, dims])
    
    # Build the qnn-model-lib-generator command
    lib_gen_path = os.path.join(qnn_sdk_root, "bin", host_toolchain, "qnn-model-lib-generator")
    lib_gen_cmd = [
        python_exe, lib_gen_path,
        "-c", abs_cpp_path,
        "-b", abs_bin_path,
        "-o", abs_output_dir,
        "-t", device_toolchain
    ]
    
    try:
        # Execute the converter command
        print(f"Running qnn-onnx-converter...")
        print(f"Command: {' '.join(converter_cmd)}")
        subprocess.run(
            converter_cmd,
            check=True,
            encoding='utf-8',
            errors='replace'
        )
        print(f"Successfully converted ONNX to C++ and binary for {model_info['model_name']}")
        
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

        # Execute the lib generator command
        print(f"\nRunning qnn-model-lib-generator...")
        print(f"Command: {' '.join(lib_gen_cmd)}")
        subprocess.run(
            lib_gen_cmd,
            check=True,
            encoding='utf-8',
            errors='replace'
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

            # Clean up temp folders created by qnn-model-lib-generator
            _cleanup_tmp_folders(model_info["model_dir"])

        return True
        
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] INT quantization failed for {model_info['model_name']}")
        print(f"\nTroubleshooting tips:")
        print(f"  1. If error mentions 'dynamic inputs', add: --input-dim name:1,3,H,W")
        print(f"  2. If 'access denied', ensure output path is writable")
        print(f"  3. If 'calibration_list' not found, check --input_list path")
        print(f"  4. If 'unsupported operator', check dry-run first")
        print(f"\nFailed command: {' '.join(converter_cmd)}")
        return False
    except Exception as e:
        print(f"\n[ERROR] Unexpected error converting {model_path}: {e}")
        return False


def main():
    """Main function to process all ONNX files."""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Convert ONNX models to quantized QNN format (INT8/A16W8)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Prerequisites:
  Before running this script, ensure you have:
  1. QAIRT_SDK_ROOT environment variable set
  2. Calibration data (raw float32 binary files)
  3. Calibration list file (one path per line)
  4. Sourced QNN environment setup script

Calibration Data Format:
  - Raw float32 binary files (.raw)
  - Shape matching model input (e.g., 1x3x64x64 = 49152 floats)
  - 50-200 representative samples recommended

Examples:
  # Convert with default settings (A16W8)
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt

  # INT8 quantization (A8W8)
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt \\
    --act_bw 8 --weight_bw 8

  # Dynamic input model
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt \\
    --input-dim input,1,3,64,64

  # Custom output directory
  python aipc_convert_int.py --input_network model.onnx --input_list calib.txt \\
    --output-root ./qnn_output
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
        help='Path for output .cpp file. If not specified, uses <model_name>_a{act_bw}_w{weight_bw}.cpp in same directory as input'
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
        help='Activation bit width for quantization (default: 16)'
    )
    
    parser.add_argument(
        '--weight_bw',
        type=int,
        default=8,
        help='Weight bit width for quantization (default: 8)'
    )
    
    parser.add_argument(
        '--qnn_sdk_root',
        type=str,
        default=None,
        help='QAIRT SDK root path (default: from QAIRT_SDK_ROOT env or /local/mnt/workspace/project/qnn/qairt/2.41.0)'
    )
    
    parser.add_argument(
        '--host-arch',
        type=str,
        default='',
        help='Host toolchain for QNN tools (default: auto-detected using detect_host_arch())'
    )
    
    parser.add_argument(
        '--target-arch',
        type=str,
        default='',
        help='Device toolchain for model compilation (default: auto-detected using detect_target_arch())'
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
        help="Explicit input dimensions for dynamic inputs. Format: input_name,1,3,224,224 (repeatable). Example: --input-dim input,1,3,64,64"
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
    
    # Get current working directory using os.getcwd() for portability
    cwd = os.getcwd()
    
    # Check if calibration list exists
    calibration_list_path = args.input_list
    calib_path_full = os.path.join(cwd, calibration_list_path)
    if not os.path.exists(calib_path_full):
        print(f"\n[WARNING] Calibration list not found: {calib_path_full}", file=sys.stderr)
        print(f"\nCalibration list is REQUIRED for quantization.")
        print(f"Format: One raw input file path per line.")
        print(f"Example calibration_list.txt:")
        print(f"  calib_data/sample_001.raw")
        print(f"  calib_data/sample_002.raw")
        print(f"  ...")
        print(f"\nEach .raw file should be:")
        print(f"  - Float32 binary data")
        print(f"  - Shape matching model input (e.g., 1x3x64x64 = 49152 floats)")
        print(f"  - 50-200 representative samples recommended")
        print(f"\nCreate calibration data first, then re-run this script.")
        response = input("\nContinue anyway without calibration? (y/n): ")
        if response.lower() != 'y':
            sys.exit(1)
    
    # Determine which ONNX files to process
    if args.input_network:
        # Process single specified file
        if not os.path.exists(args.input_network):
            print(f"Error: Input file '{args.input_network}' not found", file=sys.stderr)
            sys.exit(1)
        onnx_files = [Path(args.input_network)]
    else:
        # Find all ONNX files in current directory
        onnx_files = find_onnx_files()
        
        if not onnx_files:
            print("No ONNX files found in the current directory")
            sys.exit(1)
    
    print(f"Found {len(onnx_files)} ONNX file(s) to convert")
    print(f"Configuration: act_bw={args.act_bw}, weight_bw={args.weight_bw}")
    
    # Process each ONNX file
    success_count = 0
    fail_count = 0
    
    for onnx_file in onnx_files:
        model_info = get_model_info(onnx_file, args.act_bw, args.weight_bw, args.output_root)
        
        # Override output path if specified
        if args.output_path and len(onnx_files) == 1:
            model_info['cpp_path'] = args.output_path
            # Update bin path to match
            model_info['bin_path'] = os.path.splitext(args.output_path)[0] + '.bin'
        
        if convert_model(model_info, cwd, calibration_list_path, args.act_bw, args.weight_bw,
                        args.qnn_sdk_root, args.host_arch, args.target_arch, args.cleanup_intermediate,
                        parsed_input_dims):
            success_count += 1
        else:
            fail_count += 1
    
    # Print summary
    print("\n" + "="*50)
    print(f"Conversion Summary:")
    print(f"  Total files: {len(onnx_files)}")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {fail_count}")
    print("="*50)
    
    # Exit with appropriate code
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
