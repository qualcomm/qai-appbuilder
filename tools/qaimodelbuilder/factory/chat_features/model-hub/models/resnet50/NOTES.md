# ResNet50 — Model Notes

## Overview

| Item | Value |
|------|-------|
| AI Hub model ID | `resnet50` |
| Task | Image classification, ImageNet-1K |
| Version | v0.56.0 (QAIRT 2.45.0) |
| Download (QNN_DLC float) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/resnet50/releases/v0.56.0/resnet50-qnn_dlc-float.zip` |
| Download (ONNX float) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/resnet50/releases/v0.56.0/resnet50-onnx-float.zip` |
| Local path | `C:\WoS_AI\resnet50\` |

---

## Input / Output

| Format | Tensor | Shape | Dtype | Layout |
|--------|--------|-------|-------|--------|
| QNN DLC | `image_tensor` | `[1,224,224,3]` | float32 | **NHWC** |
| ONNX | `image_tensor` | `[1,3,224,224]` | float32 | **NCHW** |
| both | `class_logits` | `[1,1000]` | float32 | — |

`value_range: [0.0, 1.0]` for both formats (normalization baked into model — **divide by 255 only, no ImageNet mean/std**).

> Layout difference and preprocessing: → SKILL.md Issue 15 + "Standard image preprocessing".
> ONNX external data file (`resnet50.data`): → SKILL.md Issue 14.

---

## Performance (Snapdragon X2 Elite, SoC 8480)

| Backend | Compute | Latency (warm) |
|---------|---------|----------------|
| QNN DLC (`qai_appbuilder`, HTP) | NPU | **4.34 ms** |
| ONNX Runtime (`CPUExecutionProvider`) | CPU | **26.30 ms** |
| **NPU speedup** | | **6.06×** |

Cold-start (first load): ~256 ms (HTP graph compilation, normal).

---

## QNN DLC vs ONNX — Comparison (Snapdragon X2 Elite)

Same input tensor, 1 warm-up + 1 timed run. Compare pattern: → SKILL.md "ONNX (CPU) + QNN DLC (NPU) in the same process".

| Metric | Value |
|--------|-------|
| Cosine similarity (logits) | **0.999998** |
| Max \|logit diff\| | 0.018 |
| Top-1 agreement | ✓ SAME |

---

## Inference Scripts

| Script | Purpose |
|--------|---------|
| `C:\WoS_AI\resnet50\run_inference.py` | QNN DLC (NPU), `--input img1 img2 ...` |
| `C:\WoS_AI\resnet50\compare_onnx_vs_qnn.py` | Side-by-side ONNX CPU vs QNN NPU |
| `C:\WoS_AI\resnet50\infer_resnet50.py` | QNN DLC (NPU) Top-K classify, `--model --input --labels --topk` (verified on X Elite) |

```powershell
# Top-5 classify on NPU (Snapdragon X Elite verified)
& "<python_arm64_venv>\Scripts\python.exe" "C:\WoS_AI\resnet50\infer_resnet50.py" `
  --model "C:\WoS_AI\resnet50\resnet50-qnn_dlc-float\resnet50.dlc" `
  --input "<repo>\samples\images\flower.jpg" `
  --labels "C:\WoS_AI\resnet50\resnet50-qnn_dlc-float\labels.txt" --topk 5
```

```powershell
# QNN DLC inference
& "<python_arm64_venv>\Scripts\python.exe" "C:\WoS_AI\resnet50\run_inference.py" `
  --input "C:\test\images\1.jpg" "C:\test\images\4.jpg"

# ONNX vs QNN comparison
& "<python_arm64_venv>\Scripts\python.exe" "C:\WoS_AI\resnet50\compare_onnx_vs_qnn.py"
```

---

## Verified Output (Snapdragon X Elite, SoC 8380 — added 2026-06)

Ran `run_infer2.py` on the QNN_DLC float package with the repo built-in real images (`qai_appbuilder` 2.48.40, QAIRT 2.48). Warm NPU inference ~5 ms.

| Image (repo `samples\images\`) | Top-5 |
|--------|-------|
| `flower.jpg` | cabbage butterfly 27.29% / daisy 9.77% / bee 9.18% / sulphur butterfly 5.33% / monarch 4.32% |
| `tabletop.jpg` | teapot 8.29% / coffeepot 8.06% / cup 7.91% / mortar 5.74% / ladle 4.25% |

> `flower.jpg` Top-5 matches the X2 Elite `4.jpg` result exactly (cross-chipset consistency confirmed).
> Non-fatal teardown log `Error 0x200: failed to close queue` appears at process exit (SKILL Step 6) — ignore, results are correct.

### Issue Z-1: `os._exit()` 触发进程崩溃（已修复）

在推理脚本末尾调用 `os._exit(0)` 且未提前 `del` `QNNContext` 会导致进程以 `0xC0000409` 异常退出。已删除该调用，脚本正常退出即可。若需提前退出，先 `del model` 再 `os._exit()`。