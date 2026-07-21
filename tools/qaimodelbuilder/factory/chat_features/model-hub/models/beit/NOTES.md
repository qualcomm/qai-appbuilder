# BEiT — Model Notes

## Model Overview

| Item | Value |
|------|-------|
| AI Hub Model ID | `beit-image-classification` |
| Hugging Face | https://huggingface.co/qualcomm/BEiT |
| Task | Image Classification (1000 classes, ImageNet) |
| Format used | QNN_DLC (w8a16 quantized) |
| Download URL | https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/beit/releases/v1/beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16.zip |
| Local path | `C:\WoS_AI\BEiT\beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16\` |
| Model file | `beit.dlc` |
| Labels file | `labels.txt` (1000 ImageNet classes) |

---

## Input / Output

| Tensor | Shape | Dtype | Notes |
|--------|-------|-------|-------|
| `image` | `[1, 224, 224, 3]` | float32 | NHWC format, range [0,1] |
| output | `[1, 1000]` | float32 | Logits → softmax → top-k |

> ⚠️ **Input is NHWC, NOT NCHW.**
> Transpose is handled automatically by `infer_classify.py` (which reads shape from metadata).
> Do NOT manually transpose to NCHW before passing to the model.

---

## Issues Encountered

### Issue 1: NHWC input format

**Symptom:** Wrong classification results or shape mismatch errors when using standard
NCHW preprocessing.
**Cause:** BEiT on QNN DLC expects NHWC `[1, 224, 224, 3]`, not the standard PyTorch
NCHW `[1, 3, 224, 224]`.
**Fix:** Use `infer_classify.py` — it reads `metadata.json` and handles the layout
automatically. If writing custom preprocessing:
```python
# PIL → numpy → NHWC
img = Image.open(path).convert("RGB").resize((224, 224))
arr = np.array(img, dtype=np.float32) / 255.0        # shape [224, 224, 3]
arr = arr[np.newaxis, ...]                             # shape [1, 224, 224, 3]
```

### Issue 2: Can use infer_classify.py directly (no custom code needed)

BEiT is a straightforward classification model. The generic `infer_classify.py`
template works out of the box — no custom preprocessing or postprocessing required.

---

## Inference Command

```powershell
& "<python_arm64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-builder\scripts\inference\infer_classify.py" `
  --model "C:\WoS_AI\BEiT\beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16\beit.dlc" `
  --input "C:\WoS_AI\AutoCropV1\test_image.jpg" `
  --labels "C:\WoS_AI\BEiT\beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16\labels.txt" `
  --topk 5
```

Or use the standalone script in this directory:
```powershell
& "<python_arm64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\skills\aihub-model-run\models\beit\infer_beit.py"
```

> `<python_arm64_venv>` is read from `${APP_ROOT}\data\config\qairt_env.json`, do not hardcode it; `${APP_ROOT}` = QAIModelBuilder repo root.

---

## Verified Output (test_image.jpg — cat photo)

```
Top-5 predictions:
  1. Egyptian cat         — 0.6231
  2. tabby, tabby cat     — 0.1847
  3. tiger cat            — 0.0923
  4. lynx, catamount      — 0.0211
  5. Persian cat          — 0.0134
```
