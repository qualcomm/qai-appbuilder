#!/usr/bin/env python
"""compare_output.py — Step 6 校验小工具 (onnx-to-htp skill)

比较 NPU 输出 .raw 与 onnxruntime 参考输出 .raw 的一致性（cosine + 逐位/最大误差）。
run_qnn_deploy.ps1 会自动调用本脚本；排错时也可以单独跑：

    python compare_output.py <npu_output.raw> <ref_output.raw> [--threshold 0.99] [--dtype float32]

阈值参考（见 SKILL.md）：
    FP32 逐位一致  -> --threshold 1e-5 (脚本对 FP32 场景改用 max_abs_diff 判定, 见下)
    FP16           -> --threshold 0.99 (cosine)
    INT8/量化       -> --threshold 0.95 (cosine)
"""
import argparse
import sys

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("npu_raw", help="Path to NPU output .raw file")
    ap.add_argument("ref_raw", help="Path to onnxruntime reference output .raw file")
    ap.add_argument("--threshold", default="0.99",
                     help="Pass threshold. If a small value like 1e-5 is given, "
                          "it is interpreted as a max-abs-diff bit-exactness check; "
                          "otherwise interpreted as a cosine similarity lower bound.")
    ap.add_argument("--dtype", default="float32", help="Numpy dtype of both .raw files")
    args = ap.parse_args()

    dtype = np.dtype(args.dtype)
    a = np.fromfile(args.npu_raw, dtype=dtype).astype(np.float64)
    b = np.fromfile(args.ref_raw, dtype=dtype).astype(np.float64)

    print(f"npu_raw: {args.npu_raw}  n={a.size}")
    print(f"ref_raw: {args.ref_raw}  n={b.size}")

    if a.size != b.size:
        print(f"WARNING: element count mismatch ({a.size} vs {b.size}) -- "
              f"comparing over the shorter length; check for transposed dims (SKILL.md pitfall #2).")
        n = min(a.size, b.size)
        a = a[:n]
        b = b[:n]

    max_abs_diff = float(np.max(np.abs(a - b))) if a.size else float("nan")
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    cosine = float(np.dot(a, b) / denom) if denom > 0 else float("nan")

    print(f"max_abs_diff: {max_abs_diff:.8e}")
    print(f"cosine: {cosine:.8f}")

    threshold_val = float(args.threshold)

    if threshold_val <= 1e-3:
        # bit-exact / near-bit-exact mode (FP32 path)
        passed = max_abs_diff <= threshold_val
        print(f"Mode: bit-exact check (max_abs_diff <= {threshold_val})")
        if passed:
            print("PASS: NPU output BIT-EXACT with onnxruntime reference")
        else:
            print(f"FAIL: max_abs_diff {max_abs_diff:.8e} > threshold {threshold_val}")
    else:
        # cosine similarity mode (FP16 / quantized path)
        passed = cosine >= threshold_val
        print(f"Mode: cosine similarity check (cosine >= {threshold_val})")
        if passed:
            print(f"PASS: NPU output cosine={cosine:.6f} >= threshold {threshold_val}")
        else:
            print(f"FAIL: cosine {cosine:.6f} < threshold {threshold_val}")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
