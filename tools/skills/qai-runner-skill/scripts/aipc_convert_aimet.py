#!/usr/bin/env python3
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""
AIPC AIMET Handoff Converter Script

Converts pre-calibrated AIMET ONNX models and scale encodings to quantized QNN or SNPE formats.

Usage:
  # Convert to QNN format (A16W8 / default QNN flow)
  python aipc_convert_aimet.py --input_network model_ptq.onnx --quantization_overrides model_ptq.encodings

  # Convert to SNPE DLC format
  python aipc_convert_aimet.py --input_network model_ptq.onnx --quantization_overrides model_ptq.encodings --flow SNPE
"""

import os
import sys
import argparse
import subprocess
import platform
import shutil
from pathlib import Path

# Helper function to detect host arch
def detect_host_arch():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "x86_64-windows-msvc"
    elif system == "linux":
        if machine in ["amd64", "x86_64"]:
            return "x86_64-linux-clang"
        elif machine in ["arm64", "aarch64"]:
            return "aarch64-ubuntu-gcc9.4"
    return "x86_64-linux-clang"

# Helper function to detect target arch
def detect_target_arch():
    system = platform.system().lower()
    if system == "windows":
        return "windows-aarch64"
    if system == "linux":
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            return "aarch64-ubuntu-gcc9.4"
        return "x86_64-linux-clang"
    return "x86_64-linux-clang"

# Helper function to get model info
def get_model_info(model_path, act_bw, weight_bw, output_root=None):
    model_path = Path(model_path)
    model_name = model_path.stem
    model_name_quant = f"{model_name}_q"
    model_dir = model_path.parent
    abs_model_dir = os.path.abspath(str(model_dir))

    output_dir_name = f"test_libs_{model_name_quant}_aarch64"
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

# Retrieve input names and shapes from ONNX model
def get_onnx_input_info(model_path, explicit_dims=None):
    import onnx
    model = onnx.load(model_path)
    inputs = {}
    
    # Map explicit inputs
    explicit_map = {}
    if explicit_dims:
        for name, dims in explicit_dims:
            explicit_map[name] = [int(d) for d in dims.split(",")]

    for inp in model.graph.input:
        name = inp.name
        if name in explicit_map:
            inputs[name] = explicit_map[name]
            continue
            
        shape = []
        for dim in inp.type.tensor_type.shape.dim:
            if dim.HasField("dim_value"):
                shape.append(dim.dim_value)
            elif dim.HasField("dim_param"):
                # Dynamic named dimension: fallback to 1
                shape.append(1)
            else:
                shape.append(1) # fallback
        if not shape:
            shape = [1, 3, 224, 224]
        inputs[name] = shape
        
    return inputs

def main():
    parser = argparse.ArgumentParser(
        description='Convert pre-calibrated AIMET ONNX models to QNN/SNPE formats using quantization overrides',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--input_network', type=str, required=True, help='Path to exported AIMET ONNX model')
    parser.add_argument('--quantization_overrides', type=str, required=True, help='Path to exported AIMET scale encodings')
    parser.add_argument('--act_bw', type=int, default=16, help='Activation bitwidth (default: 16)')
    parser.add_argument('--weight_bw', type=int, default=8, help='Weight bitwidth (default: 8)')
    parser.add_argument('--flow', choices=('QNN', 'SNPE'), default='QNN', help='Target flow (default: QNN)')
    parser.add_argument('--output-root', type=str, default=None, help='Output directory')
    parser.add_argument('--qnn_sdk_root', type=str, default=None, help='QAIRT SDK root path')
    parser.add_argument('--host-arch', type=str, default='', help='Host toolchain')
    parser.add_argument('--target-arch', type=str, default='', help='Target toolchain')
    parser.add_argument('--no-cleanup', action='store_false', dest='cleanup', help='Do not clean intermediate files')
    parser.add_argument('--preserve-io-mode', choices=('datatype', 'layout', 'none'), default='datatype', help='Preserve IO mode')
    parser.add_argument('--input-dim', action='append', default=[], dest='input_dims', metavar="INPUT_NAME,DIMS", help="Explicit input shape override")
    
    args = parser.parse_args()

    # Resolve paths
    abs_onnx = os.path.abspath(args.input_network)
    abs_overrides = os.path.abspath(args.quantization_overrides)
    
    if not os.path.exists(abs_onnx):
        print(f"[ERROR] ONNX model not found: {abs_onnx}", file=sys.stderr)
        sys.exit(1)
        
    if not os.path.exists(abs_overrides):
        print(f"[ERROR] Quantization overrides file not found: {abs_overrides}", file=sys.stderr)
        sys.exit(1)

    # Parse explicit input dims
    parsed_dims = []
    for item in args.input_dims:
        if "," in item:
            parts = item.split(",", 1)
            parsed_dims.append((parts[0], parts[1]))

    # Discover ONNX inputs and shapes (lazy import onnx)
    import onnx
    inputs_info = get_onnx_input_info(abs_onnx, parsed_dims)
    print("\nDiscovering ONNX input schemas:")
    for name, shape in inputs_info.items():
        print(f"  Input: '{name}', Shape: {shape}")

    # Resolve SDK Toolchains
    host_toolchain = args.host_arch or detect_host_arch()
    device_toolchain = args.target_arch or detect_target_arch()
    qnn_sdk_root = args.qnn_sdk_root or os.environ.get('QAIRT_SDK_ROOT', '/local/mnt/workspace/project/qnn/qairt/2.41.0')

    if not os.path.exists(qnn_sdk_root):
        print(f"[ERROR] QAIRT_SDK_ROOT path does not exist: {qnn_sdk_root}", file=sys.stderr)
        sys.exit(1)

    model_info = get_model_info(abs_onnx, args.act_bw, args.weight_bw, args.output_root)
    run_env = os.environ.copy()
    python_exe = sys.executable or "python"

    # Start target handoff
    if args.flow == "QNN":
        print(f"\n--- Initiating QAIRT QNN Handoff ---")
        abs_cpp = os.path.abspath(model_info["cpp_path"])
        abs_bin = os.path.abspath(model_info["bin_path"])
        abs_output_dir = os.path.abspath(model_info["output_dir_path"])

        converter_path = os.path.join(qnn_sdk_root, "bin", host_toolchain, "qnn-onnx-converter")
        converter_cmd = [
            python_exe, converter_path,
            "--input_network", abs_onnx,
            "--quantization_overrides", abs_overrides,
            "--output_path", abs_cpp,
            "--act_bw", str(args.act_bw),
            "--weight_bw", str(args.weight_bw),
            "--bias_bw", "32",
            "--use_per_channel_quantization"
        ]

        # Preserve IO parameters
        if args.preserve_io_mode == "datatype":
            converter_cmd.extend(["--preserve_io"])
        elif args.preserve_io_mode == "layout":
            converter_cmd.extend(["--preserve_io", "layout"])

        # Input dimension overrides
        for name, shape in inputs_info.items():
            shape_csv = ",".join(str(s) for s in shape)
            converter_cmd.extend(["-d", name, shape_csv])

        print(f"Running qnn-onnx-converter with AIMET scale overrides...")
        print(f"Command: {' '.join(converter_cmd)}")
        subprocess.run(converter_cmd, check=True, env=run_env)

        # Fix Windows extension bug if any
        if not os.path.exists(abs_cpp):
            cpp_no_ext = abs_cpp.replace('.cpp', '')
            if os.path.exists(cpp_no_ext) and not cpp_no_ext.endswith('.bin'):
                os.rename(cpp_no_ext, abs_cpp)

        # Compile model library
        print(f"\nRunning qnn-model-lib-generator...")
        lib_gen_path = os.path.join(qnn_sdk_root, "bin", host_toolchain, "qnn-model-lib-generator")
        lib_gen_cmd = [
            python_exe, lib_gen_path,
            "-c", abs_cpp,
            "-b", abs_bin,
            "-o", abs_output_dir,
            "-t", device_toolchain
        ]
        print(f"Command: {' '.join(lib_gen_cmd)}")
        subprocess.run(lib_gen_cmd, check=True, env=run_env)

        # Cleanup
        if args.cleanup:
            for item in [abs_cpp, abs_bin, abs_cpp.replace('.cpp', '_net.json')]:
                if os.path.exists(item):
                    os.remove(item)
            # Remove tmp_ compile directories
            parent_dir = os.path.dirname(abs_cpp)
            for entry in os.scandir(parent_dir):
                if entry.is_dir() and entry.name.startswith("tmp_"):
                    try:
                        shutil.rmtree(entry.path)
                    except OSError:
                        pass

    elif args.flow == "SNPE":
        print(f"\n--- Initiating QAIRT SNPE Handoff ---")
        base_out_dir = os.path.abspath(args.output_root) if args.output_root else os.path.dirname(abs_onnx)
        dlc_filename = f"{Path(abs_onnx).stem}_aimet_a{args.act_bw}_w{args.weight_bw}.dlc"
        abs_dlc = os.path.join(base_out_dir, dlc_filename)

        snpe_converter_path = os.path.join(qnn_sdk_root, "bin", host_toolchain, "snpe-onnx-to-dlc")
        snpe_cmd = [
            python_exe, snpe_converter_path,
            "--input_network", abs_onnx,
            "--quantization_overrides", abs_overrides,
            "-o", abs_dlc
        ]

        # Add inputs
        for name, shape in inputs_info.items():
            shape_csv = ",".join(str(s) for s in shape)
            snpe_cmd.extend(["-d", name, shape_csv])

        print(f"Running snpe-onnx-to-dlc with AIMET scale overrides...")
        print(f"Command: {' '.join(snpe_cmd)}")
        subprocess.run(snpe_cmd, check=True, env=run_env)

    print(f"\n[OK] AIMET flow conversion successfully finalized!")

if __name__ == '__main__':
    main()
