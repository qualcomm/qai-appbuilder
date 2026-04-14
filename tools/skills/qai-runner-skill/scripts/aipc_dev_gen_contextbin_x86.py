# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _arch_dir_for_host() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "aarch64-windows-msvc" if "arm" in machine else "x86_64-windows-msvc"
    if system == "linux":
        return "aarch64-linux-gcc" if "aarch64" in machine or "arm" in machine else "x86_64-linux-clang"
    return "x86_64-linux-clang"


def _backend_library_name(backend: str, is_windows: bool) -> str:
    if is_windows:
        return "QnnHtp.dll" if backend == "htp" else "QnnCpu.dll"
    return "libQnnHtp.so" if backend == "htp" else "libQnnCpu.so"


def _qnn_model_library_for_dlc(is_windows: bool) -> str:
    return "QnnModelDlc.dll" if is_windows else "libQnnModelDlc.so"


def _normalize_binary_basename(output_path: str | None, input_path: str) -> str:
    if output_path:
        name = Path(output_path).name
    else:
        model_name = Path(input_path).name
        # Keep legacy naming behavior from original script but avoid .bin.bin
        name = Path(model_name).stem

    if name.lower().endswith(".bin"):
        name = name[:-4]
    return name


def run_generator(
    model_path: str | None = None,
    dlc_path: str | None = None,
    output_path: str | None = None,
    backend: str = "htp",
    profiling: bool = False,
) -> None:
    sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not sdk_root:
        print("Error: QAIRT_SDK_ROOT environment variable is not set.")
        sys.exit(1)

    if bool(model_path) == bool(dlc_path):
        print("Error: provide exactly one of --model or --dlc.")
        sys.exit(2)

    arch_dir = _arch_dir_for_host()
    is_windows = platform.system().lower() == "windows"
    generator_exe = "qnn-context-binary-generator.exe" if is_windows else "qnn-context-binary-generator"

    generator_path = os.path.join(sdk_root, "bin", arch_dir, generator_exe)
    backend_lib = _backend_library_name(backend, is_windows)
    backend_path = os.path.join(sdk_root, "lib", arch_dir, backend_lib)

    if not os.path.exists(generator_path):
        print(f"Error: generator not found at {generator_path}")
        sys.exit(2)
    if not os.path.exists(backend_path):
        print(f"Error: backend library not found at {backend_path}")
        sys.exit(2)

    command = [generator_path, "--backend", backend_path]
    input_for_naming = ""

    if model_path:
        model_path = os.path.abspath(model_path)
        if not os.path.exists(model_path):
            print(f"Error: model not found: {model_path}")
            sys.exit(2)
        input_for_naming = model_path
        command.extend(["--model", model_path])
    else:
        dlc_path = os.path.abspath(dlc_path or "")
        if not os.path.exists(dlc_path):
            print(f"Error: dlc not found: {dlc_path}")
            sys.exit(2)
        dlc_model_lib = os.path.join(sdk_root, "lib", arch_dir, _qnn_model_library_for_dlc(is_windows))
        if not os.path.exists(dlc_model_lib):
            print(f"Error: DLC model library not found at {dlc_model_lib}")
            sys.exit(2)
        input_for_naming = dlc_path
        command.extend(["--model", dlc_model_lib, "--dlc_path", dlc_path])

    binary_name = _normalize_binary_basename(output_path, input_for_naming)
    command.extend(["--binary_file", binary_name])

    if profiling:
        command.extend(["--profiling_level", "detailed", "--profiling_option", "optrace"])

    print(f"Host arch dir: {arch_dir}")
    print(f"Backend: {backend}")
    print(f"Input type: {'dlc' if dlc_path else 'model-lib'}")
    print(f"Executing: {' '.join(command)}")

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        print(f"Error executing command: {error}")
        sys.exit(1)

    generated = os.path.join("output", f"{binary_name}.bin")
    if not os.path.exists(generated):
        found = sorted(str(path) for path in Path("output").rglob("*.bin")) if os.path.isdir("output") else []
        if not found:
            print("Error: output context binary not found in ./output")
            sys.exit(1)
        generated = found[0]

    if output_path:
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        shutil.move(generated, output_path)
        print(f"Output: {output_path}")
    else:
        print(f"Output: {os.path.abspath(generated)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QNN Context Binary Generator (x86/CPU+HTP aware)")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--model", help="Path to the model .dll/.so file")
    input_group.add_argument("--dlc", help="Path to the input .dlc file")
    parser.add_argument("--output", help="Output path for context binary")
    parser.add_argument("--backend", choices=["htp", "cpu"], default="htp", help="QNN backend library to use")
    parser.add_argument("--profiling", action="store_true", help="Enable HTP optrace profiling")

    args = parser.parse_args()
    run_generator(args.model, args.dlc, args.output, args.backend, args.profiling)
