#!/usr/bin/env python
"""probe_safetensors.py — Step 0 探测器：打印 .safetensors 文件的张量列表、
键名前缀直方图、metadata，并给出家族启发式猜测，帮助人工/agent 决定怎么写导出脚本。

用法:
    python probe_safetensors.py <weights.safetensors> [--full]

--full: 打印全部张量名+shape（不截断）。默认只打印前缀直方图。
"""
import argparse
import sys
from collections import Counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("weights", help="Path to .safetensors file")
    ap.add_argument("--full", action="store_true", help="Print every tensor name + shape")
    args = ap.parse_args()

    try:
        from safetensors import safe_open
    except ImportError:
        print("ERROR: safetensors not importable in this venv. Use python_x64_venv per SKILL.md.")
        sys.exit(1)

    print(f"Opening: {args.weights}")
    try:
        f = safe_open(args.weights, framework="pt", device="cpu")
    except Exception as e:
        print(f"ERROR: failed to open as safetensors: {e}")
        sys.exit(1)

    keys = list(f.keys())
    print(f"\nTotal tensors: {len(keys)}")

    # metadata (often carries hints like "format", "modelspec.*", training config)
    try:
        metadata = f.metadata()
    except Exception:
        metadata = None
    print(f"\n=== metadata ===")
    if metadata:
        for k, v in metadata.items():
            v_repr = repr(v)
            if len(v_repr) > 300:
                v_repr = v_repr[:300] + " ...(truncated)"
            print(f"  {k}: {v_repr}")
    else:
        print("  (none)")

    total_params = 0
    dtypes = Counter()
    prefixes_1 = Counter()
    prefixes_2 = Counter()
    shapes = {}

    for k in keys:
        try:
            tensor = f.get_tensor(k)
            shape = tuple(tensor.shape)
            dtype = str(tensor.dtype)
            numel = tensor.numel()
        except Exception as e:
            shape, dtype, numel = ("?",), f"ERR:{e}", 0

        shapes[k] = (shape, dtype)
        total_params += numel
        dtypes[dtype] += 1

        parts = k.split(".")
        prefixes_1[parts[0]] += 1
        prefixes_2[".".join(parts[:2]) if len(parts) > 1 else parts[0]] += 1

    print(f"\nTotal params: {total_params:,}")
    print(f"Dtype distribution: {dict(dtypes)}")

    print(f"\n=== Key prefix histogram (top-1-level) ===")
    for prefix, count in sorted(prefixes_1.items(), key=lambda x: -x[1]):
        print(f"  {prefix:30s} : {count}")

    print(f"\n=== Key prefix histogram (top-2-level) ===")
    for prefix, count in sorted(prefixes_2.items(), key=lambda x: -x[1]):
        print(f"  {prefix:40s} : {count}")

    if args.full:
        print(f"\n=== Full tensor list ===")
        for k in keys:
            shape, dtype = shapes[k]
            print(f"  {k}  shape={shape} dtype={dtype}")

    # --- Heuristic family guess ---
    print(f"\n=== Heuristic family guess ===")
    keys_joined = " ".join(keys)
    guesses = []
    if ("rdb1" in keys_joined and "rdb2" in keys_joined and "rdb3" in keys_joined) or \
       ("conv_first" in keys_joined and "conv_up1" in keys_joined):
        guesses.append("Real-ESRGAN x4plus / RRDBNet "
                        "-> STOP, use the dedicated `realesrgan-safetensors-to-htp` skill instead of this one.")
    elif any(k.startswith("model.diffusion_model.") for k in keys) or "time_embed" in keys_joined:
        guesses.append("Diffusers-style UNet (denoising model) -- likely one component of a larger pipeline; "
                        "check for sibling .safetensors files (VAE / text encoder).")
    elif any("first_stage_model" in k or "encoder.down" in k and "decoder.up" in keys_joined for k in keys):
        guesses.append("VAE-style encoder/decoder (diffusers component)")
    elif any(k.startswith("transformer.") or k.startswith("encoder.layer.") for k in keys):
        guesses.append("Transformer/BERT-style architecture (transformers-lib compatible naming)")
    elif any("layer1" in k or "layer2" in k or "layer3" in k or "layer4" in k for k in keys) and \
         any("conv1" in k for k in keys):
        guesses.append("ResNet-style CNN (torchvision-compatible naming: layer1-4, conv1)")
    elif any(k.startswith("blocks.") for k in keys) and any("attn" in k for k in keys):
        guesses.append("ViT/timm-style transformer backbone (blocks.N.attn naming)")
    else:
        guesses.append("Unrecognized family by key heuristics -- inspect prefixes above manually, "
                        "and check metadata for model/format hints. "
                        "Consider searching the exact key strings online to identify the source repo.")
    for g in guesses:
        print(f"  - {g}")

    # --- venv package check ---
    print(f"\n=== Installed packages in this venv (informational) ===")
    for pkg in ["safetensors", "transformers", "diffusers", "timm", "torch", "torchvision"]:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            print(f"  {pkg:15s} OK ({ver})")
        except ImportError:
            print(f"  {pkg:15s} NOT installed")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
