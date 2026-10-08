#!/usr/bin/env python
"""validate_cosine.py — 通用两个 .npy 数组的 cosine/PSNR 比对小工具。

用法:
    python validate_cosine.py <a.npy> <b.npy> [--threshold 0.99]

用于排错阶段（例如比对 PyTorch baseline 输出和某个中间产物），不是 export_onnx_template.py
的必经步骤（那个脚本已经内置了校验逻辑）。与 mmengine-ckpt-to-htp/scripts/validate_cosine.py
是同一份工具，各自独立维护，方便每个 skill 目录自成一体。
"""
import argparse
import sys

import numpy as np


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = a.reshape(-1).astype(np.float64)
    b = b.reshape(-1).astype(np.float64)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return float("nan")
    return float(np.dot(a, b) / denom)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    mse = np.mean((a - b) ** 2)
    if mse == 0:
        return float("inf")
    data_range = max(a.max(), b.max()) - min(a.min(), b.min())
    if data_range == 0:
        return float("inf")
    return 20 * np.log10(data_range) - 10 * np.log10(mse)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="Path to first .npy")
    ap.add_argument("b", help="Path to second .npy")
    ap.add_argument("--threshold", type=float, default=0.99, help="Cosine pass threshold")
    args = ap.parse_args()

    a = np.load(args.a)
    b = np.load(args.b)

    print(f"a: shape={a.shape} dtype={a.dtype}")
    print(f"b: shape={b.shape} dtype={b.dtype}")

    if a.shape != b.shape:
        print(f"WARNING: shape mismatch {a.shape} vs {b.shape} -- attempting flatten compare anyway")

    cos = cosine(a, b)
    ps = psnr(a, b)
    print(f"\ncosine similarity: {cos:.8f}")
    print(f"PSNR: {ps:.4f} dB")
    if a.size == b.size:
        print(f"max abs diff: {np.max(np.abs(a.reshape(-1).astype(np.float64) - b.reshape(-1).astype(np.float64)))}")
    else:
        print("max abs diff: N/A (size mismatch)")

    if cos < args.threshold:
        print(f"\nFAIL: cosine {cos:.8f} < threshold {args.threshold}")
        sys.exit(1)
    print(f"\nPASS: cosine {cos:.8f} >= threshold {args.threshold}")


if __name__ == "__main__":
    main()
