---
name: model-hub
description: Model Hub — download pre-exported models from Qualcomm AI Hub, run inference on-device, and export them to App Builder as ready-to-import Packs. Use this skill when the user wants to use an AI Hub prebuilt package (`.bin` / `.dlc` / ONNX) without doing custom conversion. NOT for self-converted models — use model-builder skill instead.
tags: aihub, model-hub, inference, qualcomm, on-device, qnn, tts, asr, classification, export, app-builder
use_for: Download pre-exported models from AI Hub, run inference on Snapdragon X Elite / X2 Elite without conversion, then always normalize the download into the App Builder workspace contract (Step 6.5, mandatory) so it is importable; optionally pre-build the full app_pack and promote it (Phase 7).
homepage: "https://aihub.qualcomm.com/compute/models"
---

# Model Hub

> **How to use this SKILL:** ① Pass the **Pre-Flight Self-Check** below (MANDATORY gate). ② **x64 host + inference request?** → read `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` FIRST. ③ Follow the 8-step workflow; open the referenced `references/*.md` when you reach a step that needs it. ④ Hold the **Disciplines** the whole run.

---

## ✋ Pre-Flight Self-Check (MANDATORY GATE)

| # | Must answer | How |
|---|------|--------|
| 1 | Detected the chipset suffix? | Step 0 (`Get-ChildItem HKLM:\...Services` for `qcadsp*`, **NOT** `Get-PnpDeviceProperty`). The result is a **routing input**, not a stop signal — x64 / no-qcadsp is a legitimate host, decide feasibility only after step 2. |
| 2 | Read this model's `NOTES.md`? | `read factory/chat_features/model-hub/models/<model_id>/NOTES.md`; exists → **must read BEFORE declaring feasibility** (Format field tells you whether the package is `.bin`-only or has a `.dlc` variant; has download links / I/O / pitfalls / ready script, skip most Steps 1–5); absent → create at task end. |
| 3 | Inference tool = `qai_appbuilder`? | NPU always via `QNNContext` loading `.bin`/`.dlc`; `onnxruntime` only for CPU baseline |
| 4 | Fixed paths only? | Download to `C:/WoS_AI/<model>/`; **NEVER** recursive scan on `C:\`/`C:\WoS_AI` unbounded (hangs 30+ min, Issue 18) |
| 5 | Will you normalize (Step 6.5)? | **MANDATORY** after inference — `aihub_to_manifest.py` produces the App Builder layout. Skipping = "on disk but invisible" |

> **x64 host + inference request?** → read `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` FIRST (compatibility matrix, backend choice via `question` tool, B11, closing statement). ARM64 hosts and x64-via-ADB requests follow the main workflow.

> NOTES already collected: `beit` `melotts_zh` `resnet50` `zipformer` (see `models/` directory).

---

## 🤖 Sub-Agent Dispatch

When dispatching a sub-agent to this skill → **read the full template first:**
[`references/dispatch-template.md`](references/dispatch-template.md)

Key rules (summary): ① main agent reads SKILL.md first; ② sub-agent prompt's first instruction = "read the full SKILL.md"; ③ NEVER include model-builder paths; ④ NEVER phrase as "search C:\ for .bin".

---

## Decision Prerequisites

**Default to this skill**: download AI Hub pre-compiled package + run inference with `qai_appbuilder` (`QNNContext`). On ARM64 hosts use `python_arm64_venv`; on x64 hosts use `python_runtime_venv` (resolves to `.venv_x64_313`). No VS 2022 / QAIRT SDK conversion toolchain needed.

**Switch to `model-builder`**: ① custom ONNX/PyTorch model with no AI Hub package; ② need to re-quantize/compile a custom NPU `.bin`.

---

## Trigger Phrases

- "download model from aihub and run inference"
- "run \<ModelName\> on device" / "infer \<ModelName\> for me"

---

## Environment

> `${APP_ROOT}` = this repo (QAIModelBuilder) root. Never hardcode machine-specific absolute paths.

Read from `${APP_ROOT}\data\config\qairt_env.json`:

| Key | Role |
|-----|------|
| `python_arm64_venv` | ARM64 Python 3.13 — inference on ARM64 hosts (`windows-arm64` / `linux-aarch64`) |
| `python_runtime_venv` | x86_64 Python 3.13 (`.venv_x64_313`) — inference on x64 hosts (`windows-x64` / `linux-x64`) |
| `qairt_sdk_root` | QAIRT SDK root (reference only) |

Working directory: `C:/WoS_AI/<model_name>/`

> ⚠️ `C:/WoS_AI` is only the **default**; the user may set a custom working directory. The authoritative path is the system prompt's `## Working Directory` block (the file/command tools already resolve relative paths there). If `C:/WoS_AI` does not exist, the user has configured a different working directory — use that one, do NOT create or hardcode `C:/WoS_AI`.

> ⚠️ `read`/`skill` display expands `${APP_ROOT}` to absolute paths, but on-disk still contains `${APP_ROOT}` — don't copy expanded paths into `edit`'s `oldText`.

---

## Step 0 — Detect Platform (MANDATORY)

```powershell
Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
  Where-Object { $_.PSChildName -like "qcadsp*" } |
  Get-ItemProperty | Select-Object PSChildName, ImagePath
```

| Driver suffix | Chipset | Download suffix |
|---------------|---------|----------------|
| `_8380` | X Elite | `qualcomm_snapdragon_x_elite` |
| `_8480` | X2 Elite | `qualcomm_snapdragon_x2_elite` |
| `_8380` (8-core) | X Plus 8-Core | `qualcomm_snapdragon_x_plus_8_core` |

> ⚠️ Do NOT use `Get-PnpDeviceProperty` (blocks 300–400 s) or `Get-WmiObject Win32_SystemDriver` (hangs on busy systems).

---

## Step 0.5 — Check Model Notes (MANDATORY)

> Directory name may differ from model_id (e.g. `inceptionv3` → `inception_v3`). **Always two-step:**

```
1. list "factory/chat_features/model-hub/models/"   → exact directory names
2. read  "factory/chat_features/model-hub/models/<exact_name>/NOTES.md"
```

If NOTES.md exists → use its download links / I/O / script directly (skip Steps 1–4).
If absent → continue; create NOTES.md at task end.

---

## Step 1 — Look Up the Model

Three methods (preference order): A. Parse AI Hub HTML; B. webfetch HuggingFace; C. Construct S3 URL from GitHub.
**Complete code for all three** → [`references/workflow-details.md § Step 1`](references/workflow-details.md).

> ⚠️ Do NOT use `qai_hub.get_models(name=...)` (raises `unexpected keyword argument`).

---

## Step 2 — Choose Format

| Priority | Format | Tool | When |
|----------|--------|------|------|
| **1st** ✅ | `QNN_CONTEXT_BINARY` | `QNNContext` (`.bin`) | Default — chipset-matched context binary |
| **2nd** ✅ | `QNN_DLC` | `QNNContext` (`.dlc`) | No `.bin` available; portable across HTP versions |
| 3rd | `ONNX` | `onnxruntime` CPU | **Only** for CPU baseline comparison |

> 🚨 NPU inference **always** via `qai_appbuilder.QNNContext`; `onnxruntime` is **never** for NPU.

---

## Step 3 — Download & Extract

```python
exec('curl -k -L "<url>" -o "C:/WoS_AI/<model>/<file>.zip" --create-dirs', timeout=300)
import zipfile
zipfile.ZipFile("C:/WoS_AI/<model>/<file>.zip").extractall("C:/WoS_AI/<model>/")
```

> Always `curl -k` (Issue 19: SSL on WoS). Always `zipfile`, NEVER `tar`.

---

## Step 4 — Read metadata.json (MANDATORY)

Read before writing inference code. Contains: input/output tensor names, shapes, dtypes; quantization params (scale + zero_point); input layout (NHWC vs NCHW).

> ⚠️ QNN reports quantized uint16 as `ufp16` — don't rely on `model.getInputDataType()` alone (Issue 5).

---

## Step 5 — Run Inference

Templates (`infer_classify.py` / `infer_detect.py` / `infer_segment.py` / `infer_sr.py` / `infer_generic.py`) at `${APP_ROOT}/factory/chat_features/model-builder/scripts/inference/`.
**Run command, test image priority, preprocessing** → [`references/workflow-details.md § Step 5`](references/workflow-details.md).

> ⚠️ Check `metadata.json` `value_range` before preprocessing (`[0,1]`=/255; `[-1,1]`=/127.5-1; `[0,255]`=cast). ONNX → transpose NHWC→NCHW (Issue 15).

### HTP BURST — 3 rules (hold for the whole run)

1. BURST only works **after** at least one QNNContext is loaded (else silent no-op / `0x32c9`).
2. Set **once** at session start, hold across **all** inferences, release **once** at session end. NEVER per-call Set/Release (causes clock ramp jitter).
3. Release **before** destroying contexts (`RelPerfProfileGlobal()` while models still loaded).

Full lifecycle pattern + anti-pattern code → [`references/workflow-details.md § HTP BURST`](references/workflow-details.md).

### ONNX (CPU) + QNN DLC (NPU) same process

Standard `onnxruntime` CPUExecutionProvider does NOT conflict with `qai_appbuilder`. Pattern → `references/workflow-details.md § ONNX (CPU) + QNN DLC (NPU)`.

QNN inference routing (per-platform defaults + override keywords) → `${APP_ROOT}/factory/chat_features/_shared/qnn-inference-routing.md`. **x64 host + local inference?** → also read `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md`.

---

## Step 6 — Interpret Results

On first `.dlc`/`.bin` load, WARNINGs (`warmup_parallel_stl` / `input_data_type: float`) are normal; cold start ~5–60 s.

**Non-fatal HTP logs (ignore — results are correct, do NOT fall back to CPU):** `setPowerConfig error 0x32c9`, `Error 0x200: failed to close queue`, `m_CFBCallbackInfoObj is not initialized`, `Failed to create context with file mapping`.

---

## Step 6.5 — Normalize to App Builder contract (MANDATORY)

> 🚨 NOT optional, NOT gated on "the user asked to export". Run immediately after inference passes.

> **Root cause:** App Builder's readiness scan (`ImportScanBinsUseCase._scan_workspace`, `deferred_routes.py:1022`) ONLY looks under `<workdir>/output/` for files named `<workdir-name>_<label>.{bin,dlc}` ≥ 1 MiB. A fresh AI Hub download extracts into a nested subfolder with no `output/` directory — the scan returns empty → no readiness dot. The mapper normalizes this into the model-builder-identical layout.

> `<python_arm64_venv>` below is a placeholder: on ARM64 hosts read it from `python_arm64_venv` in `${APP_ROOT}\data\config\qairt_env.json`; on x64 hosts read it from `python_runtime_venv` in the same file.

```powershell
& "<python_arm64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\factory\chat_features\model-hub\scripts\aihub_to_manifest.py" `
  --workdir "C:\WoS_AI\<model>" `
  --model-name <model> `
  --precision <w8a8|float|w8a16|...> `
  --output-type <classification|detection|super_resolution|segmentation|text|audio|raw> `
  --vendor "<original author>"
```

> ⚠️ `--workdir` = top-level `C:\WoS_AI\<model>\` (NOT nested subfolder); `--model-name` = that folder name.

**Verify:** `C:\WoS_AI\<model>\output\<model>_<label>.{bin,dlc}` exists (≥ 1 MiB) + `inference_manifest.json` exists.

> 🚨 **Every-turn summary MUST print `C:\WoS_AI\<model>`** — App Builder's promote-ready detection extracts this path from your final summary each turn.

---

## Phase 7 — Export app_pack + Promote (optional)

Step 6.5 already made the model detectable/importable. Phase 7 is optional — do it when user wants `app_pack/` pre-built.

**Full commands** (Step 7.2 `qai_pack_export.py`, Step 7.3 `qai_pack_validate.py`, multi-sub-model note) → [`references/workflow-details.md § Phase 7`](references/workflow-details.md).

---

## ⚠️ Known Issues — Quick Index

All 20 issues with full symptom / cause / fix / code → [`references/known-issues.md`](references/known-issues.md).
Read the relevant `### Issue N:` section when your run hits one.

| Category | Issues | Key traps |
|----------|--------|-----------|
| **Crashes / hangs** | 9, 12, 18, 20 | `QNNConfig.Config()` before load; input order = `getInputName()` order; NEVER full-disk scan; `del model` before `os._exit` |
| **Wrong numerics** | 5, 7, 15 | ufp16 dtype trap (manual dequant); trust runtime shapes; NHWC vs NCHW per format |
| **Download / SSL** | 1, 3, 19 | zipfile not tar; timeout=300; `curl -k` always |
| **ARM64 / env** | 2, 4, 6 | PortableGit shell; `sys.stdout.reconfigure(encoding='utf-8')`; pre-import mocks for missing native pkgs |
| **Multi-model** | 13, 16, 17 | multi-`.bin` package; ASR trio same package; verify input energy ≥ 1e-3 |
| **Warnings (safe)** | 8, 10, 11, 14 | qai_appbuilder only for NPU; separate processes for baseline; file-mapping warning; external .data weights |

> 🚨-marked issues (5, 6, 9, 12, 18, 19) carry irreversible traps. Open `known-issues.md` immediately on match.

---

## Quick Reference

```
0.   detect   Get-ChildItem HKLM:\...\Services | Where qcadsp* → SoC code
0.5  NOTES    read models/<model_id>/NOTES.md  ← MANDATORY FIRST (skip Steps 1–4 if exists)
1.   lookup   AI Hub HTML / HuggingFace / S3 construct → download URL  (→ workflow-details.md)
2.   format   QNN_CONTEXT_BINARY > QNN_DLC > ONNX(CPU only)
3.   download curl -k -L → C:/WoS_AI/<model>/  ; zipfile.extractall  (NEVER tar, Issue 19: -k)
4.   metadata read metadata.json → shapes, dtypes, quant params
5.   infer    <python_arm64_venv on ARM64 | python_runtime_venv on x64> infer_*.py  (→ workflow-details.md)
6.   results  ignore non-fatal HTP warnings; cold start normal
6.5  NORMALIZE (MANDATORY) aihub_to_manifest.py → output/<model>_<label>.{bin,dlc} + manifest
7.   report   print C:\WoS_AI\<model> in summary; add NOTES.md for new findings
8.   export   (optional) qai_pack_export.py → app_pack/ → Promote  (→ workflow-details.md)
```
