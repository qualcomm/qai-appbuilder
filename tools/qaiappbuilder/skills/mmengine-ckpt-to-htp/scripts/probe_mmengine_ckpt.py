#!/usr/bin/env python
"""probe_mmengine_ckpt.py — Step 0 探测器：判断一个 .pth 是否为 mmengine 格式，
并打印 key 前缀直方图、meta 摘要、家族启发式猜测，帮助人工/agent 决定怎么写导出脚本。

用法:
    python probe_mmengine_ckpt.py <ckpt.pth> [--full]

--full: 打印全部 key 名（不截断），排查用。默认只打印前缀直方图。
"""
import argparse
import sys
from collections import Counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ckpt", help="Path to .pth checkpoint")
    ap.add_argument("--full", action="store_true", help="Print every full key name")
    args = ap.parse_args()

    try:
        import torch
    except ImportError:
        print("ERROR: torch not importable in this venv. Use python_x64_venv per SKILL.md.")
        sys.exit(1)

    print(f"Loading: {args.ckpt}")
    try:
        # weights_only=False: mmengine checkpoints commonly carry non-tensor
        # objects in `meta` (dict/list/str). Only do this for a trusted local file.
        ckpt = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    except TypeError:
        # older torch without weights_only kwarg
        ckpt = torch.load(args.ckpt, map_location="cpu")
    except Exception as e:
        print(f"ERROR: torch.load failed: {e}")
        sys.exit(1)

    print(f"\nTop-level type: {type(ckpt)}")
    if not isinstance(ckpt, dict):
        print("NOT a dict at top level -> NOT mmengine format (probably torch.save(model) or a raw state_dict object).")
        sys.exit(2)

    top_keys = list(ckpt.keys())
    print(f"Top-level keys: {top_keys}")

    is_mmengine = "meta" in ckpt and "state_dict" in ckpt
    print(f"\n=== mmengine format check ===")
    print(f"has 'meta' key:        {'meta' in ckpt}")
    print(f"has 'state_dict' key:  {'state_dict' in ckpt}")
    print(f"=> IS_MMENGINE_FORMAT: {is_mmengine}")

    if not is_mmengine:
        if any(k.count(".") > 0 for k in top_keys[:5] if isinstance(k, str)):
            print("\nLooks like a FLAT state_dict (no meta/state_dict wrapper) -> "
                  "this is a plain PyTorch checkpoint, not mmengine format. "
                  "This skill's Step0 stops here; treat top_keys directly as the state_dict.")
        sys.exit(0 if is_mmengine else 3)

    meta = ckpt.get("meta", {})
    sd = ckpt.get("state_dict", {})

    print(f"\n=== meta ===")
    if isinstance(meta, dict):
        for k, v in meta.items():
            v_repr = repr(v)
            if len(v_repr) > 300:
                v_repr = v_repr[:300] + " ...(truncated)"
            print(f"  {k}: {v_repr}")
    else:
        print(f"  (meta is not a dict: {type(meta)}) {repr(meta)[:300]}")

    print(f"\n=== state_dict ===")
    print(f"Total tensors: {len(sd)}")

    total_params = 0
    dtypes = Counter()
    prefixes = Counter()
    for k, v in sd.items():
        if hasattr(v, "numel"):
            total_params += v.numel()
        if hasattr(v, "dtype"):
            dtypes[str(v.dtype)] += 1
        # top-2-level prefix, e.g. "backbone.stem" from "backbone.stem.conv.weight"
        parts = k.split(".")
        prefix = ".".join(parts[:2]) if len(parts) > 1 else parts[0]
        prefixes[prefix] += 1

    print(f"Total params: {total_params:,}")
    print(f"Dtype distribution: {dict(dtypes)}")

    print(f"\n=== Key prefix histogram (top-2-level) ===")
    for prefix, count in sorted(prefixes.items(), key=lambda x: -x[1]):
        print(f"  {prefix:40s} : {count}")

    top1_prefixes = Counter(k.split(".")[0] for k in sd.keys())
    print(f"\n=== Key prefix histogram (top-1-level) ===")
    for prefix, count in sorted(top1_prefixes.items(), key=lambda x: -x[1]):
        print(f"  {prefix:30s} : {count}")

    if args.full:
        print(f"\n=== Full key list ===")
        for k in sd.keys():
            print(f"  {k}  shape={tuple(sd[k].shape) if hasattr(sd[k], 'shape') else '?'}")

    # --- Heuristic family guess ---
    print(f"\n=== Heuristic family guess ===")
    keys_joined = " ".join(sd.keys())
    guesses = []
    if "text_model" in keys_joined and "bbox_head" in keys_joined:
        guesses.append("YOLO-World (CLIP text tower + detection head) "
                        "-> STOP, use the dedicated `mmyolo-ckpt-to-htp` skill instead of this one.")
    elif "bbox_head" in keys_joined or "rpn_head" in keys_joined:
        guesses.append("mmdetection-style object detector (bbox_head/rpn_head present)")
    elif "decode_head" in keys_joined:
        guesses.append("mmsegmentation-style segmentor (decode_head present)")
    elif "backbone" in keys_joined and "head" in keys_joined and "neck" not in keys_joined:
        guesses.append("mmpretrain-style classifier (backbone+head, no neck)")
    elif any(k.startswith("generator.") for k in sd.keys()):
        guesses.append("GAN-style generator checkpoint (mmagic/mmediting family)")
    else:
        guesses.append("Unrecognized family by key heuristics -- inspect prefixes above manually, "
                        "and check meta for config/class info.")
    for g in guesses:
        print(f"  - {g}")

    # --- venv package check ---
    print(f"\n=== Installed packages in this venv (informational) ===")
    for pkg in ["mmengine", "mmcv", "mmdet", "mmyolo", "mmseg", "ultralytics", "timm", "transformers"]:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            print(f"  {pkg:15s} OK ({ver})")
        except ImportError:
            print(f"  {pkg:15s} NOT installed")

    print(f"\nDone. is_mmengine_format={is_mmengine}")


if __name__ == "__main__":
    main()
