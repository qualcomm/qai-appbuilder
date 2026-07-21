---
name: Model Hub
description: Model Hub — download pre-exported models from Qualcomm AI Hub, run inference on-device, and export them to App Builder as ready-to-import Packs (same app_pack contract as model-builder). Supports QNN_CONTEXT_BINARY, QNN_DLC, VOICE_AI, ONNX, TFLITE formats. All on-device (NPU/HTP) inference runs through qai_appbuilder (QNNContext), loading the QNN context binary (.bin) or .dlc. Standard onnxruntime is used only for an optional CPU baseline comparison.
tags: aihub, model-hub, inference, qualcomm, on-device, qnn, tts, asr, classification, export, app-builder
use_for: Download pre-exported models from AI Hub, run inference on Snapdragon X Elite / X2 Elite without conversion, then always normalize the download into the App Builder workspace contract (Step 6.5, mandatory) so it is importable; optionally pre-build the full app_pack and promote it (Phase 7).
homepage: "https://aihub.qualcomm.com/compute/models"
---

# Model Hub — AI Hub Model Download, Inference & App Builder Export

> ## 🚨 READ THIS ENTIRE FILE BEFORE TAKING ANY ACTION

---

## ✋ Pre-Flight Self-Check (MANDATORY GATE — answer all questions before downloading)

> Skipping this table (especially question 2) is the root cause of repeated pitfalls — most "dependency trial-and-error / I/O mismatch / wrong tool" problems already have ready answers in the corresponding model's `NOTES.md`.

| # | Must answer | How to do it |
|---|------|--------|
| 1 | Detected the chipset suffix? | Step 0 (`Get-WmiObject`, **NOT** `Get-PnpDeviceProperty`) |
| 2 | Read this model's `NOTES.md`? | `read factory/chat_features/model-hub/models/<model_id>/NOTES.md`; if it exists → must read (contains download links / I/O / known pitfalls / ready-made `infer_*.py`, lets you skip most of Step 1~5); if it does not exist → you **must create it** at the end of the task |
| 3 | Inference tool = `qai_appbuilder`? | NPU inference always uses `QNNContext` to load `.bin`/`.dlc`; `onnxruntime` is only for the optional CPU baseline comparison |
| 4 | Locating the model package **uses fixed paths only**? | Model packages go via webfetch→curl downloaded to the fixed `C:/WoS_AI/<model>/`; **NEVER** `Get-ChildItem -Recurse` to scan all of `C:\`/`C:\Shared`/`C:\Users`/`C:\WoS_AI` (it hangs for 30+ minutes, see Issue 18) |
| 5 | Will you normalize the workspace after inference? | Step 6.5 (`aihub_to_manifest.py`) is **MANDATORY** right after inference passes — it produces `output/<model>_<label>.{bin,dlc}` + `inference_manifest.json`, the layout App Builder's "Import" scan requires. Skipping it = "model on disk but App Builder can't see it". NOT optional (only the full app_pack export / promote in Phase 7 is optional) |

> NOTES already collected: `beit` `melotts_zh` `resnet50` `zipformer` (see the Model Notes Directory at the end of this file).

---

## 🤖 Sub-Agent Dispatch Template (MANDATORY when dispatching a sub-agent to execute this skill)

> **Background lesson (2026-06)**: The main agent saw only the one-line `Use for` summary of this skill in its system prompt, did not read this full SKILL.md, and was contaminated by the path mindset of the `model-builder` skill that was also present — so it wrote a sub-agent instruction to "search `C:\Shared` / `C:\WoS_AI` for `.bin` / `metadata.json`". The sub-agent had a brand-new blank context, did not inherit the main agent's system prompt, and could only execute the prompt as written → triggering the full-disk recursion of Issue 18 and hanging for 30+ minutes.

**If the main agent is going to dispatch a sub-agent to execute this skill, it MUST follow these rules:**

1. **Before dispatching, the main agent MUST first `read` the full SKILL.md** (do not assemble a prompt based only on the one-line `Use for` summary in the catalog).
2. **The sub-agent prompt's first instruction MUST be "read the full SKILL.md before acting"** — the sub-agent does not inherit the main agent's context, so all the constraints of this SKILL (Issue 18 anti-recursion, fixed-path download, qai_appbuilder inference, etc.) can only be learned by reading it itself.
3. **The sub-agent prompt MUST NOT carry any path / script / toolchain reference from the `model-builder` skill** (`factory\chat_features\model-builder\scripts\*.py`, `run_pipeline.py`, `qnn-onnx-converter`, `qairt_sdk_root` conversion tools, etc.) — those apply only to "custom ONNX conversion", are useless for AI Hub pre-compiled packages, and induce wrong full-disk searches.
4. **The sub-agent prompt MUST NOT contain any "search / look for" combined with a large directory (`C:\`, `C:\Shared`, `C:\Users`, `C:\WoS_AI`)** (see Issue 18). Locate the model package using fixed paths only: `qai_hub` direct download / `curl` download to `C:/WoS_AI/<model>/`.

**Reusable standard sub-agent prompt template:**

```
You will execute an model-hub skill task: <specific task, e.g. "download the Inception v3 pre-compiled package and run inference on a test image on the NPU, printing Top-5">.

[Step 1, mandatory] First use the read tool to fully read:
  ${APP_ROOT}\factory\chat_features\model-hub\SKILL.md
Follow it strictly (including the Pre-Flight Self-Check GATE, fixed-path download, Issue 18 anti-recursion, qai_appbuilder inference rule).
(${APP_ROOT} = QAIModelBuilder repo root; do not hardcode the absolute path of any particular machine.)

[Step 0.5, mandatory — NOTES.md two-step check]
The directory name under factory/chat_features/model-hub/models/ may differ from the model name in this prompt
(e.g. prompt says "inceptionv3" but directory is "inception_v3"; prompt says "BEiT" but directory is "beit").
NEVER guess the path or rely on a single glob — always do BOTH steps:
  1. list "factory/chat_features/model-hub/models/" to see the exact directory names present.
  2. read the matching NOTES.md using the exact name found in step 1.
If a matching NOTES.md exists it contains download links, I/O shapes, known issues, and a ready-made
inference script — use them directly and skip most of Steps 1~5.

[Locating the model package — fixed paths only, NEVER full-disk search]
- If qai_hub is installed (import qai_hub can print the version) → download directly with qai_hub; or per SKILL Step 1/3 use webfetch to get the link, curl download to C:/WoS_AI/<model>/.
- You may only check these two fixed shallow directories (Get-ChildItem WITHOUT -Recurse): C:\WoS_AI\<model>\, C:\Users\<user>\.qaihub\.
- 🚫 NEVER run -Recurse / glob **/* full-disk recursion on the UNBOUNDED roots C:\, C:\Shared, C:\Users, or C:\WoS_AI WITHOUT a <model> subdir (it hangs for 30+ minutes). Recursing inside a specific bounded dir like C:\WoS_AI\<model>\ is fine.

[Environment] Read python_arm64_venv from ${APP_ROOT}\data\config\qairt_env.json; inference always uses qai_appbuilder / QNNContext.
[Forbidden] Do not use any script/path from the model-builder skill (run_pipeline.py, qnn-onnx-converter, qairt conversion tools, etc.) — this task is a pre-compiled package, no conversion. (Exception: the shared exporter qai_pack_export.py referenced by SKILL Phase 7 is allowed — it is the common App Builder export chain, not a conversion tool.)

[MANDATORY after inference — Step 6.5 workspace normalization] Once inference is verified correct, you MUST run
  factory/chat_features/model-hub/scripts/aihub_to_manifest.py --workdir C:\WoS_AI\<model> --model-name <model> --precision <prec> --output-type <type>
to normalize the download into the App Builder contract (output/<model>_<label>.{bin,dlc} + inference_manifest.json). This is NOT optional — without it the model is invisible to App Builder's "Import" scan. --workdir must be the top-level C:\WoS_AI\<model> (not the nested ...-qnn_dlc-<prec> subfolder) and --model-name must equal that folder name.

When done, report: chipset detection result / model package path and contents / inference script path / Top-5 results / the normalized output/<model>_<label>.{bin,dlc} + inference_manifest.json paths (Step 6.5).
```

---

## Decision Prerequisites

**Default to this skill**: download the AI Hub pre-compiled package and run inference directly with `qai_appbuilder` (`QNNContext`). Only `python_arm64_venv` is needed; no VS 2022 / QAIRT SDK conversion toolchain required.

> 🚨 **Inference tool selection rule (MANDATORY)**
>
> **NPU (HTP) inference always uses `qai_appbuilder` (`QNNContext`), loading a QNN context binary (`.bin`) or `.dlc`.**
>
> In this skill, standard `onnxruntime` is **only used for the optional CPU baseline comparison** (`CPUExecutionProvider`, for accuracy/performance comparison against the NPU result), and is **never** used to run a model on the NPU.

**Switch to the `model-builder` skill when**: ① you have a custom ONNX/PyTorch model and AI Hub has no corresponding pre-compiled package; ② you need to re-quantize/compile a custom NPU `.bin`.

---

## Trigger Phrases

- "download model from aihub and run inference"
- "run \<ModelName\> on device" / "run inference for \<ModelName\>" / "infer \<ModelName\> for me"

---

## Environment

> ℹ️ **`${APP_ROOT}` = the root directory of this repo (QAIModelBuilder)**. This file uses `${APP_ROOT}` to refer to the repo root, avoiding hardcoding the absolute path of any particular machine. At runtime, just resolve it to the actual repo root location (the current working directory of `exec` is usually the repo root; if unsure, infer it from this machine's install path, or work backwards from an absolute path that appears in `qairt_env.json`).

Read paths from `${APP_ROOT}\data\config\qairt_env.json`:

| Key | Role |
|-----|------|
| `python_arm64_venv` | ARM64 Python 3.13 — ALL inference runs here |
| `qairt_sdk_root` | QAIRT SDK root (reference only) |

Working directory: `C:/WoS_AI/<model_name>/`

> ⚠️ **`read` / `skill` tool path rendering warning — do NOT trust the displayed path literally when editing files:**
> The `read` and `skill` tools automatically expand placeholder variables (e.g. `$​{APP_ROOT}`) into the actual absolute path of the current machine before displaying content. This means:
> 1. **The displayed path may look like a hardcoded absolute path, but the file actually contains `$​{APP_ROOT}` (or another placeholder).** Do NOT copy the displayed absolute path back into an `edit` call as `oldText` — the match will fail because the file does not contain that string.
> 2. **Do NOT replace `$​{APP_ROOT}` with an absolute path when editing.** If you see `C:\Work\AppBuilder\...` in the displayed output and want to edit that line, use Python to read the raw file content first (`open(...).read()`) to confirm what the file actually contains, then use the real content as `oldText`.
> 3. **Quick check:** run `python -c "print(open(r'<file>').read()[<idx>:<idx+100>])"` to inspect the raw bytes around the target line before writing an `edit`.

---

## Step 0 — Detect Platform (MANDATORY)

```powershell
Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
  Where-Object { $_.PSChildName -like "qcadsp*" } |
  Get-ItemProperty | Select-Object PSChildName, ImagePath
```

Read the 4-digit SoC code from the INF filename in `ImagePath`.

| Driver name | Chipset | Download suffix |
|-------------|---------|----------------|
| `qcadsp*_8380` | Snapdragon X Elite | `qualcomm_snapdragon_x_elite` |
| `qcadsp*_8480` | Snapdragon X2 Elite | `qualcomm_snapdragon_x2_elite` |
| `qcadsp*_8380` (8-core) | Snapdragon X Plus 8-Core | `qualcomm_snapdragon_x_plus_8_core` |

> ⚠️ Wrong chipset package → wrong inference results. Always detect first.
> ⚠️ Do NOT use `Get-PnpDeviceProperty` — it wakes the DSP and blocks 300–400 s.
> ⚠️ Do NOT use `Get-WmiObject Win32_SystemDriver` — WMI provider throttling causes this query to hang indefinitely on busy systems.

---

## Step 0.5 — Check Model Notes (MANDATORY — Before Any Other Step)

> 🚨 **MANDATORY — before executing Step 1 you must first check the local NOTES.md (skipping this step is the root cause of repeated pitfalls).**

> ⚠️ **The directory name under `models/` may differ from the model_id you were given** (e.g. task says `inceptionv3` but directory is `inception_v3`; task says `BEiT` but directory is `beit`). **NEVER rely on a single glob/path-exists check alone.** The mandatory two-step procedure is:
> 1. **Always `list` the `models/` directory first** to see the exact directory names present.
> 2. **Then `read` the matching NOTES.md** using the exact directory name from step 1.
> Skipping step 1 and guessing the path is the direct cause of "NOTES.md not found" false negatives that waste hours of unnecessary work.

```
# Step 1: list to find the exact directory name
list "factory/chat_features/model-hub/models/"   ← always do this first

# Step 2: read NOTES.md using the exact name from the listing
read "factory/chat_features/model-hub/models/<exact_dir_name>/NOTES.md"
```

```python
# Only after the two steps above — if truly no matching directory exists:
# → continue to Step 1, and create NOTES.md in this directory to record new findings when done
```

The content in NOTES.md lets you **directly skip** most of the work in Step 1~4:

| If NOTES.md has | Steps you can skip |
|--------------|-------------|
| Download links | The webfetch lookup in Step 1 |
| Sub-models I/O table | Reading metadata.json in Step 4 |
| Known Issues (e.g. input order, dtype) | Avoid hitting the pitfall before looking it up |
| `infer_<model>.py` inference script | Skip Step 5 entirely, run it directly |


---

## Step 1 — Look Up the Model

### Method A — Parse AI Hub page HTML (preferred, always works without login)

The AI Hub model page (`https://aihub.qualcomm.com/models/<model_id>`) embeds **all download links directly in the HTML source** (inside Next.js data islands). `webfetch` cannot see them (returns only visible text), but Python `urllib` + regex extracts them reliably:

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

This returns the **full versioned list** of all available formats and precisions, e.g.:
```
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.57.3/inception_v3-qnn_dlc-float.zip
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.57.3/inception_v3-qnn_dlc-w8a8.zip
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/inception_v3/releases/v0.57.3/inception_v3-onnx-float.zip
...
```
> Note: The AI Hub page URL uses `https://aihub.qualcomm.com/models/<model_id>` (no `/compute/` prefix).

### Method B — webfetch HuggingFace (fallback when Method A fails)

Fetch both URLs with `webfetch`:
- `https://aihub.qualcomm.com/models/<model_id>`
- `https://huggingface.co/qualcomm/<ModelName>`

The HuggingFace page has download links, `metadata.json` I/O shapes, and performance tables.

> ⚠️ **Do NOT use `qai_hub.get_models(name=...)` to search models by name** — that API **does not accept the `name` keyword** (raises `unexpected keyword argument`). The correct way to get download links is Method A above or the HuggingFace model page. For models that already have a `NOTES.md` (see Step 0.5), just use the fixed S3 link recorded in NOTES — you can even skip this step entirely.

### Fallback: Construct S3 URL from GitHub (when webfetch/HuggingFace is blocked)

When both the AI Hub page and HuggingFace return 401/404 (gated model or network restriction), use this fallback to find and probe the S3 URL directly:

**Step A — Get the exact `model_id` from GitHub:**
```
https://github.com/qualcomm/ai-hub-models/tree/main/src/qai_hub_models/models/
```
The directory name IS the `model_id` (e.g. `inception_v3`, `mobilenet_v2`, `resnet50`). Do NOT guess — fetch the directory listing to confirm.

**Step B — Read `release-assets.yaml` from GitHub to get available formats/precisions:**
```
https://raw.githubusercontent.com/qualcomm/ai-hub-models/main/src/qai_hub_models/models/<model_id>/release-assets.yaml
```
This file lists all available formats (`qnn_dlc`, `onnx`, `tflite`) and precisions (`float`, `w8a8`, `w8a16`).
> ⚠️ If `s3_key` starts with `pre_release_assets/gh_actions/...` → that path is **private (AccessDenied)**. Use the public versioned path in Step C instead.

**Step C — Construct and probe the public versioned S3 URL:**

URL template:
```
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<model_id>/releases/v<ver>/<model_id>-<runtime>-<precision>.zip
```

- Start with the **latest known working version** (`v0.56.0` verified 2026-06; try `v0.55.0` as fallback).
- The DLC assets are **universal** (no chipset suffix in the filename for `qnn_dlc`).
- Probe with Python `urllib` GET (not HEAD/Range — S3 returns 403 for those even on public objects):

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

**Known limitations of this fallback:**
- Not all models/precisions are publicly accessible at every version — 403 or timeout is normal; try the next candidate.
- `float` precision is sometimes inaccessible (timeout) while `w8a8` works, or vice versa.
- Some models use a chipset suffix in the filename (e.g. BEiT: `beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16.zip`); check `release-assets.yaml` for the exact filename pattern.
- Version `v0.56.0` was verified working for `inception_v3`, `mobilenet_v2`, `resnet50` (2026-06); update this note when a newer version is confirmed.

---

## Step 2 — Choose Format

> 🚨 **Format selection rule (MANDATORY):**
> 1. **NPU inference always uses `qai_appbuilder.QNNContext`**, loading a QNN context binary (`.bin`) or `.dlc`.
> 2. **Prefer `QNN_CONTEXT_BINARY` (`.bin`) or `QNN_DLC` (`.dlc`)** — the vast majority of AI Hub models provide one of them, loadable directly by `qai_appbuilder`. For multi-platform cases, **prefer the context binary matching this machine's chipset** (X Elite / X2 Elite, see Step 0 detection).
> 3. The `ONNX` format is only used for the **CPU baseline comparison** with `onnxruntime` (`CPUExecutionProvider`), to compare accuracy/performance against the NPU result, and is **never** used for NPU inference.

| Priority | Format | How to run | When to use |
|----------|--------|-----------|-------------|
| **1st** ✅ | `QNN_CONTEXT_BINARY` | `qai_appbuilder.QNNContext` (load `.bin`, see Issue 9) | **Default first choice**, context binary matching this machine's chipset |
| **2nd** ✅ | `QNN_DLC` | `qai_appbuilder.QNNContext` (load `.dlc`) | When no `.bin`; `.dlc` is portable across HTP versions |
| 3rd | `ONNX` | `onnxruntime` CPUExecutionProvider | **Only** for CPU baseline comparison |
| 4th | `VOICE_AI` | custom pipeline (see Issue 13) | TTS/multi-model pipeline (`.bin` inside the package) |
| 5th | `TFLITE` | fallback only | Last resort |

Download URL pattern:
```
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<model_id>/releases/v<ver>/<model_id>-<runtime>-<precision>-<chipset_suffix>.zip
```

---

## Step 3 — Download & Extract

```python
# Download (model ZIPs can be 200 MB+, use long timeout)
# -k disables SSL certificate verification (required on WoS — see Issue 19)
exec('curl -k -L "<url>" -o "C:/WoS_AI/<model>/<file>.zip" --create-dirs', timeout=300)

# Extract — ALWAYS zipfile, NEVER tar (tar does not support ZIP)
import zipfile
zipfile.ZipFile("C:/WoS_AI/<model>/<file>.zip").extractall("C:/WoS_AI/<model>/")
```

ZIP typically contains: model files (`.dlc`/`.bin`/`.onnx`), `metadata.json`, optionally `labels.txt` / `config.json`.

---

## Step 4 — Read metadata.json (MANDATORY)

Always read before writing inference code. Contains:
- Input/output tensor **names, shapes, dtypes**
- **Quantization params** (scale + zero_point) for quantized models
- Input layout (NHWC vs NCHW)

> ⚠️ Do NOT rely on `model.getInputDataType()` alone — QNN reports quantized uint16 as `ufp16`. See Issue 5.

---

## Step 5 — Run Inference

### Templates (at `${APP_ROOT}/factory/chat_features/model-builder/scripts/inference/`)

| Model type | Template |
|------------|----------|
| Image classification | `infer_classify.py` |
| Object detection | `infer_detect.py` |
| Segmentation | `infer_segment.py` |
| Super resolution | `infer_sr.py` |
| Generic / I/O inspection | `infer_generic.py` |
| TTS / multi-model pipeline | custom script (see model NOTES.md) |

### Run command

```powershell
& "<python_arm64_venv>\Scripts\python.exe" `
  "<template_path>\infer_classify.py" `
  --model "C:\WoS_AI\<model>\<folder>\<model>.dlc" `
  --input "C:\path\to\test_image.jpg" --labels "...\labels.txt" --topk 5
```

### Test image priority
1. **Repo built-in** (first choice): `${APP_ROOT}\samples\images\` (`flower.jpg` / `tabletop.jpg`)
2. Reuse existing: `C:\WoS_AI\<model>\test_image.jpg`
3. Synthesize locally (shape-specific only): `PIL.Image.fromarray(...)`
4. Download (last resort): `curl --max-time 20 -L -o x.jpg <URL>`

### Standard image preprocessing (classification)

Use unless `metadata.json` says otherwise:

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

> ℹ️ Check `metadata.json` `"value_range"`: `[0,1]` → /255 only (AI Hub default, norm baked in);
> `[-1,1]` → `/127.5-1`; `[0,255]` → cast float32 directly. For ONNX: transpose → Issue 15.

### 🚀 HTP BURST performance mode — lifecycle rule (MANDATORY)

`PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)` raises the HTP to its
highest clock. Getting the **lifecycle** right is critical for stable, fast
inference — especially for **streaming / session-based** workloads (voice
input/ASR, TTS, any task that runs many inferences over a session).

**Rules:**

1. **BURST only takes effect AFTER at least one model (QNNContext) is loaded
   in the process.** Calling it before any context exists silently does
   nothing (you will see `setPowerConfig error 0x32c9` — "no BURST permission").

2. **Set BURST ONCE at the start of the whole task and hold it for the ENTIRE
   session; release it ONCE only when the whole task is finished.** Do **NOT**
   Set/Release around every single inference call.
   - **Voice input / streaming ASR**: raise HTP to BURST the moment recording
     starts (the model begins working), keep it held through **all** interim +
     final inference chunks, and release only after the **entire** voice-input
     session ends (voice output finished). Never Set/Rel per audio chunk.
   - **TTS / multi-model pipeline**: Set BURST once before the first NPU stage
     (BERT/encoder/flow/decoder…), keep it across all cooperating models, and
     release once after the last stage completes.
   - **One-shot single inference** (classify one image, etc.): Set → infer →
     Release is fine because there is only one inference.

3. **Release BURST BEFORE destroying the model contexts** (`RelPerfProfileGlobal()`
   must run while models are still loaded), otherwise you get
   `You should set perf profile before you release it!`.

**Correct pattern (session / streaming):**

```python
# 1. Load model(s) first.
enc = QNNContext("encoder", "encoder.bin")
dec = QNNContext("decoder", "decoder.bin")

# 2. Session starts (e.g. user starts recording) → raise HTP ONCE.
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
try:
    # 3. Run MANY inferences over the whole session — BURST stays held.
    for audio_chunk in session:          # interim chunks + final chunk
        run_one_inference(enc, dec, audio_chunk)
finally:
    # 4. Session ends (voice output done) → release ONCE.
    PerfProfile.RelPerfProfileGlobal()

# 5. Only now destroy the contexts.
del enc, dec
```

> ❌ **Anti-pattern (causes HTP to ramp its clock up/down every call — slow,
> jittery latency):**
> ```python
> for audio_chunk in session:
>     PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)  # WRONG: per-chunk
>     run_one_inference(...)
>     PerfProfile.RelPerfProfileGlobal()                   # WRONG: per-chunk
> ```

### ONNX (CPU) + QNN DLC (NPU) in the same process

Standard `onnxruntime` with `CPUExecutionProvider` **does NOT conflict** with `qai_appbuilder`
(only the NPU/HTP device is exclusive — the CPU baseline runs fine alongside it). Use this pattern for format comparison:

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

## Step 6 — Interpret Results

On first load of a `.dlc`/`.bin`, WARNINGs such as `warmup_parallel_stl` / `input_data_type: float` can be ignored; cold start is ~5–60 s (graph compilation).

> The following HTP logs are all **non-fatal, ignore them** (results are correct, do not fall back to CPU based on them): `setPowerConfig error 0x32c9` (no BURST permission), `Error 0x200: failed to close queue` (queue close timing at teardown), `m_CFBCallbackInfoObj is not initialized` (v81 callback init order), `Failed to create context with file mapping` (ORT retries automatically and succeeds).

---

## Step 6.5 — Normalize the workspace to the App Builder contract (MANDATORY — run every time, right after inference passes)

> 🚨 **This step is NOT optional and NOT gated on "the user asked to export".** The moment inference is verified correct in Step 6, you MUST normalize the download into the standard workspace layout. This is what makes the model **detectable and importable** in App Builder (the "Import to App Builder" readiness dot + import panel are driven by a workspace scan that looks for `output/<model>_<label>.{bin,dlc}`). Skipping it is the direct cause of the recurring "the model is on disk but App Builder can't see / import it" bug.

### Why this is mandatory (root cause)

`model-hub` and `model-builder` MUST produce the **exact same workspace / `app_pack` contract** — App Builder cannot tell (and must not need to tell) whether a model was downloaded or self-converted. `model-builder`'s pipeline always writes `output/<model>_<precision>.{bin,dlc}` + `inference_manifest.json`; App Builder's readiness scan (`ImportScanBinsUseCase._scan_workspace`, `src/qai/app_builder/application/use_cases/deferred_routes.py:1022`) ONLY looks under `<workdir>/output/` for files named `<workdir-name>_<label>.{bin,dlc}` ≥ 1 MiB whose `<label>` is in `_LABEL_TO_PRECISION` (deferred_routes.py:888).

But a fresh AI Hub download does NOT match that contract: the ZIP extracts into a nested `<model>-<soc>-qnn_dlc-<prec>/` subfolder holding e.g. `resnet50.dlc`, with **no `output/` directory and no `inference_manifest.json`**. Under that raw layout the scan returns **empty** → no readiness dot, empty import panel. Running the mapper below normalizes the download into the model-builder-identical layout so the scan (and the whole import/export chain) succeeds — verified: raw layout → 0 variants; after mapper → the FP16/INT8/… variant is detected.

So `model-hub` does **not** re-implement anything: it just **normalizes the downloaded model into the model-builder layout** (`复用 > 重造`), which is required for the model to be usable in App Builder at all.

### Run the mapper (MANDATORY after Step 6)

Use the `model-hub` mapper `aihub_to_manifest.py`. It reads the AI Hub `metadata.json` (auto-found even when it lives in the nested `<model>-<soc>-qnn_dlc-<prec>/` subfolder), maps input/output shapes/dtypes/layout (NHWC vs NCHW — see Issue 15) into the standard `inference_manifest.json`, and **copies** (never moves — keeps your download intact) the largest qualifying context binary — including a `.dlc` nested in a subfolder — into `output/<model_name>_<label>.{bin,dlc}` (the exact name App Builder's scan + the exporter's `find_context_binary` expect).

```powershell
& "<python_arm64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-hub\scripts\aihub_to_manifest.py" `
  --workdir "C:\WoS_AI\<model>" `
  --model-name <model> `
  --precision <w8a8|float|w8a16|...> `
  --output-type <classification|detection|super_resolution|segmentation|text|audio|raw> `
  --vendor "<original author, e.g. Google / Meta / Ultralytics>"
```

> ⚠️ **`--workdir` MUST be the top-level `C:\WoS_AI\<model>\` (NOT the nested `...-qnn_dlc-<prec>\` subfolder), and `--model-name` MUST equal that top-level folder name** — App Builder's scan derives the expected filename prefix from `<workdir>.name`, so `output/<model>_<label>.dlc` only matches when `<model>` == the workdir folder name.

> The mapper prints a **"fields inferred — please confirm"** list for anything it could not read from `metadata.json` (e.g. `output.type`, preprocessing mean/std, `num_classes`, label assets). Review it; re-run with `--output-type` / `--num-classes` / `--vendor` overrides if a guess is wrong. `output.type` is CRITICAL — it decides which runner template the exporter generates.

### Verify the workspace is now scannable (quick self-check)

After the mapper runs, confirm `C:\WoS_AI\<model>\output\<model>_<label>.{bin,dlc}` exists (≥ 1 MiB) and `C:\WoS_AI\<model>\inference_manifest.json` exists. At this point the model is detectable by App Builder's "Import to App Builder" readiness scan. Report the normalized `output/` path + manifest path in your final summary.

> 🚨 **MANDATORY — every-turn workspace path in the final summary.** In the **final summary of EVERY reply turn** (not just the last turn of the whole task, and even when this turn is only an intermediate step), you **MUST** print the model's top-level workspace path `C:\WoS_AI\<model>` as a plain, user-visible text line. App Builder's promote-ready detection runs at the end of **each** turn and extracts this path from your final summary, then scans its `output/` for variants. A conversation is often multi-turn (the user asks follow-ups, or the conversion succeeds only after several rounds of fixing) — so the path must appear in the summary of whichever turn the model became ready, which is why every turn must include it. Missing it = the "Import to App Builder" ready-dot / CTA never appears.

---

## Phase 7 — Export the app_pack + Promote to App Builder (optional)

> Step 6.5 already made the model **detectable/importable** in App Builder. Phase 7 is the further step of pre-building the full `app_pack/` on disk and importing it — the **same `app_pack/` contract that `model-builder` produces**. Only Phase 7 is optional (do it when the user wants the model actually promoted / a ready-to-import Pack pre-built); Step 6.5 is always required.

### When to use

- Step 6.5 has been done (workspace normalized — this is a prerequisite).
- The user wants this AI Hub model available in App Builder (demo / benchmark / build an app on top of it), or wants the `app_pack/` pre-generated for import.

### Step 7.2 — Run the shared exporter (reuses model-builder's chain, unchanged)

```powershell
& "<python_x64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_export.py" `
  --workdir "C:\WoS_AI\<model>" `
  --model-name <model> `
  --precision <same precision as 7.1>
```

This creates `C:\WoS_AI\<model>\app_pack\` with `manifest.json` / `runner.py` / `requirements.txt` / `weights/` / `assets/` / `examples/` / `provenance/` / `_candidate.json` (`ready: true` when structural checks pass) — identical structure to a `model-builder` export.

### Step 7.3 — Validate + import

```powershell
& "<python_x64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_validate.py" `
  "C:\WoS_AI\<model>\app_pack"
```

After validation passes, use the "Promote to App Builder" action in the UI, or call `POST /api/appbuilder/import/commit` directly. Full field spec for `inference_manifest.json` and the export contract: `${APP_ROOT}\factory\chat_features\model-builder\references\pack_export.md`.

> ⚠️ **Note for multi-sub-model packages (VOICE_AI TTS, streaming ASR encoder/decoder/joiner):** these have several `.bin` files and a custom pipeline. The single-`.bin` mapper above targets single-graph models (classification/detection/SR/segmentation). For multi-sub-model packs, the existing App Builder Packs `melotts-zh` / `zipformer-zh` / `whisper-base` under `factory/app_builder/models/` are the reference shape; follow their `manifest.json` + `runner.py` layout rather than the single-bin mapper.

---

## ⚠️ Known Issues (All Models)

---

### Issue 1: ZIP extraction fails

**Symptom:** `tar: This does not look like a tar archive`
**Cause:** Using `tar -xf` to extract a `.zip`.
**Fix:** Always use Python `zipfile`:
```python
import zipfile; zipfile.ZipFile("model.zip").extractall("out/")
```

---

### Issue 2: Unix tools not found in exec

**Symptom:** `[hint] Detected Unix tool ls. Possible cause: PortableGit not installed`
**Fix:** Explicitly specify `shell='sh'`:
```python
exec("ls C:/WoS_AI/", shell='sh')
exec("grep -r pattern dir/", shell='sh')
```
PortableGit is at `%LOCALAPPDATA%\QAIModelBuilder\git\`, available under `shell='sh'`.

> ⚠️ **Do not count/filter file numbers with `dir | find /c`** (PortableGit's Unix `find` intercepts it and reports `No such file or directory`). Use PowerShell `(Get-ChildItem <path>\*.raw).Count` or Python `glob` instead.

---

### Issue 3: Download timeout

**Symptom:** `[process killed: timeout after 30.0s]`
**Fix:** Use `timeout=300` or omit it (0 = no limit):
```python
exec('curl -k -L "<url>" -o "out.zip" --create-dirs', timeout=300)  # -k: disable SSL verify (see Issue 19)
```

---

### Issue 4: Chinese print causes UnicodeEncodeError

**Symptom:** `UnicodeEncodeError: 'charmap' codec can't encode characters`
**Fix:** Add at the top of the script:
```python
import sys; sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```
> Double safety: use ASCII directly for log symbols (`->` for `→`, `[OK]` for `✓`), not depending on terminal encoding.

---

### Issue 5: QNN native dtype trap (uint16 reported as ufp16)

**Symptom:** Output is NaN or absurd values.
**Cause:** With `output_data_type='native'`, QNN wraps a quantized `uint16` in a float16 container and reports it as `ufp16`, while the bits are actually a uint16 integer.
**Detection:** `"dtype":"uint16"` in `metadata.json` + presence of `"quantization_parameters"` → hit.
**scale/zero_point MUST be taken from `metadata.json`, do not compute them yourself.**

```python
def quantize(x, scale, zp):   # float32 → uint16 viewed as float16
    return np.clip(np.round(x / scale) + zp, 0, 65535).astype(np.uint16).view(np.float16)

def dequantize(raw, scale, zp):  # QNN native out → float32
    return (raw.view(np.uint16).astype(np.float32) - zp) * scale

model = QNNContext("m", "m.bin", input_data_type="native", output_data_type="native")
out = dequantize(np.array(model.Inference([quantize(x, scale=1e-4, zp=32768)])[0]),
                 scale=3e-4, zp=32329)
```

---

### Issue 6: ARM64 Windows missing native packages (torchaudio / numba / MeCab)

All mocks must be placed before the model import.

**torchaudio mock:**
```python
import sys, types, importlib.util
_ta = types.ModuleType("torchaudio")
_ta.__spec__ = importlib.util.spec_from_loader("torchaudio", loader=None)
_ta.__version__ = "0.0.0"
sys.modules.setdefault("torchaudio", _ta)
```

**numba mock:**
```python
_nb = types.ModuleType("numba")
def _jit(*a, **kw): return (lambda f: f) if not (len(a)==1 and callable(a[0])) else a[0]
# ⚠️ Setting only int32/float32 to None is not enough: melo internally has
# type-indexing syntax like `numba.int32[:, ::1]`; None does not support __getitem__
# and will crash at import. Use a placeholder type that supports chained indexing.
class _NumbaType:
    def __getitem__(self, _key): return self   # supports chained indexing like int32[:, ::1] / [:, :, ::1]
_nb_type = _NumbaType()
_nb.jit = _jit
_nb.void = None
_nb.int32 = _nb.int64 = _nb.float32 = _nb.float64 = _nb_type
sys.modules.setdefault("numba", _nb)
```

**MeCab (Japanese):** patch `melo/text/japanese.py`, wrapping the module-level import in `try/except`. See `models/melotts_zh/NOTES.md § Dependency Patch`.

**python-mecab-ko (Korean):** `melo/text/korean.py` also imports `python-mecab-ko` at the module top level (fails to compile on ARM64). Handle the same as japanese: before `import melo`, inject `melo.text.korean` into `sys.modules` as a stub, or wrap its top-level import in `try/except`. When running only Chinese TTS this module is not used, so a stub is enough.

> ℹ️ **Install all dependencies for VOICE_AI/TTS models in one go** (including `--no-deps` items and the mock list); see the corresponding `models/<id>/NOTES.md § Quick Start`. **Install everything at once per the list, do not trial-and-error package by package.**

---

### Issue 7: When the model I/O shape disagrees with metadata.json, trust `getInputName()` / `getInputShapes()`

`metadata.json` sometimes describes the I/O of the ONNX model, while the actually loaded QNN `.bin` may, after compilation, have subtle differences in input order or shape.
**Always confirm the actual order and shape with `model.getInputName()` and `model.getInputShapes()`, then build the input list.**

---

### Issue 8: For NPU inference use only `qai_appbuilder` to load `.bin` / `.dlc`; do not use `onnxruntime` to run the NPU

**Principle:** All on-device (NPU/HTP) inference in this skill goes through `qai_appbuilder.QNNContext` loading a QNN context binary (`.bin`) or `.dlc`. The downloaded `.bin` filename varies by model (e.g. `encoder.bin` / `model.bin`); trust `metadata.json` and the actual extracted files.

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.OFF)  # 2.47 signature: (runtime, log_level, profiling_level)
model = QNNContext("name", "encoder.bin")   # load the model's .bin, infer on the NPU
outputs = model.Inference([input_tensor])
```

> ℹ️ A `.bin` is bound to the QAIRT version it was compiled with; a version mismatch reports `Error code: 5000`. The QNN runtime bundled with `qai_appbuilder` is usually backward-compatible with `.bin` files compiled by older versions (e.g. a `.bin` compiled with 2.45 can be loaded by `qai_appbuilder` 2.46).
> ℹ️ Only the plain `ONNX` format uses `onnxruntime`'s `CPUExecutionProvider` for the **CPU baseline comparison**; never run the model on the NPU with it.

---

### Issue 9: `qai_appbuilder.QNNContext` crash (0xC0000005), caused by not calling `QNNConfig.Config()` first

**Symptom:** The Python process exits directly with exit code `3221225477` (0xC0000005, ACCESS_VIOLATION), with no Python traceback.
**Cause:** The global state of the C extension `appbuilder.pyd` (log level, profiling level, HTP backend) must be initialized by `QNNConfig.Config()`, otherwise the constructor dereferences an uninitialized pointer.

**Fix: the first qai_appbuilder call in every process must be `QNNConfig.Config()`:**
```python
from qai_appbuilder import QNNContext, QNNConfig, LogLevel, Runtime, ProfilingLevel
QNNConfig.Config(Runtime.HTP, LogLevel.ERROR, ProfilingLevel.OFF)  # 2.47 signature: (runtime, log_level, profiling_level) — no lib-dir arg
model = QNNContext("name", "model.bin")             # only then can you load the model
```
Omitting `qnn_lib_path` automatically uses the package's `libs/` directory; no need to specify the DLL path manually.

> ✅ Version compatibility: a `.bin` compiled by an older QAIRT (e.g. 2.45) can be loaded normally by a newer `qai_appbuilder` (e.g. 2.46).

---

### Issue 10: When numerically comparing NPU inference against an ONNX CPU baseline, separate processes are more reliable

**Note:** Multiple `.bin`/`.dlc` contexts can be loaded simultaneously within the same `qai_appbuilder` process (e.g. zipformer's encoder/decoder/joiner trio); this is normal usage and does not conflict.

For a "NPU result vs ONNX CPU baseline" numerical comparison, it is recommended to run them in **separate processes**, each saving its output as `.npy` then comparing them uniformly, to avoid the two runtimes/dependencies interfering with each other in the same process:
```python
# Process A: load .bin with qai_appbuilder, infer on NPU, save outputs/qnn/*.npy
# Process B: run a plain .onnx baseline with onnxruntime + CPUExecutionProvider, save outputs/cpu/*.npy
# Process C: load both sets of .npy, compute cosine similarity
```

> ✅ A float model's NPU output vs CPU baseline is usually cosine > 0.999; for quantized models judge by the accuracy threshold (≥0.95).

---

### Issue 11: The `Failed to create context with file mapping` warning can be ignored

```
[W] Failed to create context with file mapping enabled. ... Retrying with feature disabled.
```
ORT retries automatically and succeeds; this is not an error and needs no handling.

---

### Issue 12: The input list of `QNNContext.Inference()` must match the order of `getInputName()` exactly

**Symptom:** `Inference()` hangs forever or produces garbled output.
**Cause:** Inputs are matched by index, not by name. The input registration order of the compiled graph may differ from the ONNX order.

```python
print(model.getInputName())    # confirm order
print(model.getInputShapes())  # confirm shapes
inputs = [t0, t1, ...]         # strictly in getInputName() order
outputs = model.Inference(inputs)

out_dict = dict(zip(model.getOutputName(), outputs))  # also look up outputs by name
result = out_dict["output_name"]
```

> ⚠️ A wrong order causes a silent hang without raising an exception. Multi-input models (such as the cache tensors of streaming ASR) are especially dangerous.

---

### Issue 13: VOICE_AI format needs a custom multi-model pipeline

**Cause:** The VOICE_AI ZIP directly contains `.bin` context binary files, loadable directly with `qai_appbuilder.QNNContext`, no Voice AI SDK needed.
```
encoder.bin / flow.bin / decoder.bin / bert_wrapper.bin
config.json    ← sample rate, speaker ID, etc.
metadata.json  ← each sub-model's I/O shapes, quantization params
```
**Fix:** Call the sub-models in pipeline order one by one. Full example in `models/melotts_zh/NOTES.md`.

> ⚠️ **The VOICE_AI `.bin` is a proprietary packaging format, loadable only with `qai_appbuilder.QNNContext`; never run it with command-line tools like `qnn-net-run` / `snpe-net-run`** — those CLIs expect a standard QNN context binary and will fail or produce garbage on VOICE_AI sub-model `.bin` files. Quantized sub-models (such as flow/decoder) also need their I/O handled per the native dtype rule in Issue 5.

---

### Issue 14: An ONNX ZIP may contain an external weights file; the two files must be in the same directory

PyTorch 2.x `torch.onnx.export` splits a large model into `<m>.onnx` (the graph, a few KB) + `<m>.data` (the weights, tens to hundreds of MB), with the `.onnx` referencing `.data` by relative path.
**Always extract the whole ZIP and keep the two files in the same directory.** Copying the `.onnx` alone will fail to load because it cannot find `.data`.

---

### Issue 15: QNN DLC uses NHWC, ONNX uses NCHW

AI Hub QNN DLC image models take input `[1,H,W,C]`, corresponding to ONNX `[1,C,H,W]`; they are not interchangeable.
```python
nhwc = preprocess(img)              # (1,H,W,C) → QNN DLC
nchw = nhwc.transpose(0, 3, 1, 2)  # (1,C,H,W) → ONNX
```
> Before writing preprocessing, check the `shape` in `metadata.json` to confirm the layout.

---

### Issue 16: Multi-sub-model ASR/Transducer (encoder/decoder/joiner)

- **Sub-models cannot be mixed across release packages**: encoder/decoder/joiner must come from **the same AI Hub package**. Even with the same name (e.g. Zipformer), different versions have different hidden dimensions (e.g. 512-dim vs the 320-dim of sherpa-onnx 14M); mixing reports `Got invalid dimensions: Got 512 Expected 320`.
- **Debug order encoder → decoder → joiner** (not in reverse): when recognition outputs only 1~2 tokens, first check **whether the encoder output L2 norm is abnormal** (normally usually <20; a value of ~50 means numerical explosion, which will overflow the downstream joiner's FP16), rather than diving straight into the joiner.
- **Do a numerical health check after loading**: use a clip of real speech to inspect the encoder output norm distribution; if abnormal, stop and investigate (it may be a difference between the compile-time QAIRT version and runtime HTP behavior, see below).

> ℹ️ A QNN context binary (`.bin`) is sensitive to the **compile-time QAIRT version**: on the same chipset, a graph compiled with an older version occasionally has numerical precision issues on a newer HTP. Before starting, compare `tool_versions.qairt` in `metadata.json` with `_version` in this machine's `qairt_env.json`; if they differ, watch out for the norm health check in advance.

---

### Issue 17: Invalid test audio/input causes a false "empty result" judgment

Synthetic audio (sine wave/silence) makes ASR inference results empty, easily misjudged as a code bug. **Verify input validity before inference:**
```python
energy = float(np.sqrt(np.mean(waveform.astype(np.float32)**2)))
if energy < 1e-3:
    print("[WARN] audio looks like silence/synthetic — empty result is expected")
```
Same for image tasks: prefer real images from `${APP_ROOT}\samples\images\`; Top-K on synthetic images is meaningless.

---

### Issue 18: 🚨 NEVER full-disk recursively scan to find a model package (it hangs for 30+ minutes)

**Symptom:** `Get-ChildItem -Recurse` runs for tens of minutes without returning, the sub-agent times out, the terminal appears frozen.

**Root cause:** Trying to "find a locally cached model package" by doing `-Recurse` full-disk searches for `*.bin`/`*.dlc`/`metadata.json` over an **unbounded root directory** (`C:\`, `C:\Shared`, `C:\WoS_AI`, `C:\Users`). Under `C:\Shared` are the entire project repo + the V1/v0.5 sister repos + `node_modules` + `.venv` + `vendor/whl` + `data/`, hundreds of thousands of files; recursively stat-ing them inevitably takes tens of minutes or even hangs. The cost depends on "how many files were traversed", regardless of "how many matched"; `-ErrorAction SilentlyContinue` does not speed it up.

**Iron rule: this skill locates model packages with fixed paths and NEVER does a full-disk search:**

1. **The "find a local cache" step is not needed**: the normal flow of this skill is Step 1 (webfetch to get the link) → Step 3 (curl download to the fixed directory `C:/WoS_AI/<model>/`). The model package location is **always a known fixed path**; there is no "search locally by luck" step.
2. **If `qai_hub` is installed, download with it directly** (installed if `qai_hub.__version__` can print), do not fall back to searching the local cache.
3. **If you really must check existing downloads**: only `Get-ChildItem` (**without `-Recurse`**) the following **fixed shallow directories**, each returning in 0.x seconds:
   - `C:\WoS_AI\<model>\` (this skill's working directory, where download/extract happens)
   - `C:\Users\<user>\.qaihub\` (qai_hub's official cache, a shallow directory)
4. **Always forbidden**: doing `-Recurse` on root/large directories like `C:\`, `C:\Shared`, `C:\Users`, `C:\WoS_AI` (without a `<model>` subdirectory). When you need to confirm the contents of a particular model subdirectory, only recurse down to the level of `C:\WoS_AI\<specific model name>\`.

> ⚠️ **Also applies when dispatching a sub-agent to execute this skill**: if the sub-agent task prompt contains phrasing like "search `C:\Shared` for packages" / "look for any existing `.bin` files", it must be rewritten into the fixed shallow-directory check in point 3 above, otherwise the sub-agent will faithfully execute it as a full-disk recursion and hang.

> 🚨 **MANDATORY — before inference you must first execute Step 0.5 (read `NOTES.md`)**, which contains known issues, I/O shapes, and a ready-made inference script.

```
factory/chat_features/model-hub/models/
├── beit/          NOTES.md + infer_beit.py       (QNN_DLC w8a16, image classification)
├── melotts_zh/    NOTES.md + infer_melotts_zh.py (VOICE_AI, TTS)
├── resnet50/      NOTES.md                       (QNN_DLC + ONNX float, comparison verified)
└── zipformer/     NOTES.md + infer_zipformer.py  (QNN_CONTEXT_BINARY, qai_appbuilder, streaming ASR)
```

**After completing a new model, you must create the corresponding `NOTES.md`**, recording:
- Download links, chipset suffix, file structure
- Sub-models I/O table (name, shape, dtype)
- Performance data (latency, RTF)
- Model-specific known issues (in Issue Z-x format)
- Inference command

---

### Issue 19: SSL certificate verification failure on WoS (curl exit code 35 / Python urllib SSL error)

**Symptom:** `curl` exits with code 35 (`SSL connect error`) or Python `urllib` raises `ssl.SSLError` / `certificate verify failed` when downloading from `qaihub-public-assets.s3.us-west-2.amazonaws.com`.

**Cause:** The Windows on Snapdragon (WoS) environment's system CA bundle sometimes does not include the intermediate certificate for the S3 endpoint, causing TLS handshake failure.

**Fix — always use `-k` with curl and disable cert verification in Python urllib:**

```python
# curl: add -k to skip SSL certificate verification
exec('curl -k -L "<url>" -o "C:/WoS_AI/<model>/<file>.zip" --create-dirs', timeout=300)
```

```python
# Python urllib: create an unverified SSL context
import ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
urllib.request.urlopen(req, context=ctx, timeout=15)
```

> ⚠️ **Always apply `-k` proactively** — do not wait for a 35/SSL error to appear. The S3 host is a known Qualcomm public asset server; skipping cert verification here is safe.
> ⚠️ **S3 returns 403 for HEAD and Range-GET even on publicly readable objects** — always use a plain GET (optionally read just the first few bytes and close) to probe URL existence. HEAD/Range probing will give misleading 403 results.

---

### Issue 20: `os._exit()` 触发进程异常退出（exit code `0xC0000409`）

在 `qai_appbuilder` 推理脚本中调用 `os._exit()` 会导致进程崩溃，exit code `0xC0000409`。让脚本正常跑完即可，Python 会有序析构所有 `QNNContext`，正常退出。

若确实需要提前退出，必须先 `del` 所有 `QNNContext` 对象，再调用 `os._exit()`：

```python
del model   # 先析构，触发 C++ 层释放
os._exit(0) # 此时再 exit 不会崩溃
```

> ⚠️ `DSP_INFO UNSUPPORTED_KEY: 49/50` 和 `Error 0x200: failed to close queue` 是非致命 HTP teardown 日志（见 Step 6），**不需要也不应该用 `os._exit()` 来规避它们**。
---

## Quick Reference

```
0.   detect   Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" | Where qcadsp* → ImagePath → SoC code
0.5  NOTES    read factory/chat_features/model-hub/models/<model_id>/NOTES.md  ← MANDATORY FIRST
              (contains download URL, I/O shapes, known issues, ready-to-run infer script)
1.   webfetch https://huggingface.co/qualcomm/<ModelName>       → download URL + I/O  (skip if NOTES.md has it)
2.   curl     download ZIP to C:/WoS_AI/<model>/                → -k -L, timeout=300  (Issue 19: always -k)
3.   python   zipfile.ZipFile(...).extractall(...)               → NEVER tar
4.   read     metadata.json                                      → shapes, dtypes, quant  (skip if NOTES.md has it)
5.   check    Issue list above                                   → avoid known pitfalls
6.   python   "<python_arm64_venv>\Scripts\python.exe" infer_*.py
6.5  normalize (MANDATORY, Step 6.5) aihub_to_manifest.py → output/<model>_<label>.{bin,dlc} + inference_manifest.json
              → makes the model detectable/importable in App Builder (identical layout to model-builder). NEVER skip.
7.   report   results; add NOTES.md for new model-specific issues
8.   export   (optional, Phase 7) qai_pack_export.py → app_pack/ → Promote to App Builder
```
