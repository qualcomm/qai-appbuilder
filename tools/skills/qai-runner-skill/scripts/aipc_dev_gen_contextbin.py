# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
QNN Context Binary Generator

Generates hardware-specific context binaries for QNN models.
Required for Windows ARM64 deployment with HTP runtime.

Usage:
  # Linux - input: libmodel.so
  python aipc_dev_gen_contextbin.py --model libmodel.so --output libmodel.so.bin

  # Windows - input: model.dll
  python aipc_dev_gen_contextbin.py --model model.dll --output model.dll.bin

  # With profiling
  python aipc_dev_gen_contextbin.py --model model.dll --output model.dll.bin --profiling

Note:
  - Windows ARM64: Context binary is REQUIRED for inference
  - Linux: Optional, use for specific SoC deployment without on-device compilation
  - Input must be absolute path
  - Output = input filename + '.bin' postfix

Args:
  --model, --model_lib: Path to .dll or .so file
  --output: Output path for context binary (default: <model>.bin)
  --profiling: Enable HTP optrace profiling
"""

import subprocess
import argparse
import os
import sys
import platform
import shutil

def run_generator(model_path, output_path=None, profiling=False):
    sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not sdk_root:
        print("Error: QAIRT_SDK_ROOT environment variable is not set.")
        sys.exit(1)

    system = platform.system().lower()
    if system == "linux":
        arch_dir = "x86_64-linux-clang"
        generator_exe = "qnn-context-binary-generator"
        backend_name = "libQnnHtp.so"
    elif system == "windows":
        arch_dir = "aarch64-windows-msvc"
        generator_exe = "qnn-context-binary-generator.exe"
        backend_name = "QnnHtp.dll"
    else:
        arch_dir = "x86_64-linux-clang"
        generator_exe = "qnn-context-binary-generator"
        backend_name = "libQnnHtp.so"

    generator_path = os.path.join(sdk_root, "bin", arch_dir, generator_exe)
    backend_path = os.path.join(sdk_root, "lib", arch_dir, backend_name)

    if not os.path.exists(generator_path):
        print(f"Error: generator not found at {generator_path}")
        sys.exit(2)
    if not os.path.exists(backend_path):
        print(f"Error: backend library not found at {backend_path}")
        sys.exit(2)

    base = os.path.basename(model_path)
    name_without_ext = os.path.splitext(os.path.splitext(base)[0])[0]
    binary_name = os.path.basename(output_path) if output_path else name_without_ext
    
    command = [generator_path, "--backend", backend_path, "--model", model_path, "--binary_file", binary_name]
    
    if profiling:
        command.extend(["--profiling_level", "detailed", "--profiling_option", "optrace"])

    print(f"Executing: {' '.join(command)}")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

    actual_output = os.path.join("output", binary_name)
    if os.path.exists(actual_output):
        if output_path:
            os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
            shutil.move(actual_output, output_path)
            print(f"Output: {os.path.abspath(output_path)}")
        else:
            print(f"Output: {os.path.abspath(actual_output)}")
    else:
        files = []
        for root, dirs, filenames in os.walk("output"):
            for f in filenames:
                if f.endswith(".bin"):
                    files.append(os.path.join(root, f))
        if files:
            actual_output = files[0]
            if output_path:
                os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
                shutil.move(actual_output, output_path)
                print(f"Output: {os.path.abspath(output_path)}")
            else:
                print(f"Output: {os.path.abspath(actual_output)}")
        else:
            print("Error: Output file not found")
            sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QNN Context Binary Generator")
    parser.add_argument("--model", "--model_lib", dest="model", required=True, 
                        help="Path to the model .dll/.so file")
    parser.add_argument("--output", help="Output path for context binary")
    parser.add_argument("--profiling", action="store_true", help="Enable HTP optrace profiling")

    args = parser.parse_args()

    run_generator(args.model, args.output, args.profiling)
