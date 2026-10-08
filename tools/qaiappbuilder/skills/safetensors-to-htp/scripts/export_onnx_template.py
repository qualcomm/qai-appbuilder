#!/usr/bin/env python
"""export_onnx_template.py — Step 1 导出脚本骨架 (safetensors-to-htp skill)

用法:
    python export_onnx_template.py --weights <weights.safetensors> --out <out.onnx> --input_shape 1,3,512,512

只需要改 build_model() 这一个函数（按 probe_safetensors.py 探测到的键名前缀/家族猜测填写）。
其余部分（导出参数、cosine 校验）已经按 model-builder 的
references/model_export_validation.md 规范写好，不要改：
  - FP32 only（永不 FP16）
  - opset_version=18
  - do_constant_folding=False
  - 导出后自动做 PyTorch vs ONNX (onnxruntime CPUExecutionProvider) cosine 校验，阈值 >=0.9999

注：这份模板与 mmengine-ckpt-to-htp/scripts/export_onnx_template.py 的导出/校验逻辑
是同一套规范，各自独立维护（一个从 .pth 的 state_dict 加载，一个从 .safetensors 加载）。
"""
import argparse
import sys
import warnings

import numpy as np
import torch


# ============================================================
# >>> EDIT THIS FUNCTION ONLY <<<
# ============================================================
def build_model(weights_path: str) -> torch.nn.Module:
    """Load the .safetensors weights and return an eval()-mode nn.Module
    whose forward(x) takes the ONNX input tensor and returns the ONNX
    output tensor(s).

    Steps to fill this in:
      1. Run probe_safetensors.py <weights_path> --full first to see the
         key prefixes and shapes.
      2. safetensors has NO graph/class info -- you must know (or guess
         from key naming) which model class this belongs to:
         - transformers/timm-compatible naming -> try loading the matching
           class from that library directly (lowest effort).
         - custom/unrecognized naming -> hand-write an nn.Module whose
           submodule names match the key prefixes.
      3. Load with strict=False and PRINT missing/unexpected keys --
         both lists MUST be empty (or only contain buffers you know are
         safe) before you trust the export.
      4. Disable any training-only branch (dropout etc.) and call
         model.eval().

    Reference implementation to read (do NOT copy blindly -- Real-ESRGAN
    specific): <skills_jygpu>/realesrgan-safetensors-to-htp/scripts/ (hand-rolled
    RRDBNet, avoids the basicsr dependency).
    """
    from safetensors.torch import load_file

    state_dict = load_file(weights_path)

    raise NotImplementedError(
        "Fill in build_model(): construct your nn.Module, then:\n"
        "  missing, unexpected = model.load_state_dict(state_dict, strict=False)\n"
        "  print('missing:', missing)\n"
        "  print('unexpected:', unexpected)\n"
        "  assert not missing and not unexpected, 'key mismatch -- fix the module structure'\n"
        "  model.eval()\n"
        "  return model"
    )


# ============================================================
# Everything below follows model-builder/references/model_export_validation.md
# Do not change unless you have a specific, documented reason.
# ============================================================

def export_and_validate(model: torch.nn.Module, input_shape: tuple, out_path: str):
    dummy = torch.zeros(*input_shape, dtype=torch.float32)

    model.eval()
    with torch.no_grad():
        torch_out = model(dummy)
    if isinstance(torch_out, (tuple, list)):
        torch_out_list = list(torch_out)
    else:
        torch_out_list = [torch_out]
    torch_out_np = [t.detach().cpu().numpy() for t in torch_out_list]

    n_outputs = len(torch_out_list)
    output_names = [f"output{i}" if n_outputs > 1 else "output" for i in range(n_outputs)]

    print(f"Exporting to {out_path} ... (opset=18, FP32, do_constant_folding=False)")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The shape inference of prim::Constant type is missing")
        torch.onnx.export(
            model,
            dummy,
            out_path,
            opset_version=18,
            input_names=["input"],
            output_names=output_names,
            dynamic_axes=None,
            do_constant_folding=False,
            verbose=False,
        )
    print("Export done.")

    # --- Validation: PyTorch vs ONNX (onnxruntime CPUExecutionProvider) ---
    import onnxruntime as ort

    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    onnx_out = sess.run(None, {"input": dummy.numpy()})

    cosines = []
    for i, (t_out, o_out) in enumerate(zip(torch_out_np, onnx_out)):
        t_flat = t_out.reshape(-1).astype(np.float64)
        o_flat = o_out.reshape(-1).astype(np.float64)
        denom = (np.linalg.norm(t_flat) * np.linalg.norm(o_flat))
        cos = float(np.dot(t_flat, o_flat) / denom) if denom > 0 else float("nan")
        cosines.append(cos)
        print(f"  output[{i}] shape={t_out.shape} cosine={cos:.8f}")

    min_cos = min(cosines) if cosines else float("nan")
    print(f"\nPyTorch vs ONNX cosine (min over outputs): {min_cos:.8f}")

    THRESHOLD = 0.9999
    if min_cos < THRESHOLD:
        print(f"FAIL: cosine {min_cos:.8f} < threshold {THRESHOLD} -- DO NOT proceed to model-builder. "
              f"Fix build_model() (layer definitions likely mismatched) and re-export.")
        sys.exit(1)

    print("EXPORT_OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True, help="Path to .safetensors weights")
    ap.add_argument("--out", required=True, help="Output .onnx path")
    ap.add_argument("--input_shape", required=True,
                     help='Comma-separated input shape, e.g. "1,3,512,512"')
    args = ap.parse_args()

    input_shape = tuple(int(x) for x in args.input_shape.split(","))

    model = build_model(args.weights)
    model.eval()

    export_and_validate(model, input_shape, args.out)


if __name__ == "__main__":
    main()
