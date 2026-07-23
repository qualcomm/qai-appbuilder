---
name: Model Hub
description: Model Hub — download pre-exported models from Qualcomm AI Hub, run inference on-device, and export them to App Builder as ready-to-import Packs (same app_pack contract as model-builder). Supports QNN_CONTEXT_BINARY, QNN_DLC, VOICE_AI, ONNX, TFLITE formats. All on-device (NPU/HTP) inference runs through qai_appbuilder (QNNContext), loading the QNN context binary (.bin) or .dlc. Standard onnxruntime is used only for an optional CPU baseline comparison.
tags: aihub, model-hub, inference, qualcomm, on-device, qnn, tts, asr, classification, export, app-builder
use_for: Download pre-exported models from AI Hub, run inference on Snapdragon X Elite / X2 Elite without conversion, then always normalize the download into the App Builder workspace contract (Step 6.5, mandatory) so it is importable; optionally pre-build the full app_pack and promote it (Phase 7).
homepage: "https://aihub.qualcomm.com/compute/models"
---

# Model Hub — AI Hub Model Download, Inference & App Builder Export

> **How to use this SKILL:** ① Pass the **Pre-Flight Self-Check** below (MANDATORY gate). ② Follow the 8-step workflow; open the referenced `references/*.md` when you reach a step that needs it. ③ Hold the **A-class disciplines** (BURST lifecycle, Step 6.5 normalize, non-fatal HTP logs, Do NOT rules) the whole run.

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

> **Lesson**: a sub-agent inherits **NOTHING** from the main agent's context — it only executes the prompt as written. Once (2026-06) a prompt polluted by `model-builder` paths told a sub-agent to search `C:\Shared`/`C:\WoS_AI` for `.bin` → 30+ min hang (Issue 18). So when dispatching a sub-agent to this skill:

1. **Main agent MUST first `read` the full SKILL.md** (the one-line `Use for` catalog summary is not enough).
2. **Sub-agent prompt's first instruction MUST be "read the full SKILL.md before acting"** — sub-agent has blank context; all constraints (Issue 18, fixed-path download, `qai_appbuilder`) must be learned by reading.
3. **NEVER include any `model-builder` script/path/tool** (`run_pipeline.py`, `qnn-onnx-converter`, `qairt_sdk_root`, etc.) — those are for custom ONNX conversion, useless here and induce wrong searches.
4. **NEVER phrase locate-the-package as "search/look-for in `C:\`/`C:\Shared`/`C:\Users`/`C:\WoS_AI`"** (Issue 18). Only fixed paths: `qai_hub` download / `curl` to `C:/WoS_AI/<model>/`.

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

> 🚨 **Inference tool rule (MANDATORY)**: NPU always via `qai_appbuilder.QNNContext` loading `.bin`/`.dlc`; `onnxruntime` (`CPUExecutionProvider`) **only** for optional CPU baseline comparison, **never** for NPU.

**Switch to the `model-builder` skill when**: ① you have a custom ONNX/PyTorch model and AI Hub has no corresponding pre-compiled package; ② you need to re-quantize/compile a custom NPU `.bin`.

---

## Trigger Phrases

- "download model from aihub and run inference"
- "run \<ModelName\> on device" / "run inference for \<ModelName\>" / "infer \<ModelName\> for me"

---

## Environment

> ℹ️ **`${APP_ROOT}` = this repo (QAIModelBuilder) root**. Resolved to the actual path at runtime (usually the `exec` CWD; when unsure, infer from `qairt_env.json` paths). Never hardcode a machine-specific absolute path.

Read paths from `${APP_ROOT}\data\config\qairt_env.json`:

| Key | Role |
|-----|------|
| `python_arm64_venv` | ARM64 Python 3.13 — ALL inference runs here |
| `qairt_sdk_root` | QAIRT SDK root (reference only) |

Working directory: `C:/WoS_AI/<model_name>/`

> ⚠️ **`read`/`skill` display expands placeholders like `${APP_ROOT}` to absolute paths, but the file on disk still contains `${APP_ROOT}`** — do NOT copy the displayed absolute path into `edit`'s `oldText` (match will fail); do NOT replace `${APP_ROOT}` with an absolute path when editing. To inspect raw bytes: `python -c "print(open(r'<file>').read()[<idx>:<idx+100>])"`.

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

> 🚨 **MANDATORY — before Step 1**, check the local NOTES.md. Skipping is the root cause of repeated pitfalls (dependency trial-and-error, I/O mismatch, wrong tool).

> ⚠️ **Directory name may differ from the model_id given** (task says `inceptionv3` / `BEiT` but directory is `inception_v3` / `beit`). **NEVER guess the path or use a single glob** — always the two-step check below.

```
# 1. list the models/ dir to see exact directory names
list "factory/chat_features/model-hub/models/"

# 2. read NOTES.md using the exact name from step 1
read "factory/chat_features/model-hub/models/<exact_dir_name>/NOTES.md"
```

If no matching directory exists → continue to Step 1, and create NOTES.md at the end of the task to record new findings.

The content in NOTES.md lets you **directly skip** most of the work in Step 1~4:

| If NOTES.md has | Steps you can skip |
|--------------|-------------|
| Download links | The webfetch lookup in Step 1 |
| Sub-models I/O table | Reading metadata.json in Step 4 |
| Known Issues (e.g. input order, dtype) | Avoid hitting the pitfall before looking it up |
| `infer_<model>.py` inference script | Skip Step 5 entirely, run it directly |


---

## Step 1 — Look Up the Model

**How to find the download URL** — three methods, in order of preference:

1. **Method A — Parse AI Hub page HTML** (preferred, always works without login). Python `urllib` + regex on `https://aihub.qualcomm.com/models/<model_id>` extracts all S3 zip download links directly from the Next.js data island in the HTML.
2. **Method B — webfetch HuggingFace** (fallback). `https://aihub.qualcomm.com/models/<model_id>` + `https://huggingface.co/qualcomm/<ModelName>`. HuggingFace has download links, `metadata.json` I/O shapes, and performance tables. ⚠️ **Do NOT use `qai_hub.get_models(name=...)`** — that API doesn't accept `name=` (raises `unexpected keyword argument`).
3. **Fallback — Construct S3 URL from GitHub** (when both above are blocked). Get exact `model_id` from `github.com/qualcomm/ai-hub-models/tree/main/src/qai_hub_models/models/`, read `release-assets.yaml` for formats/precisions, construct `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<model_id>/releases/v<ver>/<model_id>-<runtime>-<precision>.zip` and probe with plain GET (**not HEAD/Range** — S3 returns 403 for those). Start with `v0.56.0` (verified 2026-06); try `v0.55.0` as fallback. `float` precision sometimes inaccessible while `w8a8` works.

**Complete code for all three methods** (Python `urllib` + `ssl.CERT_NONE` on WoS, S3 link regex, GitHub `release-assets.yaml`, S3 URL construction + probe with 403/timeout tolerance, chipset-suffix pattern e.g. BEiT) → `references/workflow-details.md § Step 1`.
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

**Templates & command details** — inference templates (`infer_classify.py` / `infer_detect.py` / `infer_segment.py` / `infer_sr.py` / `infer_generic.py` at `${APP_ROOT}/factory/chat_features/model-builder/scripts/inference/`), run command example, test-image priority (built-in `${APP_ROOT}\samples\images\` first), and standard classification preprocessing (NHWC `[0,1]`, resize shortest-side + center-crop) → `references/workflow-details.md § Step 5`.

> ⚠️ Check `metadata.json` `value_range` before writing preprocessing (`[0,1]` = /255 only; `[-1,1]` = /127.5-1; `[0,255]` = float32 cast). For ONNX, remember to transpose NHWC→NCHW (Issue 15).
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

Standard `onnxruntime` with `CPUExecutionProvider` **does NOT conflict** with `qai_appbuilder` (only the NPU/HTP device is exclusive — the CPU baseline runs fine alongside it). Complete pattern (`QNNConfig.Config` + load both + cosine similarity check) → `references/workflow-details.md § ONNX (CPU) + QNN DLC (NPU) in the same process`.
---

## Step 6 — Interpret Results

On first load of a `.dlc`/`.bin`, WARNINGs such as `warmup_parallel_stl` / `input_data_type: float` can be ignored; cold start is ~5–60 s (graph compilation).

> The following HTP logs are all **non-fatal, ignore them** (results are correct, do not fall back to CPU based on them): `setPowerConfig error 0x32c9` (no BURST permission), `Error 0x200: failed to close queue` (queue close timing at teardown), `m_CFBCallbackInfoObj is not initialized` (v81 callback init order), `Failed to create context with file mapping` (ORT retries automatically and succeeds).

---

## Step 6.5 — Normalize the workspace to the App Builder contract (MANDATORY — run every time, right after inference passes)

> 🚨 **This step is NOT optional and NOT gated on "the user asked to export".** The moment inference is verified correct in Step 6, you MUST normalize the download into the standard workspace layout. This is what makes the model **detectable and importable** in App Builder (the "Import to App Builder" readiness dot + import panel are driven by a workspace scan that looks for `output/<model>_<label>.{bin,dlc}`). Skipping it is the direct cause of the recurring "the model is on disk but App Builder can't see / import it" bug.

### Why this is mandatory (root cause)

`model-hub` and `model-builder` MUST produce the **same workspace / `app_pack` contract** — App Builder cannot tell whether a model was downloaded or self-converted, and must not need to. `model-builder` always writes `output/<model>_<precision>.{bin,dlc}` + `inference_manifest.json`; App Builder's readiness scan (`ImportScanBinsUseCase._scan_workspace` in `deferred_routes.py`) only detects `<workdir>/output/<workdir-name>_<label>.{bin,dlc}` ≥ 1 MiB where `<label>` ∈ `_LABEL_TO_PRECISION`.

A fresh AI Hub download does NOT match: the ZIP extracts into a nested `<model>-<soc>-qnn_dlc-<prec>/` subfolder holding e.g. `resnet50.dlc`, with **no `output/` and no `inference_manifest.json`** → scan returns empty → no readiness dot, empty import panel. The mapper below normalizes it into the model-builder-identical layout — reuse, not reimplementation.

### Run the mapper (MANDATORY after Step 6)

Use `aihub_to_manifest.py`. It reads AI Hub `metadata.json` (auto-found in nested subfolders), maps I/O shapes/dtypes/layout (NHWC vs NCHW — Issue 15) into `inference_manifest.json`, and **copies** (does not move — download stays intact) the largest qualifying context binary into `output/<model_name>_<label>.{bin,dlc}` — the exact name App Builder's scan + the exporter's `find_context_binary` expect.

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

> Step 6.5 already made the model **detectable/importable** in App Builder. **Phase 7 is optional** — do it when the user wants the `app_pack/` pre-built and imported. Step 6.5 is **always required**; Phase 7 is not.

**Trigger conditions:** Step 6.5 done + user wants the model actually promoted / a ready-to-import Pack pre-built.

**Full commands** (Step 7.2 `qai_pack_export.py`, Step 7.3 `qai_pack_validate.py`, `app_pack/` layout, multi-sub-model note for VOICE_AI / streaming ASR) → `${APP_ROOT}/factory/chat_features/model-hub/references/workflow-details.md § Phase 7`.
---

## ⚠️ Known Issues — Symptom Index (all 20 issues → `references/known-issues.md`)

**Symptoms below are one-liners for lookup only.** Full symptom / cause / fix /
copy-paste code for every issue lives in
`${APP_ROOT}/factory/chat_features/model-hub/references/known-issues.md`
(`### Issue N:`). Read that file when your run hits one of these — do not carry
all 20 issues in context up front.

| # | Trigger / symptom (one-liner) | Section in `known-issues.md` |
|---|---|---|
|  1 | `tar: This does not look like a tar archive` — used `tar` on a `.zip` | `### Issue 1: ZIP extraction fails` |
|  2 | `[hint] Detected Unix tool ls. PortableGit not installed` — `exec()` needs `shell='sh'` | `### Issue 2: Unix tools not found in exec` |
|  3 | `[process killed: timeout after 30.0s]` — download timeout | `### Issue 3: Download timeout` |
|  4 | `UnicodeEncodeError: 'charmap' codec` on Chinese `print` | `### Issue 4: Chinese print causes UnicodeEncodeError` |
|  5 | 🚨 NaN / absurd output with `output_data_type='native'` — QNN reports uint16 as ufp16 (needs manual quantize/dequantize with scale/zp from `metadata.json`) | `### Issue 5: QNN native dtype trap` |
|  6 | 🚨 ARM64 Windows imports fail: `torchaudio`/`numba`/MeCab/`python-mecab-ko` (needs pre-import mocks incl. `_NumbaType` for `int32[:, ::1]` chained indexing) | `### Issue 6: ARM64 Windows missing native packages` |
|  7 | Model I/O shape disagrees with `metadata.json` — trust `getInputName()` / `getInputShapes()` | `### Issue 7: I/O shape disagreement` |
|  8 | NPU inference: use `qai_appbuilder.QNNContext` only, not `onnxruntime` (onnxruntime is CPU-baseline only) | `### Issue 8: qai_appbuilder-only for NPU` |
|  9 | 🚨 `QNNContext` crash exit `3221225477` / `0xC0000005` — must call `QNNConfig.Config()` first | `### Issue 9: QNNContext crash (0xC0000005)` |
| 10 | NPU-vs-ONNX-CPU cosine comparison: run in **separate processes**, save `.npy`, compare afterwards | `### Issue 10: Separate processes for baseline compare` |
| 11 | `Failed to create context with file mapping` warning — ignore, ORT auto-retries | `### Issue 11: file-mapping warning is safe` |
| 12 | 🚨 `Inference()` hangs / garbled — input list order MUST match `getInputName()` exactly (index-based, not name-based) | `### Issue 12: Inference input order` |
| 13 | VOICE_AI `.bin` (encoder/flow/decoder/bert): `qai_appbuilder.QNNContext` only, NEVER `qnn-net-run`/`snpe-net-run` | `### Issue 13: VOICE_AI custom pipeline` |
| 14 | ONNX ZIP has external `.data` weights — keep `.onnx` + `.data` in same dir | `### Issue 14: ONNX external weights` |
| 15 | QNN DLC image models use NHWC `[1,H,W,C]`; ONNX uses NCHW `[1,C,H,W]` — check `metadata.json` shape first | `### Issue 15: NHWC vs NCHW` |
| 16 | ASR/Transducer trio (encoder/decoder/joiner): must be from same AI Hub package (dim mismatch e.g. `512 vs 320`); debug encoder→decoder→joiner; check encoder-out L2 norm (~<20) | `### Issue 16: Multi-sub-model ASR trio` |
| 17 | Synthetic audio (silence/sine) → ASR empty result — verify input energy `>= 1e-3` before inference | `### Issue 17: Invalid test input` |
| 18 | 🚨 **NEVER full-disk recursive scan** for model packages (hangs 30+min). Use fixed shallow paths: `C:\WoS_AI\<model>\`, `C:\Users\<user>\.qaihub\` | `### Issue 18: No full-disk recursive scan` |
| 19 | 🚨 SSL failure on WoS (`curl exit 35` / `SSLError`) — always use `curl -k`; Python urllib needs `ssl.CERT_NONE`. S3 also 403s on HEAD/Range — use plain GET | `### Issue 19: SSL / S3 HEAD 403` |
| 20 | `os._exit()` → `0xC0000409` process crash — let script exit normally, or `del model` first then `os._exit(0)` | `### Issue 20: os._exit crash` |

> ⚠️ 🚨-marked rows carry irreversible traps (silent hangs, crashes, wrong numerics, whole-disk hangs). If any symptom hits, open the referenced section IMMEDIATELY.

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
