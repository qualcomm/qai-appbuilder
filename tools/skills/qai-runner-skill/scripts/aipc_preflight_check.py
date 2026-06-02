# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#!/usr/bin/env python3
"""
AIPC Preflight Check Script

Validates environment and model before QNN/SNPE conversion.

Usage:
  python aipc_preflight_check.py --onnx model.onnx --target-arch windows-aarch64
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import onnx
except ImportError:
    print("Warning: onnx not installed. Skipping ONNX validation.")
    onnx = None


def check_qairt_env():
    """Check QAIRT SDK environment is set up."""
    sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not sdk_root:
        print("❌ QAIRT_SDK_ROOT not set")
        print("   Solution: Run aienv.ps1 (Windows) or source aienv.sh (Linux)")
        return False
    
    print(f"✓ QAIRT_SDK_ROOT: {sdk_root}")
    
    # Check converter tools exist
    import platform
    if platform.system().lower() == "windows":
        arch_dir = "x86_64-windows-msvc"
        converter = os.path.join(sdk_root, "bin", arch_dir, "qnn-onnx-converter")
        libgen = os.path.join(sdk_root, "bin", arch_dir, "qnn-model-lib-generator")
    else:
        arch_dir = "x86_64-linux-clang"
        converter = os.path.join(sdk_root, "bin", arch_dir, "qnn-onnx-converter")
        libgen = os.path.join(sdk_root, "bin", arch_dir, "qnn-model-lib-generator")
    
    if not os.path.exists(converter):
        print(f"❌ Converter not found: {converter}")
        return False
    print(f"✓ Converter: {converter}")
    
    if not os.path.exists(libgen):
        print(f"❌ Library generator not found: {libgen}")
        return False
    print(f"✓ Library generator: {libgen}")
    
    return True


def check_onnx_model(onnx_path):
    """Validate ONNX model file."""
    if not os.path.exists(onnx_path):
        print(f"❌ ONNX file not found: {onnx_path}")
        return False
    
    print(f"✓ ONNX file: {onnx_path}")
    
    # Check file size
    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    print(f"  Size: {size_mb:.2f} MB")
    
    if onnx is None:
        print("⚠ Skipping ONNX validation (onnx not installed)")
        return True
    
    try:
        model = onnx.load(onnx_path)
        onnx.checker.check_model(model)
        print("✓ ONNX model validation passed")
    except Exception as e:
        print(f"❌ ONNX validation failed: {e}")
        return False
    
    # Check for dynamic inputs
    has_dynamic = False
    for inp in model.graph.input:
        dims = inp.type.tensor_type.shape.dim
        for i, d in enumerate(dims):
            if d.dim_value == 0:
                has_dynamic = True
                print(f"⚠ Dynamic input detected: {inp.name}[{i}]")
    
    if has_dynamic:
        print("⚠ Model has dynamic inputs - use --input-dims during conversion")
    
    # Print I/O info
    print(f"  Inputs: {[i.name for i in model.graph.input]}")
    print(f"  Outputs: {[o.name for o in model.graph.output]}")
    
    return True


def check_output_dir(output_dir):
    """Check output directory is writable."""
    os.makedirs(output_dir, exist_ok=True)
    
    test_file = os.path.join(output_dir, ".write_test")
    try:
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        print(f"✓ Output directory writable: {output_dir}")
        return True
    except Exception as e:
        print(f"❌ Output directory not writable: {e}")
        return False


def check_disk_space(output_dir, min_mb=500):
    """Check sufficient disk space."""
    try:
        import shutil
        stat = shutil.disk_usage(output_dir)
        free_mb = stat.free / (1024 * 1024)
        
        if free_mb < min_mb:
            print(f"❌ Insufficient disk space: {free_mb:.0f} MB free (need {min_mb} MB)")
            return False
        
        print(f"✓ Disk space: {free_mb:.0f} MB available")
        return True
    except Exception as e:
        print(f"⚠ Could not check disk space: {e}")
        return True  # Don't fail on this


def main():
    parser = argparse.ArgumentParser(description="AIPC Preflight Check")
    parser.add_argument("--onnx", required=True, help="Path to ONNX model")
    parser.add_argument("--output-dir", default="./qairt_output", 
                        help="Output directory")
    parser.add_argument("--target-arch", default="windows-aarch64",
                        help="Target architecture")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("AIPC Preflight Check")
    print("=" * 60)
    print(f"ONNX Model: {args.onnx}")
    print(f"Output Dir: {args.output_dir}")
    print(f"Target Arch: {args.target_arch}")
    print("=" * 60)
    print()
    
    checks = [
        ("QAIRT Environment", check_qairt_env),
        ("ONNX Model", lambda: check_onnx_model(args.onnx)),
        ("Output Directory", lambda: check_output_dir(args.output_dir)),
        ("Disk Space", lambda: check_disk_space(args.output_dir)),
    ]
    
    results = []
    for name, check_fn in checks:
        print(f"\n[{name}]")
        result = check_fn()
        results.append((name, result))
    
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    
    all_passed = all(r for _, r in results)
    for name, result in results:
        status = "✓ PASS" if result else "❌ FAIL"
        print(f"  {name}: {status}")
    
    print("=" * 60)
    
    if all_passed:
        print("\n✓ All checks passed. Ready for conversion.")
        print("\nNext step:")
        print(f"  python aipc_convert_fp.py --onnx {args.onnx} --output_dir {args.output_dir} --precision 16")
        return 0
    else:
        print("\n❌ Some checks failed. Please fix issues before proceeding.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
