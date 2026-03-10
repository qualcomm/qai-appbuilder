# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import subprocess
import argparse
import os
import sys
import platform

def run_generator(model_so_path):
    # Get QAIRT_SDK_ROOT from environment
    sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not sdk_root:
        print("Error: QAIRT_SDK_ROOT environment variable is not set.")
        sys.exit(1)

    # Select toolchain based on host OS
    system = platform.system().lower()
    if system == "linux":
        arch_dir = "x86_64-linux-clang"  # Use x86_64 for compatibility
        generator_exe = "qnn-context-binary-generator"
        backend_name = "libQnnHtp.so"  # Use HTP backend for QNN inference
    elif system == "windows":
        arch_dir = "x86_64-windows-msvc"  # Use x86_64 for compatibility on Windows
        arch_dir = "aarch64-windows-msvc"
        generator_exe = "qnn-context-binary-generator.exe"
        backend_name = "QnnHtp.dll"  # Use HTP backend for QNN inference (Windows DLL)
    else:
        # default to x86_64 Linux toolchain if unknown
        arch_dir = "x86_64-linux-clang"
        generator_exe = "qnn-context-binary-generator"
        backend_name = "libQnnHtp.so"

    generator_path = os.path.join(sdk_root, "bin", arch_dir, generator_exe)
    backend_path = os.path.join(sdk_root, "lib", arch_dir, backend_name)

    # Validate expected files
    if not os.path.exists(generator_path):
        print(f"Error: generator not found at {generator_path}")
        sys.exit(2)
    if not os.path.exists(backend_path):
        print(f"Error: backend library not found at {backend_path}")
        sys.exit(2)

    # Construct the binary file output name
    # We append .bin to avoid overwriting the input .so file if they are in the same dir
    output_binary = f"./{os.path.basename(model_so_path)}.bin"

    # Construct command as list to avoid shell=True
    command = [generator_path, "--backend", backend_path, "--model", model_so_path, "--binary_file", output_binary]

    print(f"Executing: {' '.join(command)}")
    try:
        subprocess.run(command, check=True)
        print("Generation complete.")
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QNN Context Binary Generator")
    parser.add_argument("--model", required=True, help="Path to the model .so file (e.g., libyolov8n.so)")
    
    args = parser.parse_args()
    
    run_generator(args.model)
