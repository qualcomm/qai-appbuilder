# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
qai_runner.py — QAI ModelBuilder Inference Runner

Runs a user inference script with `onnxruntime` hot-patched to the
QAI/QNN wrapper (onnxwrapper.py), enabling existing onnxruntime-based
scripts to run on Qualcomm HTP without code changes.

Usage:
    python qai_runner.py path/to/inference_script.py [script_args...]

Example:
    python qai_runner.py infer_resnet.py --input image.jpg
    python qai_runner.py onnx_inference.py
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys

# UTF-8 safe stdout/stderr (model-builder): Windows consoles default to a
# legacy code page (GBK/cp1252) and crash with UnicodeEncodeError when a
# print() contains non-ASCII (e.g. emoji ✅ / 中文). Reconfigure here so both
# this runner and the hot-patched user script it runs print safely regardless
# of the console code page; this also removes any need for the fragile
# `set PYTHONUTF8=1 &&` cmd workaround.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a script with `onnxruntime` hot-patched to QAI/QNN wrapper."
    )
    parser.add_argument(
        "script",
        nargs="?",
        default="onnx_inference.py",
        help="Script to run (default: onnx_inference.py)",
    )
    parser.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the target script",
    )
    args = parser.parse_args()

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    if repo_dir not in sys.path:
        sys.path.insert(0, repo_dir)

    # --- Platform-aware wrapper check ---
    # On x86_64 Linux, onnxwrapper.py must be the x86 version (no qai_appbuilder).
    import platform as _platform
    _wrapper_path = os.path.join(repo_dir, "onnxwrapper.py")
    if (_platform.machine() == "x86_64" and sys.platform.startswith("linux")
            and os.path.exists(_wrapper_path)):
        with open(_wrapper_path, encoding="utf-8") as _wf:
            if "qai_appbuilder" in _wf.read():
                sys.exit(
                    "[qai_runner] ERROR: onnxwrapper.py is the ARM/Windows version (requires qai_appbuilder).\n"
                    "This host is x86_64 Linux. Use the x86 wrapper instead:\n"
                    "  cp factory/chat_features/model-builder/scripts/onnxwrapper_x86.py ./onnxwrapper.py"
                )

    import onnxwrapper as _ort

    # Hot-patch: make `import onnxruntime as ort` resolve to the QNN wrapper.
    sys.modules["onnxruntime"] = _ort

    sys.argv = [args.script, *args.script_args]
    runpy.run_path(args.script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
