#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import runpy
import sys


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

    import qai_onnxruntime as _ort  # noqa: PLC0415

    # Hot-patch: make `import onnxruntime as ort` resolve to the QNN wrapper.
    sys.modules["onnxruntime"] = _ort

    sys.argv = [args.script, *args.script_args]
    runpy.run_path(args.script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
