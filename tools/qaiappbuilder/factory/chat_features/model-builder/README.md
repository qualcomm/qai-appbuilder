# Model Builder Skill (`model-builder`)

The agent skill behind QAI ModelBuilder's **Model Builder** chat mode: convert a
**custom** ONNX / PyTorch model to QNN or SNPE, quantize it, generate a context
binary, and validate it against the ONNX baseline on Qualcomm hardware.

`SKILL.md` in this directory is the authoritative entry point — it is a thin
dispatch layer that routes the agent to exactly one reference or troubleshooting
sub-SKILL per step. **This README is orientation only. When the two disagree,
`SKILL.md` wins.**

```
⚠️ Disclaimer: This is an experimental feature and still requires further improvements. Code generated with this
skill is the starting point of code development. All generated code must complete code review, testing, security
validation, and other required software release processes before being released.
```

---

## Scope — check this before you use it

`SKILL.md` opens with a mandatory boundary gate. It exists because a prebuilt AI
Hub package needs no conversion at all, and running one through this skill wastes
a long pipeline on a problem that does not exist.

| Your model | Use |
|---|---|
| Custom ONNX / PyTorch you export yourself | **this skill** |
| Custom `.bin` / `.dlc` you want to re-quantize or recompile | **this skill** |
| Already has a prebuilt package on AI Hub (Zipformer, MobileNet, YOLO, …) | `model-hub` |
| An AI Hub prebuilt artifact you just want to download and run | `model-hub` |
| Wrong results, cosine drop after quantization, or you want it faster | `model-opt` |

Same model name, different job: exporting **your own** YOLOv8 from PyTorch and
converting it is this skill; downloading the **AI Hub** YOLO package is
`model-hub`. Phrasings like "download from AI Hub" / "prebuilt package" /
"QNN_CONTEXT_BINARY" always mean `model-hub`.

---

## How it activates

This skill is **not** a `.claude/skills/`-style drop-in that you copy into an
agent's skills folder. It ships with the product at
`${APP_ROOT}/factory/chat_features/model-builder/`, and the host decides when to
load it:

- **Explicit** — pick **Model Builder** (`模型构建器`) in the composer's mode
  selector. The full `SKILL.md` is injected into the system prompt.
- **Auto-detected** — a message that matches the model-build patterns (`qnn`,
  `snpe`, `qairt`, `dlc`, `onnx`, `context binary`; `fp16`/`int8`/`w8a16`/
  `quantize`/`calibrate`; Chinese `转换`/`量化`/`导出`/`部署`/`编译` next to
  `模型`; or a well-known model name plus a conversion verb) flips the toolbar to
  Model Builder and lets the agent read `SKILL.md` on demand. An AI-Hub-flavoured
  message is routed to `model-hub` instead, even if a build verb also matches.

`skill.policy.json` declares the paths the skill may read (QAIRT SDK, Visual
Studio, CMake, this directory) and write (`${PROJECT_ROOT}/logs`, `${TEMP}`), plus
the binaries it may execute. `taxonomy_rules.yaml` feeds the model-taxonomy
classifier that maps a model name to a task family.

### Sanity check

In Model Builder mode, ask the agent to summarize the 8-step Core Workflow, or
give it a real request such as *"convert my inception_v3.onnx for this
device"*. A correctly loaded skill will first detect the host OS and ask you the
DLC-portability question before touching the converter.

---

## Prerequisites — `Setup.bat` does all of it

Do **not** set `QNN_SDK_ROOT` by hand, and do not `pip install` into the venvs.
`Setup.bat` installs the QAIRT SDK, creates the venvs, and writes every path into
`${APP_ROOT}/data/config/qairt_env.json`; the scripts read it automatically and
the skill is forbidden from hardcoding paths. If something is missing, re-run
`Setup.bat` — see `references/win_qairt_setup.md`, or the `env-troubleshooting`
sub-SKILL when it is already broken.

Two Python environments, **not interchangeable** (getting this wrong is the single
most common failure):

| Env | Key in `qairt_env.json` | Python | Used for |
|---|---|---|---|
| Conversion | `python_x64_venv` | x86_64 3.10 | ONNX export, `qairt-converter`, `qairt-quantizer`, `qnn-*` tools — on every host |
| Runtime | `python_runtime_venv` | 3.13 (aarch64 on WoS, x86_64 on x64) | `qai_appbuilder` / `QNNContext` inference |
| Ubuntu | `python3_venv` | x86_64 3.12 | all Ubuntu operations |

An agent that is unsure which venv a tool needs must stop and ask (blocking
condition **B10**), and it must ask before installing any package (**B2**).

Upstream, for background only — not installation steps:
[QAIRT SDK](https://quic.github.io/cloud-ai-sdk-pages/latest/qnn-aic/general/QAIRT-SDK-Installation/index.html),
[qai_appbuilder](https://github.com/qualcomm/qai-appbuilder/releases).

---

## Directory layout

| Path | What it is |
|---|---|
| `SKILL.md` | Authoritative entry point: boundary gate, routing table, 8-step workflow, blocking conditions, disciplines |
| `references/` (17 files) | One doc per topic — `core_workflow.md` for per-step commands, `operations_reference.md` for flows/working-dir/config/script index, plus per-topic conversion, quantization, inference, ADB, remote, and pack-export docs |
| `troubleshooting/` (6 sub-SKILLs) | Canonical authority for errors: `operator-patching`, `conversion-`, `inference-`, `env-`, `export-troubleshooting`, `sdk-integrity-recovery`, plus `_diagnosis-framework.md` |
| `scripts/` | The wrappers the workflow drives — `qai_workspace_init.py`, `qai_inspect_onnxio.py`, `run_pipeline.py`, `run_pipeline_legacy.py`, `qai_convert_{fp,int,snpe}.py`, `qai_dev_gen_contextbin.py`, `qai_runner.py`, `adb_runner.py`, `qai_pack_{export,validate}.py`, `onnxwrapper.py` |
| `scripts/inference/` | Per-task inference templates (`infer_generic`/`classify`/`detect`/`segment`/`sr`) — see its own README |
| `assets/plan.md` | Template copied into a new workspace as `plan.md` (agent-facing progress + config notes) |
| `skill.policy.json` | Declared read/write paths and trusted binaries |
| `taxonomy_rules.yaml` | Keyword → task rules for the model-taxonomy classifier |

Cross-skill knowledge lives in `${APP_ROOT}/factory/chat_features/_shared/`
(`qnn-inference-routing.md`, `x64-host-notes.md`) and is referenced, never copied.

---

## The workflow at a glance

Details and exact commands are in `references/core_workflow.md`; do not treat this
table as a substitute for it.

Before step 1 the agent detects the host OS (`windows-arm64` / `windows-x64` /
`linux-aarch64` / `linux-x64`), records it in `plan.md`, and asks the
**DLC-portability question** — cross-platform DLC or SoC-optimised.

| # | Step | Driven by |
|---|---|---|
| 1 | Export to ONNX — FP32 only, `opset_version=18`, `model.eval()` | `python_x64_venv` |
| 2 | Inspect ONNX I/O (`--dry_run` is advisory; never gate on it) | `qai_inspect_onnxio.py` |
| 3 | Operator patching — only when a real conversion hits a hard op error | `operator-patching` sub-SKILL |
| 4 | Convert float model | `run_pipeline.py` (Flow A, default on all hosts) |
| 5 | Quantization (optional) — needs real, diverse calibration data | `run_pipeline.py --precision … --calib_list …` |
| 6 | Context binary — QNN emits `.bin` automatically | `run_pipeline.py` |
| 7 | Inference + validation — local HTP on ARM64 hosts, ADB by default on x64 | `qai_runner.py` / `adb_runner.py` |
| 8 | Validation report — cosine vs the ONNX baseline, write `REPORT.md` | mandatory |

`run_pipeline_legacy.py` (and `qai_convert_fp/int.py`) are the legacy DLL flow —
`windows-arm64` only, and they error out elsewhere. Never call `qnn-net-run`
directly; the wrappers handle `--preserve_io`, layout, `PYTHONPATH`, and arch
directories.

**QNN vs SNPE artifacts:** the QNN deliverable is a context binary **`.bin`**; the
SNPE / QAIRT deliverable is a **`.dlc`**. Flow A produces the `.dlc` as an
intermediate on the way to the `.bin`. Use `.dlc` when you want the backend choice
to stay open or need cross-backend portability.

### Where the artifacts go

Everything lands under `${WORKSPACE}\<model_name>\`, bootstrapped by
`qai_workspace_init.py` (it renames an existing directory to
`<model_name>_bak_<ts>`, creates `output/calib/`, and copies `assets/plan.md` in as
`plan.md`). `${WORKSPACE}` is the session working directory shown in the agent's
prompt, never a fixed location — the documented default is only a fallback, and
the agent must not create it when a custom working directory is configured
(`references/operations_reference.md § Working Directory`).

Writing anywhere else is forbidden: no paths containing `QAIModelBuilder`, no home
or Downloads directory, no working directory outside `${WORKSPACE}\`. The skill
does **not** build a project "in place" next to your source tree.

---

## Rules you will notice as a user

- **It stops and asks** rather than guessing. Eleven blocking conditions
  (`SKILL.md § Blocking Conditions`) cover missing config, needed installs,
  exhausted patch attempts, semantic-changing patches, unreachable devices,
  accuracy below threshold, unknown operators, context-binary failures, SDK edits,
  and ambiguous venvs.
- **No silent CPU fallback.** If QNN/HTP fails, the agent fixes it or reports it.
  Substituting an ONNX/CPU run for a failed HTP run is never an acceptable fix.
- **Every number comes from a real run.** Top-K, latency, and cosine values must
  trace to an actual execution log — never estimated from model knowledge.
- **Accuracy gate:** cosine ≥ 0.99 for FP16/FP32, ≥ 0.95 for quantized. Below
  threshold the agent stops (**B6**) and presents options — it does not silently
  auto-tune.
- **The SDK tree is read-only** (**B9**). A fix that requires editing anything
  under `$QAIRT_SDK_ROOT` / `$QNN_SDK_ROOT` stops for explicit, file-scoped
  approval; the normal answer is to copy the file into the workspace and point the
  tooling at the copy.
- **Deliverables:** `REPORT.md` with the cosine summary, `infer_<model>.py`, and
  `inference_manifest.json` (the App Builder hand-off) — plus an updated
  `plan.md`.

---

## Example prompts

Step-by-step:

- **ONNX baseline** — `"Run ONNX inference on my model to get the baseline"`
- **Convert (QNN, the default)** — `"I have real_esrgan_x4plus.onnx, convert it
  for my Qualcomm device"` → host detection, DLC-portability question, Flow A,
  `<model>_fp16.bin`
- **Convert (SNPE)** — `"Convert real_esrgan_x4plus.onnx to an SNPE DLC"` →
  `<model>_fp32.dlc`, optionally quantized to `<model>_w8a8.dlc`
- **Quantize** — `"Create a W8A16 QNN quantization, use COCO128 for calibration"`
  (a single image and its augmentations is not diverse calibration data)
- **Inference from a context binary** — `"Create a QNN inference script from
  @onnx_inference.py using @yolov8n_a16_w8_qnn_ctx.bin"`
- **Inference from a DLC** — `"Create an SNPE inference script from
  @onnx_inference.py using @yolov8n.dlc"`
- **Operator patching** — `"Conversion failed on an unsupported operator, patch
  the model"`

Whole project in one go: state the goal and the target, e.g. *"Take
`model.onnx`, produce a W8A16 QNN context binary for this WoS device, and validate
it against the ONNX baseline."* The agent initializes
`${WORKSPACE}\<model_name>\`, fills the Project Config block in `plan.md`
(`MODEL_NAME` / `FLOW` / `PRECISION` / `TARGET_DEVICE` / `MODE`, plus
`CALIBRATION_DATA` for quantized runs and `ADB_*` / `RETMOE_DEVICE_INFO` for
remote targets), then runs the phases. Review that block before it proceeds —
`MODE = batch` (the default) means it will run every phase autonomously and stop
only on a blocking condition; `MODE = interactive` confirms at each phase.

If the agent drifts off the workflow, re-send the request prefixed with
`"follow the model-builder skill"`. Agents are not perfectly steerable; an
explicit constraint prefix pulls it back to the documented rules.

---

## Tested scenarios

- **WoS (Windows on Snapdragon)** — convert and run inference on the same device
  (Snapdragon X Elite / X2 Elite).
- **Remote ARM Linux**
  - **qcs6490** — convert an SNPE model (FP or quantized) on an x86 host, run
    inference on the qcs6490 target.
  - **RB8** — convert a QNN or SNPE model on an x86 host, run inference on the RB8
    target.

## Known issues

- **qcs6490 + QNN quantization** may fail with `<E> The SocModel doesn't support
  FP16`. `--preserve_io` during conversion can trigger FP16 preservation on some
  SoC configurations. Workaround: use the SNPE DLC quantization flow instead —
  generate a quantized `.dlc` and run it on the SNPE runtime.

---

## Maintaining this skill

- **`SKILL.md` is the contract.** Keep rules there and in `references/`; this
  README should point at them, not restate them, or the two drift.
- **No absolute paths.** Use the `${APP_ROOT}` / `${WORKSPACE}` /
  `<python_x64_venv>` placeholders — the install location differs per machine.
- **Shared knowledge goes to `_shared/`** and is referenced by path from each
  skill. Do not copy the body into this directory.
- **Errors belong in the `troubleshooting/` sub-SKILLs**, which are the canonical
  authority; inline summaries elsewhere must stay shorter than they are.
- **New model keywords** go in `taxonomy_rules.yaml` (one keyword, one task — the
  loader asserts uniqueness).

Licensed BSD-3-Clause; see `LICENSE`.
