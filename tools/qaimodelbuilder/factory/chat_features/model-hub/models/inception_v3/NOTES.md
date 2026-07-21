# Inception-v3 — Model Notes

## Overview

| Item | Value |
|------|-------|
| AI Hub model ID | `inception_v3` |
| Task | Image classification, ImageNet-1K |
| Version | v0.56.0 (QAIRT 2.45.0.260326154327) |
| Format used | QNN_DLC float (Universal — no chipset suffix) |
| Download URL | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.56.0/inception_v3-qnn_dlc-float.zip` |
| Local path | `C:\WoS_AI\inception_v3\inception_v3-qnn_dlc-float\` |

---

## Chipset Detection

| Driver name | Chipset | Suffix |
|-------------|---------|--------|
| `qcadsprpc8380` / `qcadsprpcd8380` | Snapdragon X Elite | `qualcomm_snapdragon_x_elite` |
| `qcadsprpc8480` / `qcadsprpcd8480` | Snapdragon X2 Elite | `qualcomm_snapdragon_x2_elite` |

> Note: The QNN_DLC float package is "Universal" (no chipset suffix in the filename). It works on all supported chipsets including X Elite.

---

## Available Download Packages (v0.56.0)

| Runtime | Precision | URL |
|---------|-----------|-----|
| QNN_DLC | float | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.56.0/inception_v3-qnn_dlc-float.zip` |
| QNN_DLC | w8a8 | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.56.0/inception_v3-qnn_dlc-w8a8.zip` |
| ONNX | float | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.56.0/inception_v3-onnx-float.zip` |
| ONNX | w8a8 | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.56.0/inception_v3-onnx-w8a8.zip` |

> Download requires `--ssl-no-revoke` flag with curl on this machine:
> `curl -L --ssl-no-revoke "<url>" -o "<output.zip>"`

---

## Extracted File Structure

```
C:\WoS_AI\inception_v3\
├── inception_v3-qnn_dlc-float.zip
└── inception_v3-qnn_dlc-float\
    ├── inception_v3.dlc       <- QNN DLC model file (load with QNNContext)
    ├── metadata.json          <- I/O shapes, dtypes, value_range
    └── labels.txt             <- 1000 ImageNet class labels
```

---

## Input / Output (from metadata.json)

| Tensor | Shape | Dtype | Layout | Value Range |
|--------|-------|-------|--------|-------------|
| `image_tensor` (input) | `[1, 299, 299, 3]` | float32 | **NHWC** | `[0.0, 1.0]` |
| `class_logits` (output) | `[1, 1000]` | float32 | — | raw logits |

> **IMPORTANT**: Input is **299×299** (not 224×224 as shown on the HuggingFace model card page). Always trust `metadata.json` over the web page.

---

## Preprocessing

```python
from PIL import Image
import numpy as np

def preprocess(path, size=299):
    """Resize shortest-side to size, center-crop, normalize to [0,1]."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = size / min(w, h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    l = (nw - size) // 2
    t = (nh - size) // 2
    arr = np.array(img.crop((l, t, l + size, t + size)), dtype=np.float32) / 255.0
    return arr[np.newaxis]  # (1, 299, 299, 3) NHWC
```

- `value_range: [0.0, 1.0]` → divide by 255 only (normalization baked into model)
- No ImageNet mean/std subtraction needed

---

## Performance

### Snapdragon X Elite (SoC 8380)

| Metric | Value |
|--------|-------|
| Cold start (graph compilation) | ~4637 ms |
| Warm inference | **~5.96 ms** |
| Primary compute unit | NPU (HTP v73) |

### Snapdragon X2 Elite (SoC 8480)

| Metric | Value |
|--------|-------|
| Model load/compile time | ~299 ms |
| Inference time | **~8.53 ms** |
| Total (load+infer) | ~308 ms |
| Primary compute unit | NPU (HTP) |

---

## Inference Command (using infer_classify.py template)

```powershell
# ${APP_ROOT} = QAIModelBuilder repo root (e.g. C:\Shared\<user>\mb\QAIModelBuilder)
# python_arm64_venv = read from ${APP_ROOT}\data\config\qairt_env.json
& "<python_arm64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-builder\scripts\inference\infer_classify.py" `
  --model "C:\WoS_AI\inception_v3\inception_v3-qnn_dlc-float\inception_v3.dlc" `
  --input "${APP_ROOT}\samples\images\flower.jpg" `
  --labels "C:\WoS_AI\inception_v3\inception_v3-qnn_dlc-float\labels.txt" `
  --topk 5
```

---

## Dedicated standalone inference script (recommended)

Two self-contained Top-5 scripts are available:

### `infer_inception.py` (current — created/verified 2026-07, qai_appbuilder 2.48)

Clean script following SKILL.md spec: Issue 9 config-first, Issue 7/12 actual I/O confirmation, Issue 4 utf-8 stdout, correct NHWC preprocessing with value_range [0,1].

> **qai_appbuilder 2.47+ signature change**: `QNNConfig.Config` takes 3 positional args `(Runtime.HTP, LogLevel.ERROR, ProfilingLevel.OFF)` — no leading empty string. The old 4-arg form `("", Runtime.HTP, ...)` is for ≤2.46 and will fail on 2.48+.

Verified on qai_appbuilder 2.48.40 / QAIRT 2.48.40.260702 / Snapdragon X Elite (SoC 8380).

```
C:\WoS_AI\inception_v3\infer_inception.py
```

Run:
```cmd
:: python_arm64_venv = read from ${APP_ROOT}\data\config\qairt_env.json
:: ${APP_ROOT} = QAIModelBuilder repo root
<python_arm64_venv>\Scripts\python.exe ^
  C:\WoS_AI\inception_v3\infer_inception.py ^
  --model C:\WoS_AI\inception_v3\inception_v3-qnn_dlc-float\inception_v3.dlc ^
  --input ${APP_ROOT}\samples\images\flower.jpg ^
  --labels C:\WoS_AI\inception_v3\inception_v3-qnn_dlc-float\labels.txt ^
  --topk 5
```

### `infer_inception_v3_topk.py` (legacy)

```
C:\WoS_AI\inception_v3\infer_inception_v3_topk.py
```

---

## Verified Output (flower.jpg — Snapdragon X Elite, SoC 8380 & X2 Elite, SoC 8480)

Test image: `${APP_ROOT}\samples\images\flower.jpg` (repo built-in)
Model: `C:\WoS_AI\inception_v3\inception_v3-qnn_dlc-float\inception_v3.dlc` (AI Hub v0.56.0 QNN_DLC float)

> Results verified on SoC 8380 (X Elite) with qai_appbuilder 2.48.40 / QAIRT 2.48.40.260702 (2026-07).
> Also verified on SoC 8480 (X2 Elite) — identical Top-5.

| Rank | Class Index | Label | Probability (8380) | Probability (8480) |
|------|-------------|-------|-------------------|-------------------|
| #1 | 985 | daisy | **79.43%** | **79.56%** |
| #2 | 309 | bee | 0.95% | 0.94% |
| #3 | 946 | cardoon | 0.89% | 0.88% |
| #4 | 716 | picket fence | 0.63% | 0.62% |
| #5 | 738 | pot | 0.49% | 0.48% |

---

## Known Issues / Notes

### Issue Z-1: HuggingFace page shows wrong input resolution
The HuggingFace model card for `qualcomm/Inception-v3` shows "Input resolution: 224x224" in the Model Details section, but `metadata.json` clearly specifies `[1, 299, 299, 3]`. **Always trust `metadata.json`** over the web page description.

### Issue Z-2: QNN_DLC float package is Universal (no chipset suffix)
Unlike some models that have chipset-specific packages (e.g. `*-qualcomm_snapdragon_x_elite.zip`), the Inception-v3 QNN_DLC float package is universal and works on all supported chipsets. The filename is simply `inception_v3-qnn_dlc-float.zip`.

### Issue Z-3: curl requires --ssl-no-revoke on this machine
Direct `curl` download fails with `CRYPT_E_NO_REVOCATION_CHECK`. Always add `--ssl-no-revoke`:
```
curl -L --ssl-no-revoke "<url>" -o "<output>"
```

### Non-fatal warnings (safe to ignore)
- `warmup_parallel_stl` — normal HTP initialization
- `tiling.h:278:WARNING:Tried to assign requirement...` — graph optimization warnings, results are correct
- `m_CFBCallbackInfoObj is not initialized` — v81 callback init order, non-fatal
- `Error 0x200: failed to close queue` — queue close timing at teardown, non-fatal
- `DSP_INFO UNSUPPORTED_KEY: 49/50` — DSP capability query, non-fatal
