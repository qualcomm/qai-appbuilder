# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

#!/usr/bin/env python3
"""
AIPC SNPE Model Converter
Converts ONNX models to SNPE DLC format using qairt-converter
Supports multiple platforms: Windows, Linux (x86_64), and macOS
"""

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path


def detect_host_toolchain():
    """
    Detects the appropriate host toolchain based on the current platform.
    
    Returns:
        str: The appropriate toolchain directory name for the current platform
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "windows":
        if machine.lower() in ["amd64", "x86_64", "x64"]:
            return "x86_64-windows-msvc"
        elif machine.lower() in ["arm64", "aarch64"]:
            return "arm64x-windows-msvc"
    elif system == "linux":
        if machine.lower() in ["amd64", "x86_64", "x64"]:
            return "x86_64-linux-clang"
        elif machine.lower() in ["arm64", "aarch64"]:
            return "aarch64-linux-clang"

    
    # Default fallback
    return "x86_64-linux-clang"


def convert_onnx_to_snpe(
    sdk_root: str,
    host_arch: str,
    onnx_path: str,
    output_path: str,
    float_bitwidth: int = 16,
    preserve_io: bool = True,
    source_model_input_shapes=None
) -> int:
    """
    Converts ONNX model to SNPE DLC format using qairt-converter.

    Args:
        sdk_root (str): Path to the QNN SDK root directory.
        host_arch (str): Host architecture (e.g., "x86_64-windows-msvc").
        onnx_path (str): Path to the input ONNX model.
        output_path (str): Path for the output DLC file.
        float_bitwidth (int): Floating point bitwidth (16 or 32).
        preserve_io (bool): Whether to preserve input/output layouts.
        source_model_input_shapes (list[tuple[str, str]] | None): Optional list of
            (input_name, dims) pairs to pass as --source_model_input_shape for
            dynamic-input ONNX models.

    Returns:
        int: 0 if successful, non-zero otherwise.
    """
    if not sdk_root:
        raise ValueError("QAIRT_SDK_ROOT environment variable not set.")

    qairt_converter = os.path.join(sdk_root, "bin", host_arch, "qairt-converter")
    
    if not os.path.exists(qairt_converter):
        # Try with .exe extension on Windows
        if platform.system().lower() == "windows":
            qairt_converter += ".exe"
            if not os.path.exists(qairt_converter):
                raise FileNotFoundError(f"qairt-converter not found at: {qairt_converter}")
        else:
            raise FileNotFoundError(f"qairt-converter not found at: {qairt_converter}")

    # Validate input file
    if not os.path.exists(onnx_path):
        print(f"Error: ONNX file not found: {onnx_path}")
        return 1

    # Determine the output directory for the DLC file
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    print(f"Converting {onnx_path} to SNPE DLC format...")
    print(f"Using converter: {qairt_converter}")
    print(f"Output path: {output_path}")
    print(f"Float bitwidth: {float_bitwidth}")

    # Build the command
    converter_command = [
        sys.executable, qairt_converter,
        "--input_network", onnx_path,
        "--output_path", output_path,
        "--float_bitwidth", str(float_bitwidth)
    ]

    if source_model_input_shapes:
        for input_name, dims in source_model_input_shapes:
            converter_command.extend(
                ["--source_model_input_shape", input_name, dims]
            )
    
    if preserve_io:
        converter_command.extend(["--preserve_io"])

    try:
        result = subprocess.run(converter_command, check=True, capture_output=True, text=True)
        print("Conversion completed successfully!")
        print(result.stdout)
        return 0
    except subprocess.CalledProcessError as e:
        print(f"Error during conversion: {e}")
        print(f"Error output: {e.stderr}")
        return e.returncode
    except Exception as e:
        print(f"Unexpected error during conversion: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Convert ONNX models to SNPE DLC format using qairt-converter")
    parser.add_argument(
        "--input", 
        required=True, 
        help="Path to the input ONNX model file"
    )
    parser.add_argument(
        "--output", 
        required=True, 
        help="Path for the output DLC file"
    )
    parser.add_argument(
        "--arch", 
        default=None, 
        help="Host architecture (e.g., x86_64-windows-msvc). If not specified, auto-detected."
    )
    parser.add_argument(
        "--bitwidth", 
        type=int, 
        choices=[16, 32], 
        default=16, 
        help="Floating point bitwidth: 16 (default) or 32"
    )
    parser.add_argument(
        "--sdk-root", 
        default=None, 
        help="Path to QNN SDK root. If not specified, uses QAIRT_SDK_ROOT environment variable."
    )
    parser.add_argument(
        "--no-preserve-io", 
        action="store_true", 
        help="Do not preserve input/output layouts"
    )
    parser.add_argument(
        "--source-model-input-shape",
        nargs=2,
        action="append",
        metavar=("INPUT_NAME", "DIMS"),
        help=(
            "Optional dynamic input shape override. Can be specified multiple times. "
            "Example: --source-model-input-shape images 1,3,640,640"
        ),
    )

    args = parser.parse_args()

    # Determine SDK root
    sdk_root = args.sdk_root or os.environ.get("QAIRT_SDK_ROOT") or os.environ.get("QNN_SDK_ROOT")
    if not sdk_root:
        print("Error: QAIRT_SDK_ROOT or QNN_SDK_ROOT environment variable not set.")
        print("Please set the environment variable or use --sdk-root parameter.")
        sys.exit(1)

    # Validate SDK root
    if not os.path.exists(sdk_root):
        print(f"Error: SDK root directory does not exist: {sdk_root}")
        sys.exit(1)

    # Determine host architecture
    host_arch = args.arch
    if not host_arch:
        host_arch = detect_host_toolchain()
        print(f"Auto-detected host architecture: {host_arch}")

    # Validate host architecture exists in SDK
    host_arch_path = os.path.join(sdk_root, "bin", host_arch)
    if not os.path.exists(host_arch_path):
        print(f"Error: Host architecture path does not exist: {host_arch_path}")
        print(f"Available architectures in {os.path.join(sdk_root, 'bin')}:")
        available_archs = [d for d in os.listdir(os.path.join(sdk_root, "bin")) 
                          if os.path.isdir(os.path.join(sdk_root, "bin", d))]
        for arch in available_archs:
            print(f"  - {arch}")
        sys.exit(1)

    # Perform the conversion
    result = convert_onnx_to_snpe(
        sdk_root=sdk_root,
        host_arch=host_arch,
        onnx_path=args.input,
        output_path=args.output,
        float_bitwidth=args.bitwidth,
        preserve_io=not args.no_preserve_io,
        source_model_input_shapes=args.source_model_input_shape,
    )

    sys.exit(result)


if __name__ == "__main__":
    main()
