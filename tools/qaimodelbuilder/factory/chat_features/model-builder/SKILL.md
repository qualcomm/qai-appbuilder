---
name: Model Builder
description: QAI ModelBuilder. Tools and workflows for model conversion, inspection, operator patching, quantization, and inference validation of self-converted models on Qualcomm platform. Use this skill when working with custom ONNX/PyTorch models: export to ONNX, convert to QNN/SNPE DLC, FP16/FP32/INT8 quantization, operator patching, context binary generation, and inference validation of self-built models. NOT for AI Hub prebuilt packages — use model-hub skill instead. Supports QAIRT SDK 2.45+ on Windows on Snapdragon (WoS) ARM64 devices.
---

# QAI ModelBuilder

> **How to use this SKILL (it is a thin dispatch layer):**
> 1. Pass the **Boundary Decision** gate below first — it decides whether this skill even applies.
> 2. Use the **Routing Table** to load the ONE reference / sub-SKILL that matches your step or problem — do NOT read everything up front.
> 3. Follow the **Core Workflow** spine; open `references/core_workflow.md` for per-step commands.
> 4. The **Blocking Conditions** and **Disciplines** below are the only rules you must hold in mind the whole time.
> 5. Trust the docs: never run commands to re-verify facts already in this file or `${APP_ROOT}\data\config\qairt_env.json` (torch/Python versions, tool paths). Don't read script source unless a reference doc is missing the detail (then update that doc).

---

## 🚨 Boundary Decision — pass BEFORE activating (MANDATORY GATE)

Answer three questions; **if any veto holds, stop and switch skills.**

| # | Question | YES -> | NO -> |
|---|------|-------|------|
| Q1 | Does the model **already have a prebuilt package on AI Hub** (Zipformer, MobileNet, YOLO, …)? | ❌ **Stop -> `model-hub` skill** | Q2 |
| Q2 | Is the file to download/use an AI Hub prebuilt artifact (QNN context binary `.bin` / `.dlc`)? | ❌ **Stop -> `model-hub` skill** | Q3 |
| Q3 | Does the user have a **custom ONNX/PyTorch model** to convert, or to re-quantize/recompile into a custom `.bin`? | ✅ **Activate this skill** | ❓ Intent unclear — confirm first |

**Switch to `model-hub` on any of:** "download from AI Hub" / "model on AI Hub" / "prebuilt package" / "on-device pre-exported package"; a model name + "download" where it already exists on AI Hub; "QNN_CONTEXT_BINARY" / "QNN_DLC". An AI Hub prebuilt package (`.bin`/`.dlc`) is loaded directly by `model-hub` via `qai_appbuilder`/`QNNContext`; this skill only converts/compiles **custom ONNX/PyTorch**. ("run inference" alone does NOT pick this skill — only when the target is a self-converted model.)

> **Dispatching a sub-agent to another skill (e.g. `model-hub`) — MANDATORY:** a sub-agent inherits NOTHING from this skill (blank context). Once a vacuum-written prompt sent one recursively scanning `C:\Shared`/`C:\WoS_AI` for `.bin` -> 30-min hang. So: (1) **first `read` the target SKILL.md in full**, write the prompt from it; (2) the prompt's first instruction MUST be "read the target SKILL.md in full before acting"; (3) the prompt MUST NOT carry any path/script/toolchain from this skill (`run_pipeline.py`, `qnn-onnx-converter`, …) — useless for prebuilt packages and lures a wrong disk scan; (4) reuse the target's Sub-Agent Dispatch Template if it has one.

> **⚠️ First action after activating (before Step 1):** ask the **DLC portability question** (cross-platform vs SoC-optimised DLC) unless the user's request already signals it. Full wording + CLI mapping + skip-signals -> `references/operations_reference.md § DLC Portability Question`.

---

## 🧭 Routing Table — load only the ONE that matches (MANDATORY)

This SKILL is the main pipeline + index. For any specific step, error, or topic, load the single matching doc below — do NOT read them all. Troubleshooting **sub-SKILLs** are the canonical authority for errors (more complete than any inline summary). `base` ships in the external edition; `advanced` is internal-only (silently skip if the file is absent).

**By problem / error (troubleshooting sub-SKILLs — canonical):**

| Symptom / error code | Load | tier |
|---|---|---|
| `unsupported operator` / `0xc26` / Einsum / Mod / Floor / ScatterND / dry-run false positive | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/operator-patching/SKILL.md` | base |
| `Graph Compose failure` / `graph_names` / `Wrong number of Parameters 5` / `loadRemoteSymbols 4000` / arch mismatch | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/conversion-troubleshooting/SKILL.md` | base |
| QNNContext crash / stale artifact / multi-model same-process / Linux HTP transport mismatch / NCHW-NHWC wrong | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/inference-troubleshooting/SKILL.md` | base |
| VCTargetsPath / CMake / `import cv2`·Pillow / `qai_appbuilder` import fails | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/env-troubleshooting/SKILL.md` | base |
| 0-byte generator / `WinError 193` / need to modify an SDK file | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/sdk-integrity-recovery/SKILL.md` | base |
| basicsr / functional_tensor / aux-branch ReshapeOp (ONNX export) | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/export-troubleshooting/SKILL.md` | base |

**By topic (references):**

| Topic | Load |
|---|---|
| **Core Workflow step details** (commands + caveats) | `references/core_workflow.md` |
| **Operations detail** (flow selection, DLC-portability Q, guardrails, working-dir, project config, script index, pack export) | `references/operations_reference.md` |
| Environment setup (Windows) | `references/win_qairt_setup.md` |
| Export + ONNX validation | `references/model_export_validation.md` |
| Operator patching (full code library) | `references/operator_patching.md` |
| QNN conversion | `references/qnn_conversion.md` |
| SNPE conversion | `references/snpe_conversion.md` |
| Quantization (+ tool-param map) | `references/model_quantization.md` |
| Context binary | `references/context_binary.md` |
| Inference (NCHW/NHWC, API, templates) | `references/inference.md` |
| Quantization sensitivity (pre-conversion risk pre-flight) | `references/quantization-sensitivity.md` |
| Verification discipline | `references/verification-discipline.md` |
| Pack export & `inference_manifest.json` | `references/pack_export.md` |
| ADB device deployment | `references/adb_execution.md` |
| Remote (SSH) execution | `references/remote_execution.md` |
| Troubleshooting quick-index + Windows tips | `references/troubleshooting.md` |

---

## Core Workflow (8-step spine)

> Per-step commands, caveats, and MANDATORY sub-requirements -> [`references/core_workflow.md`](references/core_workflow.md) — open it when you start executing.
> **First (once, before Step 1):** run Host OS Detection, write `HOST_OS` to `plan.md` (`windows-arm64`/`linux-aarch64`/`linux-x64`; drives Step 7 path) -> `core_workflow.md § Host OS Detection`.

1. **Export to ONNX** — `python_x64_venv`, `model.eval()`, **FP32 only** (never FP16), `opset_version=18`; disable training-only branches (`aux_logits`/dropout). -> `core_workflow.md § Step 1` / `model_export_validation.md`.
2. **Inspect ONNX I/O** — `qai_inspect_onnxio.py`. ⚠️ **Do NOT gate on `--dry_run`** (false positives) — go straight to Step 4. -> `core_workflow.md § Step 2`.
3. **Operator patching** — ONLY if actual conversion hits a hard op error. Patch in-memory, re-validate (checker -> real conversion -> cosine ≥ 0.95). Canonical -> `operator-patching` sub-SKILL.
4. **Convert float model** — **`run_pipeline.py` (Flow A, default)**; `run_pipeline_legacy.py` only for explicit DLL/Flow C (no VS ARM64 env for Flow A). `--precision fp16|fp32`, `--htp_version v73|v81` (default v73). -> `core_workflow.md § Step 4` / `qnn_conversion.md`. Flow comparison -> `operations_reference.md § Flow Selection`.
5. **Quantization (optional)** — `run_pipeline.py --precision <p> --calib_list <list>`. ⚠️ Real multi-class calibration data; ask user if none. -> `core_workflow.md § Step 5` / `model_quantization.md`.
6. **Context binary** — `run_pipeline.py` emits `.bin` automatically. `.bin` (same machine, fastest) vs `.dlc` (cross-device / quick validation). -> `core_workflow.md § Step 6` / `context_binary.md`.
7. **Inference + validation** — route by `HOST_OS`: Path A (`qai_runner.py`/`qai_appbuilder`, `python_arm64_venv`) or Path B (`adb_runner.py`). **NEVER call `qnn-net-run` directly.** MANDATORY: save `infer_{MODEL}.py` + `inference_manifest.json` to workspace. -> `core_workflow.md § Step 7` / `inference.md`.
8. **Validation report (MANDATORY)** — ONNX (CPU-only) vs QNN cosine (≥0.99 FP16/FP32, ≥0.95 INT); below threshold -> **B6** (stop, don't auto-fix). Write `REPORT.md` with the exact "Cosine Similarity Summary" plain-text format. Print `${WORKSPACE}\<model_name>` in **every** turn's final summary. Update `plan.md`. -> `core_workflow.md § Step 8`.

> Artifact checklist per Flow (A/B/C) -> `references/expected_output_artifacts.md`. Batch runs -> `scripts/model_config.json`.

---

## 🛑 Blocking Conditions (always STOP & ask — both modes)

| # | Condition -> Action |
|---|---|
| B1 | Required config var empty/placeholder -> stop, list missing, ask user. |
| B2 | `pip install` needed -> stop, state package + reason, ask permission. |
| B3 | Patch iterations exhausted, NO progress (same ops, no patterns left) -> stop, list attempts + logs, escalate. |
| B4 | Operator patch would change model semantics -> stop, describe change, ask approval. |
| B5 | Target device unavailable for context-bin gen / on-device test (incl. remote unreachable) -> stop, ask how to proceed. |
| B6 | Accuracy < threshold after quant (cosine < 0.95) -> **do NOT auto-fix.** ① zero-cost diagnosis (is calibration one image / its augmentations? not diverse). ② STOP, report cosine + diagnosis, present options (each 1-line principle), ask which: (1) improve calib diversity; (2) `--cle` (+`--per_channel`); (3) `--precision w8a16`; (4) keep FP16 / try `bf16`; (5) accept if Top-K correct. Full flow -> `model_quantization.md` / quant-accuracy sub-SKILL. |
| B7 | No known replacement pattern for an unsupported operator -> stop, document, escalate. |
| B8 | Context binary gen fails on Windows ARM -> **stop** (`run_pipeline.py` exits non-zero; NOT silently degraded). Return to operator patching; do NOT retry alternate generators (x86_64 build can't load an ARM64 DLL). 0-byte/corrupt generator = damaged SDK file -> `qai_dev_gen_contextbin.py` self-heals from the kept SDK zip; if none -> `sdk-integrity-recovery` sub-SKILL. Diagnose READ-ONLY. |
| B9 | Fixing would require modifying any file under `$QAIRT_SDK_ROOT`/`$QNN_SDK_ROOT` -> **STOP IMMEDIATELY.** Never edit/copy-over/rename/delete an SDK file (the `C:\Qualcomm` tree is tool-layer write-protected). Copy the file into the workspace and edit the *copy*, pointing tooling at it via documented overrides (`--config_file`, `QNN_*` env, workspace-local `backend_extensions.json`). Reading the SDK dir is fine. Genuinely missing/corrupt -> recover from kept zip (`sdk-integrity-recovery`); ask explicitly *"edit `<sdk_path>/<file>`? [y/N]"* and act only on a scoped yes naming the file. |
| B10 | A tool/script/package not described here must run and the venv is unclear -> **stop, ask.** Don't default to `python_arm64_venv` just because the goal is inference — env depends on what the tool links (`python310.dll` -> `python_x64_venv`). |

---

## Disciplines (hold these the whole run)

- **Execution mode** (`MODE` in config, default `batch`): batch = run all phases autonomously, apply safe defaults, log decisions, only stop on a Blocking Condition — do NOT ask "proceed to next phase?" / "which precision?" (use config) / "run onnxsim?" (always). `interactive` = confirm at each phase. **Never silently fall back to ONNX/CPU when QNN/HTP fails** — diagnose & fix, or stop & report; substituting CPU for a failed HTP run is never an acceptable fix.
- **Inference results MUST come from actual execution.** Never output Top-K/confidence/latency/cosine without first running the script via `exec`; every number traces to an `exec` log line. No guessing/estimating from model knowledge; no writing the report before running.
- **Operator patching is exhaustive** — patch ALL unsupported ops until no pattern remains; never fall back to CPU; no fixed iteration cap; escalate only on B7/B4/B3. Rules + code -> `references/operator_patching.md`.
- **Working directory:** all model artifacts under `${WORKSPACE}\<model_name>\` — NEVER under a `QAIModelBuilder` path, home/Downloads, or a CWD outside `${WORKSPACE}`. Self-check every write. Bootstrap with `qai_workspace_init.py`. Tables + init diagnosis -> `references/operations_reference.md § Working Directory`.
- **Wrappers only:** conversion via `run_pipeline.py`/`run_pipeline_legacy.py` (never call `qairt-converter`/`qnn-onnx-converter`/`qnn-model-lib-generator` directly); inference via `qai_runner.py`/`qai_appbuilder` (never `qnn-net-run`). Wrappers handle `--preserve_io`, layout, PYTHONPATH, arch dirs.
- **Trust known facts; never re-verify via shell** (torch=2.x, Python x64=3.10/ARM64=3.13, tool paths — all in `qairt_env.json`). Timeouts: `timeout=0` for all conversion commands. Benign HTP errors, `os._exit` crash, encoding, escalation, SDK read-only rules -> `references/operations_reference.md § Guardrails`.

---

## ⚠️ Python Environments (not interchangeable — gates B10)

Paths from `${APP_ROOT}\data\config\qairt_env.json` (`Setup.bat` generates it). **Never hardcode.**

| Env | Key | Python | Role |
|-----|-----|--------|------|
| Conversion | `python_x64_venv` | x86_64 3.10 | ONNX export, `qnn-onnx-converter`, `qnn-model-lib-generator` |
| Inference | `python_arm64_venv` | ARM64 3.13 | `qai_appbuilder`, `QNNContext` |
| Ubuntu | `python3_venv` | x86_64 3.12 | All Ubuntu ops (no ARM64 venv on Ubuntu) |

**Default for tools not listed here:** default to `python_x64_venv` (most QAIRT tools link `python310.dll`, fail under ARM64 3.13); use `python_arm64_venv` only when the tool imports `qai_appbuilder`/`QNNContext` or runs inference on a prebuilt `.bin`/`.dlc`; still unsure -> **B10**. On Ubuntu (`linux-x64`) local inference is unavailable -> Step 7 Path B (`adb_runner.py`). Setup / pip / `--index-url` / opencv / PYTHONPATH -> `references/win_qairt_setup.md`; env broken -> `env-troubleshooting` sub-SKILL.
