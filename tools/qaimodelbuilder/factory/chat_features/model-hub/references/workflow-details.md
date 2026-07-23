# Model Hub — Workflow Details (Reference)

> Loaded on-demand by `SKILL.md`. Code blocks preserved verbatim.

---

## Step 1 — Look Up the Model (detail)

### Method A — Parse AI Hub HTML (preferred, no login needed)

Page `https://aihub.qualcomm.com/models/<model_id>` embeds download links in Next.js data. Use `urllib` + regex (webfetch can't extract):
```python
import urllib.request, ssl, re

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE  # required on WoS — see Issue 19

req = urllib.request.Request(
    'https://aihub.qualcomm.com/models/<model_id>',
    headers={'User-Agent': 'Mozilla/5.0'}
)
html = urllib.request.urlopen(req, context=ctx, timeout=15).read().decode('utf-8', errors='ignore')

# Extract all S3 zip download links
s3_links = re.findall(r'https://qaihub-public-assets\.s3[^\s<>"]+\.zip', html)
seen = set()
for u in s3_links:
    u = u.rstrip('\\').rstrip('"').rstrip("'")
    if u not in seen:
        seen.add(u)
        print(u)
```

Example: `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.57.3/inception_v3-qnn_dlc-float.zip`
```

### Method B — webfetch HuggingFace (fallback)

Fetch `https://aihub.qualcomm.com/models/<model_id>` and `https://huggingface.co/qualcomm/<ModelName>`. HuggingFace has download links, `metadata.json` I/O shapes, performance tables.

> ⚠️ Do NOT use `qai_hub.get_models(name=...)` — raises `unexpected keyword argument`. Use Method A or HuggingFace. Models with `NOTES.md` (Step 0.5) have a fixed S3 link — skip this step.

### Fallback: Construct S3 URL from GitHub

1. Get `model_id` from `https://github.com/qualcomm/ai-hub-models/tree/main/src/qai_hub_models/models/` (dir name = model_id).
2. Read `release-assets.yaml` from `https://raw.githubusercontent.com/qualcomm/ai-hub-models/main/src/qai_hub_models/models/<model_id>/release-assets.yaml` (⚠️ `s3_key` with `pre_release_assets/` → private).
3. Probe versioned S3 URL (use GET not HEAD — S3 returns 403 for HEAD):


```python
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

candidates = [
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<model_id>/releases/v0.56.0/<model_id>-qnn_dlc-float.zip",
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<model_id>/releases/v0.56.0/<model_id>-qnn_dlc-w8a8.zip",
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<model_id>/releases/v0.55.0/<model_id>-qnn_dlc-w8a8.zip",
]
for u in candidates:
    try:
        r = urllib.request.urlopen(urllib.request.Request(u), context=ctx, timeout=8)
        print(f"OK {r.headers.get('Content-Length','?'):>12}  {u}")
        r.close()
    except urllib.error.HTTPError as e:
        print(f"{e.code}  {u}")
    except Exception as e:
        print(f"ERR {e}  {u}")
```

> 403/timeout normal — try next. `float`/`w8a8` availability varies. Some models use chipset suffix; check `release-assets.yaml`.

---

## Step 5 — Run Inference (detail)

| Model type | Template |
|------------|----------|
| Image classification | `infer_classify.py` |
| Object detection | `infer_detect.py` |
| Segmentation | `infer_segment.py` |
| Super resolution | `infer_sr.py` |
| Generic / I/O inspection | `infer_generic.py` |
| TTS / multi-model pipeline | custom script (see NOTES.md) |

### Run command

```powershell
& "<python_arm64_venv>\Scripts\python.exe" `
  "<template_path>\infer_classify.py" `
  --model "C:\WoS_AI\<model>\<folder>\<model>.dlc" `
  --input "C:\path\to\test_image.jpg" --labels "...\labels.txt" --topk 5
```

### Test image priority
1. Repo built-in: `${APP_ROOT}\samples\images\` (`flower.jpg`/`tabletop.jpg`)
2. Existing: `C:\WoS_AI\<model>\test_image.jpg`
3. Synthesize: `PIL.Image.fromarray(...)`
4. Download: `curl --max-time 20 -L -o x.jpg <URL>`

### Standard preprocessing (classification)

Use unless `metadata.json` overrides:

```python
from PIL import Image
import numpy as np

def preprocess(path, size=224):
    """→ float32 NHWC (1,H,W,C), [0,1]. Resize shortest-side, center-crop."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = size / min(w, h)
    nw, nh = int(round(w*scale)), int(round(h*scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    l, t = (nw-size)//2, (nh-size)//2
    arr = np.array(img.crop((l,t,l+size,t+size)), dtype=np.float32) / 255.0
    return arr[np.newaxis]  # (1,size,size,3)
```

> `"value_range"` in `metadata.json`: `[0,1]`→/255; `[-1,1]`→/127.5-1; `[0,255]`→cast float32. ONNX transpose→Issue 15.

---

## ONNX (CPU) + QNN DLC (NPU) same process

`onnxruntime` CPUExecutionProvider does NOT conflict with `qai_appbuilder` (only NPU/HTP is exclusive):

```python
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.OFF)  # 2.47 signature: (runtime, log_level, profiling_level)
qnn = QNNContext("m", "model.dlc")
ort_sess = ort.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])
nhwc = preprocess("img.jpg")
qnn_out  = np.array(qnn.Inference([nhwc])[0]).flatten()
onnx_out = np.array(ort_sess.run(None, {"image_tensor": nhwc.transpose(0,3,1,2)})[0]).flatten()
cos = float(np.dot(qnn_out, onnx_out) / (np.linalg.norm(qnn_out)*np.linalg.norm(onnx_out)))
print(f"Cosine similarity: {cos:.6f}")  # expect >0.9999 for float models
```

---

## Phase 7 — Export app_pack + Promote to App Builder (optional)

**Prerequisites:** Step 6.5 done; user wants model in App Builder or `app_pack/` pre-generated.

### Step 7.2 — Run shared exporter

```powershell
& "<python_x64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_export.py" `
  --workdir "C:\WoS_AI\<model>" `
  --model-name <model> `
  --precision <same precision as 7.1>
```

Creates `C:\WoS_AI\<model>\app_pack\` with `manifest.json`/`runner.py`/`requirements.txt`/`weights/`/`assets/`/`examples/`/`provenance/`/`_candidate.json` (`ready: true` when checks pass).

### Step 7.3 — Validate + import

```powershell
& "<python_x64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_validate.py" `
  "C:\WoS_AI\<model>\app_pack"
```

After validation: "Promote to App Builder" in UI or `POST /api/appbuilder/import/commit`. Field spec: `${APP_ROOT}\factory\chat_features\model-builder\references\pack_export.md`.

> ⚠️ **Multi-sub-model packages** (TTS, streaming ASR encoder/decoder/joiner): multiple `.bin` + custom pipeline. Single-`.bin` mapper targets single-graph models only. For multi-sub-model packs, follow `factory/chat_features/app-builder/models/` references (`melotts-zh`/`zipformer-zh`/`whisper-base`) `manifest.json` + `runner.py` layout.
