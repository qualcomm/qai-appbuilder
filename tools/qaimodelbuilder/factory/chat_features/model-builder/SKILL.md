---
name: model-builder
description: QAI ModelBuilder. Tools and workflows for model conversion, inspection, operator patching, quantization, and inference validation of self-converted models on Qualcomm platform. Use this skill when working with custom ONNX/PyTorch models: export to ONNX, convert to QNN/SNPE DLC, FP16/FP32/INT8 quantization, operator patching, context binary generation, and inference validation of self-built models. NOT for AI Hub prebuilt packages — use aihub-model-run skill instead. Supports QAIRT SDK 2.45+ on Windows on Snapdragon (WoS) ARM64 devices.
---

# QAI ModelBuilder

> ## 🚨 MANDATORY: READ THIS ENTIRE FILE BEFORE TAKING ANY ACTION
>
> Critical constraints (working dir, tool paths, env rules) are distributed throughout —
> missing any section causes incorrect behavior.
> 1. Read this SKILL.md top to bottom **first**; read `references/*.md` only as needed.
> 2. Don't read Python scripts unless a reference doc is missing usage details — and if you
>    do, update the reference doc afterward so future sessions don't repeat the read.
> 3. Don't run commands to verify facts already in SKILL.md or `${APP_ROOT}\data\config\qairt_env.json`
>    (torch version, Python paths, tool locations, …) — trust the docs and act.

---

## 🚨 SKILL Boundary Decision — Read Before Activating (MANDATORY GATE)

> **Before activating this skill, you MUST pass the decision tree below first. Skipping this step is the root cause of misusing this skill.**

> ### ✋ Agent activation self-check (MANDATORY — answer all three questions before continuing)
>
> Before activating this skill, the agent MUST clearly answer the following three questions. **If ANY veto condition holds, stop immediately and switch skills — do not continue.**
>
> | # | Question | YES → | NO → |
> |---|------|-------|------|
> | Q1 | Does the model the user mentioned **already have a prebuilt package on AI Hub** (e.g. Zipformer, MobileNet, YOLO)? | ❌ **Stop**, switch to the `aihub-model-run` skill | Continue to Q2 |
> | Q2 | Is the file the user wants to download/use an AI Hub prebuilt artifact (QNN context binary `.bin` / `.dlc`)? | ❌ **Stop**, switch to the `aihub-model-run` skill | Continue to Q3 |
> | Q3 | Does the user have a **custom ONNX/PyTorch model** that needs converting, or needs re-quantizing/recompiling into a custom `.bin`? | ✅ Activate this skill | ❌ User intent unclear — confirm first |
>
> **Common trigger words for Q1/Q2 (matching any one → switch to `aihub-model-run` immediately, do NOT handle inside this skill):**
> - "download from AI Hub" / "model on AI Hub" / "on-device pre-exported package" / "prebuilt package"
> - A model name + "download" where that model already exists on AI Hub (e.g. Zipformer, Whisper, Inception, ResNet)
> - "QNN_CONTEXT_BINARY" / "QNN_DLC" / "AI Hub prebuilt context binary"

> ### 🤖 Dispatching a sub-agent to another skill (especially `aihub-model-run`) — MANDATORY
>
> **Background lesson (2026-06)**: After judging that a task belonged to `aihub-model-run`, we once **did not read its SKILL.md in full first**, and instead — under the context pollution of this skill's (model-builder) 70KB full text plus various `factory\chat_features\model-builder\scripts\...` paths — wrote a sub-agent instruction "out of a knowledge vacuum" telling it to "go search `C:\Shared` / `C:\WoS_AI` for `.bin`/`metadata.json`". A sub-agent is a **completely fresh, blank context that inherits NOTHING from this skill**, so it can only follow the prompt → it triggered a full recursive scan and hung for 30+ minutes.
>
> Therefore, when the GATE above concludes "the task belongs to another skill and a sub-agent must be dispatched", you **MUST**:
>
> 1. **First `read` the target SKILL.md in full** (e.g. `${APP_ROOT}\skills\aihub-model-run\SKILL.md`), then write the sub-agent prompt based on its content. **Dispatching a sub-agent without first reading the target SKILL in full is forbidden.**
> 2. **The first instruction in the sub-agent prompt MUST be "read the target SKILL.md in full before acting"** — the sub-agent does not inherit the main agent's context.
> 3. **The sub-agent prompt MUST NOT carry any path / script / toolchain reference from this skill (model-builder)** (`run_pipeline.py`, `qnn-onnx-converter`, `qai_convert_*.py`, `qairt_sdk_root` conversion tools, etc.) — they apply only to "custom ONNX/PyTorch conversion", are useless for AI Hub prebuilt packages, and would lure the sub-agent into a wrong full-disk file search.
> 4. If the target SKILL provides a "Sub-Agent Dispatch Template" (`aihub-model-run` has one), **reuse its template directly**.

**Quick decision reference:**

| User need | Correct skill | Inference tool |
|---------|-----------|--------|
| Download a prebuilt package from AI Hub (e.g. Inception v3, MobileNet) and run inference | **`aihub-model-run`** | `qai_appbuilder` / `QNNContext` (all NPU inference goes through this path) |
| Custom ONNX/PyTorch → convert → inference | **this skill** (`model-builder`) | `qai_appbuilder` / `QNNContext` |
| Quantize/recompile an existing model into a custom `.bin` | **this skill** (`model-builder`) | `qai_appbuilder` / `QNNContext` |

> ⚠️ **AI Hub prebuilt packages — belong to the `aihub-model-run` skill; this skill does not handle them:**
>
> A prebuilt package downloaded from AI Hub contains a QNN context binary (`.bin`) or `.dlc`; these are all loaded directly by the `aihub-model-run` skill via `qai_appbuilder` / `QNNContext` and run on the NPU. This skill only converts/compiles **custom ONNX/PyTorch** models into NPU models; it does not handle AI Hub prebuilt artifacts.

---

## 🧭 Problem Routing Index — 遇到问题先查这里（MANDATORY）

> **本 SKILL 是转换主流程 + 索引层。** 遇到**报错/精度/性能**问题时，**先查下表定位到对应的子 SKILL，只加载命中的那一个**，避免把全部知识读进上下文。子 SKILL 各自是独立的按需知识文档。
>
> **tier 说明**：`base` 子 SKILL 随外部版发布；`advanced` 子 SKILL 仅内部版存在（external 版物理无此目录 —— 若磁盘上不存在对应文件，静默跳过，不报错、不占位）。

| 症状 / 错误码 / 需求 | → 加载子 SKILL | 位置 | tier |
|---|---|---|---|
| `unsupported operator` / `0xc26` / Einsum / Mod / Floor / ScatterND / dry-run 误报 | operator-patching | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/operator-patching/SKILL.md` | base |
| `Graph Compose failure` / `graph_names` / `Wrong number of Parameters 5` / `loadRemoteSymbols 4000` / arch 不匹配 | conversion-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/conversion-troubleshooting/SKILL.md` | base |
| QNNContext 崩溃 / stale artifact / 多模型同进程 / Linux HTP transport mismatch / NCHW-NHWC 错 | inference-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/inference-troubleshooting/SKILL.md` | base |
| VCTargetsPath / CMake / import cv2·Pillow / qai_appbuilder import 失败 | env-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/env-troubleshooting/SKILL.md` | base |
| 0-byte generator / `WinError 193` / 需修改 SDK 文件 | sdk-integrity-recovery | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/sdk-integrity-recovery/SKILL.md` | base |
| basicsr / functional_tensor / aux 分支 ReshapeOp | export-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/export-troubleshooting/SKILL.md` | base |
| 待转模型是 YOLO/检测/分割/Transformer 等量化敏感架构 / 量化前想预判风险与推荐精度 | quantization-sensitivity（转换前风险预判速查表） | `${APP_ROOT}/factory/chat_features/model-builder/references/quantization-sensitivity.md` | base |

> **说明**：上表的排障/精度/性能知识已抽取为独立子 SKILL。本 SKILL 正文中仍保留的相关段落（Blocking Conditions、Operator Patching、Step 8 精度、Tool Path 错误等）为**主流程内联摘要**，遇到具体问题时**以对应子 SKILL 为准**（子 SKILL 内容更完整）。

---

## Trigger Phrases

**Only after passing the "SKILL Boundary Decision" above** should you activate this skill:

> ⚠️ "AI Hub download" / "AI Hub prebuilt package" / "on-device pre-exported package" → **do NOT activate this skill by themselves**; switch to `aihub-model-run`. Only when the user explicitly says "needs re-compilation/quantization" does it belong to this skill.

### Conversion (when custom ONNX/PyTorch conversion is needed)
- "convert model to qnn" / "qnn conversion" / "convert to qnn"
- "convert model to dlc" / "snpe conversion" / "convert to dlc"
- "convert onnx to qnn" / "convert onnx to snpe"
- "generate context binary" / "context bin" / "qnn context binary"
- "run qnn inference" / "snpe inference" / "qairt inference" — ⚠️ **Only activate this skill when the inference target is a self-built/self-converted model; if the inference target is an AI Hub prebuilt package → use `aihub-model-run` instead**

### Operator Patching
- "operator not supported" / "unsupported operator"
- "patch operator" / "operator patch"
- "converter failed" / "conversion failed"
- "dry run failed" / "unsupported ops found"

### Diagnostics
- "check htp" / "htp ready" / "htp check"
- "diagnose" / "environment check"

## 🚦 MANDATORY: Ask the DLC Portability Question FIRST (before any conversion work)

> **Trigger the moment you activate this skill — do NOT wait until Step 4.**
>
> The user may walk away after handing you the task. If you delay this question until Convert time, they may never see it and the task will silently pick a suboptimal default. Ask it right after you decide this skill applies.

**Ask (via the `question` tool, one question, two options):**

> "Should the generated DLC be **portable across HTP platforms** (v73 X Elite + v81 X2 Elite, same DLC works on both), or **optimised for the specific SoC** you plan to deploy on?"
>
> - **Option 1 (default):** Cross-platform DLC — same DLC works on both HTP v73 and v81. `.bin` is still generated per SoC afterwards. **Recommended unless you have a reason to specialise.**
> - **Option 2:** SoC-optimised DLC — tuned for one specific HTP version, slightly better on-device performance, but the DLC cannot be reused on the other SoC. Ask which HTP version (v73 = Snapdragon X Elite / SC8380XP; v81 = Snapdragon X2 Elite / SM8750).

**Skip the question** only when the user's original request **already contains** one of these signals:

| Signal in the user's own words | Interpretation |
|---|---|
| "optimise for current device" / "tune for this SoC" / "specific to X Elite" / "for X2 Elite only" | → Option 2 (SoC-optimised) |
| "portable" / "works on all devices" / "cross-SoC" / "share with other machines" | → Option 1 (cross-platform) |
| Nothing about portability | → **Ask** — don't guess |

**Failure-safe default (fallback):** if for any reason you did NOT ask this question and started converting, the default in `run_pipeline.py` is **cross-platform DLC**. This is deliberate — the user is never worse off than "generic DLC + per-SoC .bin", which is a workable output on every supported device. But this is a *fallback*, not permission to skip the question. Always try to ask first.

**How the answer maps to CLI:**

| Answer | `run_pipeline.py` flag |
|---|---|
| Cross-platform (default) | *(nothing — omit `--soc_optimized`)* |
| SoC-optimised | `--soc_optimized --htp_version v73` (or `v81`) |

> ℹ️ `--htp_version` on its own does NOT imply SoC optimisation — it only determines which `.bin` gets generated for the final context binary step. Cross-platform DLC + `--htp_version v73` is perfectly valid (and is the safest default when the target device is X Elite).

---

## When to Use

This skill is the **model conversion/compilation pipeline** (converting custom ONNX/PyTorch into NPU models). The default Flow A (`run_pipeline.py`, `ONNX → DLC → .bin`) needs only the QAIRT SDK. The legacy Flow C (`run_pipeline_legacy.py`, DLL-based) additionally requires **VS 2022 + VS ARM64 env** to compile the ARM64 DLL.

**✅ Applicable scenarios (use this skill):**
- You have a custom ONNX and AI Hub has no corresponding prebuilt package
- You need to quantize/recompile into a custom `.bin` yourself
- You need post-conversion inference validation of a self-built model

**❌ Non-applicable scenarios (use the `aihub-model-run` skill instead):**
- You want to run inference on a model that already has a prebuilt package on AI Hub (e.g. Inception v3, MobileNet, YOLO)
- You only need to download the prebuilt artifact and run inference directly — no VS 2022 needed, no QAIRT SDK conversion needed

Use this skill for Qualcomm QAIRT/QNN/SNPE model bring-up:
- Export model to ONNX
- Inspect ONNX I/O
- Convert to QNN or SNPE/DLC
- Quantize model (FP16/FP32/INT8/A16W8/A8W8B8)
- Generate context binaries
- Run inference and validation **of self-converted models** (NOT AI Hub prebuilt packages)

## Flow Selection Guide

> Choose the conversion flow based on target device and deployment scenario.

| Criteria | Flow A — DLC→bin (default) | Flow B — SNPE | Flow C — DLL→bin (legacy) |
|----------|-----------------------------|---------------|----------------------------|
| Output format | `.dlc` -> `.bin` | `.dlc` | `.bin` + `.cpp` + `.so`/`.dll` |
| Inference API | `qai_runner.py` wrapper | `qai_runner.py` wrapper | `qai_runner.py` wrapper |
| Supported runtimes | HTP | DSP, CPU, GPU | HTP, CPU, GPU |
| Context binary | Required (generated from DLC) | Optional | Required (Windows ARM) / Optional (Linux) |
| Quantization | FP32, FP16, **bf16**, W4A8, W4A16, W8A8, W8A8B8, W8A16, **W16A16** | FP16, FP32, INT8, A16W8 | FP16, FP32, INT8, A16W8, W4A16, W4A8, W8A8B8 |
| Primary target | WoS ARM64, ARM Linux, cross-SoC deployment | Android, Embedded Linux | WoS ARM64 (legacy DLL artifact needed) |
| Converter tool | `qairt-converter` + `qairt-quantizer` + `qnn-context-binary-generator` | `qairt-converter` | `qnn-onnx-converter` |
| Script | **`run_pipeline.py`** (default, end-to-end automation) | `qai_convert_snpe.py` | `run_pipeline_legacy.py` |
| Key advantage | **Default. Strategic direction.** No VS ARM64 compile needed. Full CLE / per-channel / bf16 / w16a16 / cross-SoC support. | Simplest conversion | Emits an ARM64 `.dll` alongside `.bin` (only path that produces a DLL artifact) |

> ⚠️ **Two conversion paths exist for WoS ARM64:**
> - **Flow A** (`ONNX → DLC → .bin`) — **default, recommended**. Fully automated via `run_pipeline.py`. Strategic direction; will eventually be the only WoS path.
> - **Flow C** (`ONNX → C++/BIN → DLL → .bin`) — **legacy, retained**. Fully automated via `run_pipeline_legacy.py`. Use ONLY when the user explicitly asks for the DLL-based pipeline (or specifically needs the `.dll` artifact for compatibility). Kept working for regression comparison and for the rare case where the DLL is the required deliverable.
> When in doubt, use Flow A.

**Default flow selection:**
- Windows on Snapdragon (WoS) ARM64 targets -> **Flow A (DLC→bin)** ← default; Flow C only when user explicitly requests the DLL pipeline
- ARM Linux (Qualcomm SoC) targets -> **Flow A** (DLC→bin)
- Cross-device deployment (both v73 and v81) -> **Flow A** (default — cross-platform DLC. No extra flag; omit `--soc_optimized`)
- Android / DSP targets -> **SNPE** (Flow B)
- x86 Linux (CPU-only inference) -> **Flow A** (CPU backend on the generated DLC)
- Quick validation without any context binary -> **DLC direct load** via `QNNContext` (skip Step 3 with `--skip_contextbin`)

## Required Guardrails

- Run skill scripts from their original skill path unless explicitly noted
- Do not swap out QAIRT toolchains ad-hoc
- On Windows, do not rely on Python arch detection — use OS-native arch commands
- **⏱️ Command timeout (MANDATORY)**: model conversion / quantization run long. **Use `timeout=0` (no limit) for ALL conversion commands** — ONNX export, `qai_convert_fp.py`, `qai_convert_int.py`, `qai_dev_gen_contextbin.py`, and any `.bat` wrapping these. Don't set fixed timeouts; user can cancel manually if needed.

  **Reference times** (expectation only, NOT for setting timeouts):

  | Operation | Typical time | Notes |
  |-----------|-------------|-------|
  | ONNX export (≤256×256, torch 2.x) | ~10s | Optimized: no forward pass + `do_constant_folding=False` |
  | ONNX export (512×512, torch 2.x) | ~41s | Optimized |
  | ONNX export (512×512, torch 1.13) | ~163s | Optimized; ~275s unoptimized |
  | FP16/FP32 conversion | ~30-120s | Per model |
  | Context binary generation | ~200s | Per model |
  | W8A8 quantization (512×512, 2 samples) | ~392s | Scales with input size x sample count |
  | W8A16 quantization (256×256, 20 samples) | ~482s | Reference |
  | W8A8 quantization (256×256, 20 samples) | ~351s | Reference |

  > ⚠️ Quantization time scales with input H×W and calibration sample count
  > (512×512 takes ~4× the time of 256×256). Check model input shape
  > (`qai_inspect_onnxio.py`) before estimating.

- **Cross-platform shell commands:**
  - Python scripts via `subprocess.run()` — no shell quoting issues
  - **Inference execution policy (MANDATORY):** run inference via `qai_runner.py` wrapper / `qai_appbuilder` / `QNNContext` only — NPU inference goes through these exclusively; `onnxruntime` is allowed ONLY with `CPUExecutionProvider` for an ONNX baseline. Details + wrapper rules → Step 7/8. 
    - **Benign HTP errors (non-fatal, ignore — inference still correct):** `setPowerConfig error 0x32c9` (couldn't switch to BURST under restricted perms; run as admin or `performance_profile: "default"` to avoid); `Error 0x200: failed to close queue` (HTP queue teardown timing at model destroy — result already written); `m_CFBCallbackInfoObj is not initialized` (HTP v81 callback init order). None affect results; never degrade to CPU on account of these.
  - Avoid PowerShell variables (`$_`, `$env:`, `!`) in bash-invoked commands — use temp `.ps1` files with `-File` or Python `glob.glob()` instead
- **Windows terminal encoding (MANDATORY):** Inference templates (`scripts/inference/infer_*.py`) and `qai_runner.py` already pre-set `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` — copy-and-run works with Unicode (`→`, `✓`, non-ASCII text, emoji). For a **new** inference script from scratch, add the same reconfigure block right after `import sys`. Do NOT use `set PYTHONUTF8=1 && ...` (cmd appends a trailing space → `invalid PYTHONUTF8 value` crash); the in-script reconfigure makes any env var unnecessary.
- **Escalation:** If conversion still fails after export/patch/retry, do not silently replace model architecture. Record error + logs + ONNX snapshot → escalate with full bundle. For B3/B4/B7 criteria → open `references/operator_patching.md`.
- **Dynamic-input ONNX:** If ONNX has dynamic inputs, pass explicit shapes during conversion. See `references/qnn_conversion.md` (QNN: `--input-dim`) or `references/snpe_conversion.md` (SNPE: `--source-model-input-shape`).
- **Prohibition on verifying known information via commands (MANDATORY):** Information already available in SKILL.md or `${APP_ROOT}\data\config\qairt_env.json` MUST be used directly — never confirmed via shell commands.

  **Forbidden command patterns (never run these):**

  | Forbidden command | Why forbidden | Correct approach |
  |-------------------|---------------|------------------|
  | `python -c "import torch; print(torch.__version__)"` | torch version is known: `python_x64_venv` always uses torch 2.x (installed by `Setup.bat`) | Use `opset_version=18` directly per Rule 7 |
  | `python --version` or `python -V` | Python versions are fixed: x64=3.10, ARM64=3.13 per `${APP_ROOT}\data\config\qairt_env.json` | Read version from `${APP_ROOT}\data\config\qairt_env.json` key names |
  | `where python` / `where pip` | Python paths are in `${APP_ROOT}\data\config\qairt_env.json` under `python_x64_venv` / `python_arm64_venv` | Read directly from `${APP_ROOT}\data\config\qairt_env.json` |
  | `pip show torch` / `pip list` | Package versions are known from the established environment | Trust the environment; proceed with task |

### Do Not... (MANDATORY)

- **Do not derive from existing artifacts** in the workspace (`.bin` / `.cpp` / `_net.json` / `.so` / `.dll` / calib files): each pipeline stage must run fresh — old artifacts may be from different precision, patches, or incomplete runs.
- **Do not browse QAIRT SDK source folder** (`$QAIRT_SDK_ROOT` / `$QNN_SDK_ROOT`): use only documented CLI tools / Python APIs from this SKILL + references.
- **Do not hardcode SDK Doxygen-generated HTML filenames** (e.g. `enum_QnnTypes_8h_<hex>.html`): the hash suffix is regenerated per SDK build, so the link breaks on upgrade. Reference the C header instead (e.g. `<QAIRT_SDK_ROOT>\include\QNN\QnnTypes.h`) — header paths are version-stable.
- **Do not modify any file under the QAIRT SDK** — hard **B9** Blocking Condition. The SDK is a shared
  third-party install; editing it corrupts shared state. Concluding "the fix needs an SDK file change" is
  **itself** B9 → **stop and ask the user** for explicit, scoped permission (cite the exact path; generic
  "go ahead" is not consent). Applies without exception to `.exe`/`.dll`/`.so`/`.lib`/`.cat`, all SDK Python
  modules, backend JSON/headers, HTP runtime files (copy into workspace `output/` — never edit originals).

  **Pre-flight before every write/exec:** does the absolute write target (incl. redirections `>`/`>>`,
  `-Destination`, `--target`, relative paths resolved against CWD, `cmd /c`, sub-agents, `.bat`) land under
  `$QAIRT_SDK_ROOT` / `$QNN_SDK_ROOT` / `C:/Qualcomm/AIStack/QAIRT/...`? If yes → **STOP, trigger B9.**
  **Correct workaround:** copy the SDK file into the workspace, edit the *copy*, point tooling at it via
  documented overrides (`--config_file`, `QNN_*` env, workspace-local `backend_extensions.json`).
  **Reading the SDK dir is allowed** (`dir`/`ls`/`read`/`grep`); any write that changes a file's
  content/timestamp/size under the SDK = B9.

  > 🧭 **Full B9 discipline — the 2026-06-16 0-byte-generator incident, the three pre-flight escape hatches,
  > read-only `WinError 193` diagnosis (do NOT misjudge as "x64 can't spawn ARM64"), and single-file SDK
  > recovery without a 2 GB reinstall → load the `sdk-integrity-recovery` sub-SKILL:**
  > `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/sdk-integrity-recovery/SKILL.md`
  > (canonical source for SDK-integrity handling).

### About QAIRT version annotations

Strings like `QAIRT 2.45 WoS` / `Verified on QAIRT 2.47.1.260610` in scripts/docs are **historical verification annotations**, not hard requirements — QAIRT 2.x is backward compatible, later versions usually work unchanged. The installed version is the single source of truth in `${APP_ROOT}\data\config\qairt_env.json` (`_version` field). Don't "fix" old version strings to a newer one, and don't alarm the user when annotations and installed version differ. In `.bat` snippets use `%QAIRT_SDK_ROOT%`, not a hardcoded versioned path.

> ⚠️ **Incompatibility between the QAIRT version and AI Hub prebuilt packages**: the backward-compatibility rule **applies only to artifacts this skill generates itself**. The `.bin` of an AI Hub prebuilt package is strictly bound to the QAIRT version it was compiled with (version mismatch → `Error code: 5000`). When you encounter an AI Hub prebuilt package → you MUST go through the `aihub-model-run` skill.

---

## Execution Mode: Batch vs Interactive

> Read `MODE` from the workspace config before starting any task. Default is `batch`.

### `batch` mode (default)

Agents execute the full pipeline **autonomously** without asking for confirmation at each step.

**In batch mode, agents MUST:**
- Proceed through all phases without pausing for user confirmation
- Continue beyond local artifact generation when validation phases remain
- Apply safe defaults for any unspecified optional parameters
- Log every decision and assumption in the workspace Issue Log
- Mark each phase done upon completion
- **Only stop** when a Blocking Condition is encountered (see below)

**In batch mode, agents MUST NOT:**
- Ask "should I proceed to the next phase?"
- Ask "which precision should I use?" (use PRECISION from config)
- Ask "should I run onnxsim?" (always run it)
- Ask "should I simplify the model?" (always simplify)
- Pause for routine confirmations that can be resolved from config values
- **Silently fall back to ONNX/CPU when QNN/HTP fails** — "fully automatic" authorizes normal-path automation only; on failure, attempt to diagnose and fix the problem first; stop and report to user only when the problem cannot be resolved. Substituting ONNX/CPU inference for a failed QNN/HTP run is never an acceptable fix.

### `interactive` mode

Agents ask the user for confirmation at each phase transition and before key decisions.

---

## Blocking Conditions (Always Stop — Both Modes)

> These conditions **always** require stopping and asking the user, regardless of mode.

| # | Condition | Action |
|---|-----------|--------|
| B1 | Required config variable is empty or placeholder | Stop. List missing variables. Ask user to fill them. |
| B2 | `pip install` is needed | Stop. State the package and reason. Ask user for permission. |
| B3 | Unlimited patch iterations exhausted with NO progress (same ops failing, no replacement patterns available) | Stop. List all attempted patches and logs. Escalate to user. |
| B4 | Operator patch would change model semantics (e.g., replace attention with different behavior) | Stop. Describe the change. Ask user to approve. |
| B5 | Target device is unavailable for context binary generation or on-device testing | Stop. Ask user how to proceed. |
| B6 | Accuracy drops below threshold after quantization (cosine < 0.95) | Do NOT auto-apply fixes in sequence. ① Run a **zero-cost diagnosis** first: check if calibration data is a single image / its augmentations (common root cause — augmentations of ONE image are NOT diverse). ② Then **STOP and report to the user**: state measured cosine + diagnosis, and present the fix options **with a one-line principle each**, then ask which to try. Wait for the user's choice. Options: (1) improve calibration diversity — real multi-class samples; (2) `run_pipeline.py --cle` — Cross-Layer Equalization on the DLC path (default); pass `--per_channel` alongside for a stronger fix; (3) raise to W8A16 (`--precision w8a16`); (4) keep FP16 or try `--precision bf16` (wider dynamic range than FP16); (5) accept current precision if Top-K is correct. Full option list + principles → inference-validation step 6. For detection models with mixed-magnitude outputs also check `references/model_quantization.md` § Large-Dynamic-Range. |
| B7 | No known replacement pattern exists for unsupported operator | Stop. Document operator, escalate to user. |
| B8 | Context binary generation fails on Windows ARM | **Stop.** `run_pipeline.py` exits non-zero — failure is surfaced, NOT silently degraded (the user asked for a context binary, so deliver that type or error). Return to operator patching. Do NOT retry alternate generators (e.g. the x86_64 build cannot load an ARM64 DLL). A 0-byte / corrupt `qnn-context-binary-generator.exe` means the SDK file was damaged. `qai_dev_gen_contextbin.py` now **self-heals**: before launch it re-extracts just that one file from the kept SDK zip (`data/sdk/qairt/v<version>.zip`, or `vendor/qairt/v<version>.zip`) — no 2 GB reinstall. If self-heal reports no usable zip, see **Manual SDK file recovery** below (recover the single damaged file from the kept zip / launcher-script backup yourself). Diagnose READ-ONLY; never run anything that could re-write the SDK. Escalate only if B3/B4/B7 met. |
| B9 | The agent has concluded (or is about to conclude) that fixing the issue requires modifying any file inside `$QAIRT_SDK_ROOT` / `$QNN_SDK_ROOT` (incl. `bin/`, `lib/`, Python packages, JSON templates, DLLs) | **STOP IMMEDIATELY.** Do not edit, copy-over, rename, or delete any SDK file. The whole `C:\Qualcomm` tree is now **write-protected at the tool layer** (ALWAYS ON, independent of FileGuard): `write`/`edit`/`apply_patch`, `exec` command write targets (`>`/copy/del/Out-File…), and Python child-process writes into it are all denied automatically — so an accidental write can no longer corrupt the SDK. Document the exact path, the proposed change, and the root cause in the Issue Log. If a file is genuinely missing/corrupt, recover it from the kept SDK zip / launcher-script backup (see **Manual SDK file recovery** below) rather than editing the SDK. Ask the user with the explicit prompt: *"This needs editing `<sdk_path>/<file>`. May I proceed? [y/N]"*. Only act after a scoped **yes** that names the file. See **Do Not Modify QAIRT SDK Files** above. |
| B10 | A tool, script, or Python package needs to be run but is **not described in SKILL.md**, and it is unclear which Python environment (`python_x64_venv` vs `python_arm64_venv`) to use | **STOP.** Do not guess. State the tool/package name and ask the user which venv to use. Do NOT default to `python_arm64_venv` just because the end goal is inference — the correct env depends on what the tool does internally (e.g. links against `python310.dll` → must use `python_x64_venv`). |

#### Manual SDK file recovery (fallback when auto self-heal did not fix it)

If `qai_dev_gen_contextbin.py` self-heal did not repair a damaged SDK file, recover the
single file yourself from the kept SDK zip (`${APP_ROOT}\data\sdk\qairt\v<version>.zip` /
`${APP_ROOT}\vendor\qairt\v<version>.zip`) or the launcher-script backup
(`${APP_ROOT}\data\sdk\qairt-scripts\<arch>\<name>`) — **no ~2 GB reinstall**. Diagnose
READ-ONLY; the one legitimate write into `$QAIRT_SDK_ROOT` uses a scoped
`QAI_PROTECTED_PATHS_BYPASS=1` around the single extract/copy, cleared immediately.

> 🧭 **Full step-by-step recovery (single-file zip extract PowerShell, launcher-script &
> `qnn-context-binary-generator.exe` backup paths, MZ/`#!` verify, bypass rules) → load the
> `sdk-integrity-recovery` sub-SKILL:**
> `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/sdk-integrity-recovery/SKILL.md`
> (canonical source for SDK-integrity handling).

### ⚠️ CRITICAL: Inference Results Must Come From Actual Execution (MANDATORY)

**Never output inference result data (Top-K, confidence, latency, cosine similarity,
accuracy, …) without first executing the inference script via `exec`.** Every number in
the report must trace back to a specific line in the `exec` output log; if `exec` was not
called or execution failed → report the error only, output **no** numeric results.

❌ **Forbidden:** generating/guessing/"estimating" results from model knowledge,
writing the report first then running `exec` to "confirm", claiming results from a
sub-agent without a visible tool-call. **User verification:** ask to see the raw `exec`
output log; if no `exec` call precedes the result report → results are fabricated.

### ⚠️ CRITICAL: Operator Patching — Exhaustive Patching Required

Continue patching ALL unsupported ops until no replacement patterns exist. Never fall back to CPU.
For patching rules, escalation policy (B3/B4/B7), and code templates → open `references/operator_patching.md`.

   **Exhaustive Patching Rules:**
   - DO NOT stop at a fixed iteration count (no "max 7 tries" limit)
   - DO NOT stop because an operator seems "fundamental" (Floor, Transpose, Reshape may all have replacement patterns)
   - MUST search `references/operator_patching.md` for each unsupported op
   - MUST document each iteration: what was tried, what failed, what changed
   - Continue until ALL ops resolved OR no replacement pattern exists
   - Escalate ONLY when: (a) No replacement pattern (B7), (b) Patch changes semantics (B4), (c) 7+ iterations with zero progress on the SAME ops (B3)

### ⚠️ CRITICAL: QAIRT 2.45 WoS ARM64 Tool Path Rules

On **Windows on Snapdragon (WoS) ARM64** with QAIRT 2.45+, each step uses a **different arch directory**:
- `qnn-onnx-converter` → `bin/x86_64-windows-msvc/` (x86 emulation)
- `qnn-model-lib-generator` → `bin/aarch64-windows-msvc/` (**NOT** x86_64 — most common mistake)
- `qnn-context-binary-generator.exe` → `bin/aarch64-windows-msvc/`
- Inference → ARM64 Python 3.13 (`python_arm64_venv`)

> ℹ️ **`Machine: AMD64` in conversion output is NORMAL on WoS ARM64** — `platform.machine()` returns `AMD64`
> because `qnn-onnx-converter` runs under x86 emulation. This does NOT mean the device is x86.
> The target arch (`windows-aarch64`) is detected separately via `systeminfo` and is correct.
> Do NOT conclude the device is x86_64 based on this output line.

> ⚠️ **VS ARM64 env** (`vcvarsall.bat arm64`) required for Steps 2 & 3 — must run inside a `.bat` file (not `cmd /c`). **Applies to Flow C only** (`run_pipeline_legacy.py`); Flow A / `run_pipeline.py` does NOT need it.
> ⚠️ **VCTargetsPath MUST point to VS 2022 Community** — BuildTools will cause `VCTargetsPath.vcxproj` / `BaseOutputPath not set` errors. **Applies to Flow C only.**
>   - ✅ `C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Microsoft\VC\v170\`
>   - ❌ `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\...` ← will FAIL
> ⚠️ **HTP runtime files** must be copied to working dir before Step 3 (files vary by HTP version — v73 vs v81 file lists, `arm64x`≠`aarch64`, v81 `--backend` must be `QnnHtp.dll` not the Stub → all in `references/context_binary.md`).
> ⚠️ **HTP runtime files CWD rule** — `qnn-context-binary-generator.exe` resolves `.cat` and `.so` files relative to its **process CWD**, NOT via PATH. The generator must be launched with `cwd=<output_dir>` (where the files were copied). `qai_dev_gen_contextbin.py` handles both copy and `cwd` automatically.
> ⚠️ `Wrong number of Parameters 5` / `Conv2d failed 3110` → in **Flow C**: caused by missing VS ARM64 env **AND/OR** missing HTP runtime files in CWD; in **Flow A**: caused by missing HTP runtime files in CWD (Flow A doesn't compile DLL, so no VS env issue). Check both before suspecting operator issues.
> ⚠️ `Unknown Key` warnings → non-fatal; `--no_simplification` recommended for WoS
> ⚠️ `WARNING_OP_VERSION_NOT_SUPPORTED: Operation <Op> ... got version: [16/18]` (e.g. LeakyRelu, because it was exported with opset≥16) → **pure warning, conversion still succeeds**; do not go back and patch based on this.

For full details → [`references/win_qairt_setup.md`](references/win_qairt_setup.md) | [`references/qnn_conversion.md`](references/qnn_conversion.md) | [`references/context_binary.md`](references/context_binary.md)

## ⚠️ CRITICAL: Working Directory Convention — File Placement Rules (STRICTLY ENFORCED)

> Note: `${WORKSPACE}` is the configured model workspace root (default `C:\WoS_AI`); it is substituted with the actual configured path when this skill is injected.

All model artifacts (ONNX, QNN libs, context binaries, inference outputs, raw files) **MUST** be placed under `${WORKSPACE}\`. **No exceptions.**

| Purpose | ✅ Correct Path |
|---------|----------------|
| Model project root | `${WORKSPACE}\<model_name>\` |
| ONNX output | `${WORKSPACE}\<model_name>\<model_name>.onnx` |
| QNN conversion output | `${WORKSPACE}\<model_name>\output\` |
| Context binary | `${WORKSPACE}\<model_name>\output\<model_name>.bin` |
| Inference output (raw/image) | `${WORKSPACE}\<model_name>\output\` |
| Calibration data | `${WORKSPACE}\<model_name>\calib\` |
| Log files | `${WORKSPACE}\<model_name>\` |

### ❌ FORBIDDEN Path Categories — NEVER write model artifacts here

The following **categories** of directories are tool/project infrastructure — writing model artifacts here pollutes the project and causes confusion. These rules apply regardless of the actual path on any user's machine:

| ❌ Forbidden Category | Pattern | Why forbidden |
|----------------------|---------|---------------|
| QAIModelBuilder tool root | Any path containing `QAIModelBuilder` | Tool infrastructure — scripts only, no artifacts |
| User home / Downloads | Any path under user home or Downloads folders | Not a designated model working directory |
| Current working directory (if not under `${WORKSPACE}\`) | `.` or relative paths that resolve outside `${WORKSPACE}\` | May silently write to wrong location |

> ⚠️ **AGENT SELF-CHECK (MANDATORY before writing any file):**
> Before writing ANY file (ONNX, `.so`, `.bin`, `.raw`, `.log`, calibration data, etc.),
> verify the destination path starts with `${WORKSPACE}\<model_name>\`.
> If it does not → **STOP** and correct the path before proceeding.
> Do NOT ask the user to move files after the fact.

### Intermediate file placement

`qai_convert_fp.py`, `qai_convert_int.py`, and `run_pipeline.py` automatically place all intermediate files (`.cpp`, `.bin`, `tmp_<pid>/`) under `--output-root` and set the correct CWD for `qnn-model-lib-generator`. No manual workaround is needed — just ensure `ONNX_FILE` and `OUTPUT_DIR` in `plan.md` are **absolute paths under `${WORKSPACE}\`** (see `plan.md` defaults).

**Rules:**
- ✅ Always bootstrap with `qai_workspace_init.py <model_name>` (creates `${WORKSPACE}\<model_name>\` + `output\` + `calib\`, copies `assets/plan.md` with `START_TIME`).
- ✅ In `plan.md`, `ONNX_FILE` / `OUTPUT_DIR` use absolute paths under `${WORKSPACE}\<model_name>\` (never bare filenames or relative paths).
- ❌ Never use a path containing `QAIModelBuilder` as a working directory for model artifacts; never hardcode user-specific paths (`C:\Users\<u>\...`); never let a script's default output path silently override this rule — review and correct before running.

**Create the working directory — backup existing first (MANDATORY):**

```bat
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_workspace_init.py <model_name>
```

`qai_workspace_init.py` does: rename existing `${WORKSPACE}\<model_name>` →
`<model_name>_bak_YYYYMMDD_HHMMSS` (exits with code 1 + clear error on rename
failure — never silently continues); create fresh `<model_name>/`, `output/`, `calib/`;
copy `assets/plan.md` → `<workspace>/plan.md` with `START_TIME` filled.
Use `--no-templates` to skip plan copy.

> ⚠️ Never overwrite or reuse an existing model directory without running this script first.

> ⚠️ **Do NOT substitute `mkdir` when `qai_workspace_init.py` errors out (MANDATORY)** — diagnose the root cause first:
>
> | Symptom | Root cause | Correct handling |
> |------|------|---------|
> | `'#' is not recognized as an internal or external command` | A `# comment line` got mixed into the `exec` call; cmd.exe does not support `#` comments, so it treats the comment line as a command and errors, yielding exit code=1 — but the real Python command actually already succeeded | Remove the comment line from the `exec` call, or use `shell='sh'` (bash supports `#`) |
> | `The system cannot find the path specified` | The `${APP_ROOT}` / `<python_x64_venv>` placeholders were not substituted with real paths | Read the actual `python_x64_venv` value from `qairt_env.json`, then build the command |
> | exit code=1 but no Python traceback | Same as above — the comment-line error masked the real result | Check stdout for the text `[OK] Workspace initialized` — if present, it succeeded |
>
> **The only criterion for success**: stdout contains `[OK] Workspace initialized: <path>`, NOT the exit code.

---

## Core Workflow

1. **Export source model to ONNX**
   - Use model's export script (e.g., `export_onnx.py`)
   - Use `python_x64_venv` Python (x86_64 3.10)
   - Recommended: opset_version=18 (torch 2.x) or 13 (torch 1.x)
   - Always call `model.eval()` before export

   > ⚠️ **Disable training-only branches before export** — branches like `aux_logits`,
   > dropout, or custom `if self.training:` paths may contain operators that QAIRT 2.45 cannot convert.
   > Fix: set `model.aux_logits = False; model.AuxLogits = None` after loading pretrained weights.
   > See [`references/model_export_validation.md`](references/model_export_validation.md) for full guidance.

   ### ⚡ ONNX Export Performance & Memory Optimization (MANDATORY for large models)

   Two rules MUST always hold (keep inline — they gate correctness, not just speed):

   - **Rule 5 — Always export FP32 ONNX (never FP16).** `qnn-onnx-converter` expects FP32; PyTorch CPU has no FP16 `Conv2d` (`slow_conv2d_cpu not implemented for 'Half'`). FP16 is applied later via `--precision 16` in QAIRT.
   - **Rule 7 — Always `opset_version=18` (NO `torch.__version__` check).** `python_x64_venv` is always torch 2.x (Setup-installed); torch 2.x min opset is 18 (lower auto-upgrades, downgrade fails on `Resize` etc.). Also `pip install onnxscript`.

   The full optimization rule set (Rules 1–4, 6, 8 — constant folding, skip-forward-pass,
   memory frees, torch-1.x warning suppression, `.onnx`+`.onnx.data` split), the validated
   benchmark table, and the optimized export template →
   [`references/model_export_validation.md`](references/model_export_validation.md).

2. **Inspect ONNX I/O and operator compatibility**
   - Run: `<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_inspect_onnxio.py ${WORKSPACE}\<model_name>\<model_name>.onnx`

   > ⚠️ **CRITICAL: Do NOT use dry-run as a gate before conversion.**
   > `qnn-onnx-converter --dry_run` warnings are frequently **false positives** — actual conversion
   > succeeds despite these warnings. Acting on dry-run output leads to unnecessary patching.
   > **Always proceed directly to Step 4 (actual conversion) first.**
   > Only return to Step 3 (operator patching) if **actual conversion exits with a hard error**.

3. **Operator Patching (only if actual conversion hits a hard op error)**

   Replace unsupported operators (Einsum / GridSample / ScatterND / Mod / Floor …) with
   QNN-compatible equivalents, **in-memory only**, then re-run conversion. Prefer in-memory
   model/symbolic patch over ONNX surgery. After **every** patch, validate:
   ONNX validity (`onnx.checker`) → actual conversion (not dry-run) → cosine ≥ 0.95 vs baseline.
   Track patched ops in `plan.md`; stop when all ops resolve or an exit criterion is met.

   > 🧭 **Full decision tree, per-op Error→Action table, Einsum decomposition patterns,
   > validation gates and escalation (B3/B4/B7) → load the `operator-patching` sub-SKILL:**
   > `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/operator-patching/SKILL.md`
   > (canonical source — this is a summary; the sub-SKILL is authoritative).
   > For non-operator conversion/compile errors instead → `conversion-troubleshooting`.

4. **Convert float model**

   > 🚨 **MANDATORY — WRAPPER SCRIPTS ONLY (NO EXCEPTIONS):**
   > - ✅ **Default: use `run_pipeline.py`** (the DLC-based Flow A pipeline). Handles ONNX → DLC → .bin end-to-end.
   > - 🕐 **Legacy: use `run_pipeline_legacy.py`** ONLY when the user explicitly asks for the DLL-based pipeline (Flow C — `ONNX → C++/BIN → DLL → .bin`), OR when they specifically need the ARM64 `.dll` artifact. It is retained working but not the strategic direction. Under the hood it calls `qai_convert_fp.py` / `qai_convert_int.py` for the manual per-step form.
   > - ⚠️ **Avoid calling `qairt-converter`, `qairt-quantizer`, `qnn-onnx-converter`, or `qnn-model-lib-generator` directly** unless the wrapper scripts cannot cover the required scenario. The wrapper scripts automatically handle `--preserve_io`, layout/color-encoding, `PYTHONPATH`, VS ARM64 env (legacy only), and correct arch directories. Bypassing them risks silent input format errors (wrong BGR/NHWC layout) that are extremely hard to diagnose.

   **Use `run_pipeline.py` — it handles Python paths, HTP file copy, and context binary automatically (no VS ARM64 env needed for the DLC path):**

   **HTP version selection** — use `--htp_version` to target the hardware's HTP version:

   | User says | `--htp_version` | Notes |
   |-----------|----------------|-------|
   | "use v73" / "HTP v73" / default | `v73` | Snapdragon X Elite (8380) |
   | "use v81" / "HTP v81" / "v81 architecture" | `v81` | Snapdragon X2 Elite (8480) |

   > ⚠️ **How to detect the HTP version** (when the user does not specify it): on the target device, in PowerShell use
   > registry query (NOT `Get-WmiObject Win32_SystemDriver` — WMI provider throttling causes it to hang indefinitely on busy systems):
   > ```powershell
   > Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
   >   Where-Object { $_.PSChildName -like "qcadsp*" } |
   >   Get-ItemProperty | Select-Object PSChildName, ImagePath
   > ```
   > Read the 4-digit SoC code from the INF filename in `ImagePath` and map it:
   > `8380` → Snapdragon X Elite → `v73`; `8480` → Snapdragon X2 Elite → `v81`.
   > **never use `Get-PnpDeviceProperty`** (it wakes the
   > DSP subsystem and may block for 300-400 seconds). For the full command + the SoC↔htp_version↔soc_model mapping table
   > → [`references/win_qairt_setup.md` § Platform SoC Identification](references/win_qairt_setup.md)
   >
   > If the user does not specify and the device cannot be queried, default to `v73` (safe for all supported WoS devices).

   ```bat
   REM FP16, default HTP v73, CROSS-PLATFORM DLC (default)
   <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
     --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
     --output ${WORKSPACE}\<model_name>\output ^
     --precision fp16

   REM FP16, HTP v81, CROSS-PLATFORM DLC (default)
   <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
     --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
     --output ${WORKSPACE}\<model_name>\output ^
     --precision fp16 ^
     --htp_version v81

   REM FP32
   <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
     --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
     --output ${WORKSPACE}\<model_name>\output ^
     --precision fp32

   REM SoC-optimised DLC (only when user explicitly asked for it, see Pre-Conversion Question)
   <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
     --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
     --output ${WORKSPACE}\<model_name>\output ^
     --precision fp16 ^
     --htp_version v73 ^
     --soc_optimized
   ```

   > ℹ️ `run_pipeline.py` reads `${APP_ROOT}\data\config\qairt_env.json`, resolves the QAIRT SDK path, invokes `qairt-converter` → optional `qairt-quantizer` → `qai_dev_gen_contextbin.py --model <file>.dlc`, and copies HTP runtime files automatically. **No VS ARM64 env needed** — the DLC pipeline skips the ARM64 `.dll` compile step entirely. `.bat` wrappers are unnecessary.
   > ℹ️ For manual/debug steps → `references/qnn_conversion.md`. For the legacy DLL-based pipeline (when explicitly requested) → `run_pipeline_legacy.py` at the same script path.

   **Output naming convention:** Context binary is named `{model_stem}_{precision}.bin`
   (e.g. `inception_v3_fp16.bin`, `inception_v3_int8.bin`).

   For the full `run_pipeline.py` argument table (`--precision`/`--act_bw`/`--weight_bw`/`--bias_bw`/`--input_dim`/
   `--config`/`--htp_version`/`--skip_contextbin`/`--no_simplification`, plus the new DLC-path flags
   `--cle`/`--per_channel`/`--per_row`/`--dump_encoding`/`--calib_method`/`--soc_optimized`/`--strip_quant`/`--io_config`/`--quant_overrides`) + the Precision→bitwidth
   internal mapping + more command examples → [`references/qnn_conversion.md`](references/qnn_conversion.md)
   (the Full Argument Reference under § End-to-End Pipeline); the Script Index is at the end of this file.

5. **Optional: Quantization** (INT8/A16W8/A8W8B8)

   **Pre-quantization checklist (MANDATORY before running quantization):**
   - Verify `CALIBRATION_DATA` source exists and is accessible
   - **If no calibration data, ask the user via the `question` tool** — 3 options:
     (1) user provides/uploads their own dataset; (2) Agent auto-prepares; (3) run with
     synthetic random data to get the pipeline working (accuracy may be low — re-quantize
     with real data later). For (2), try in order: project `samples\images\` → workspace
     `${WORKSPACE}\` → a user-given path → web download → synthetic.
     ⚠️ Scan ONLY those explicit dirs with a sample-count cap — never recursive/whole-disk glob.
   - If source is images: convert ALL valid samples to float32 `.raw` format matching model input shape
   - If source is raw folder: validate entries match expected tensor shape
   - Generate `CALIB_LIST` (one absolute `.raw` path per line)
   - Record dataset source, path, and sample count in workspace log
   - Recommended sample count: 50-200 representative samples
   - ⚠️ Calibration data must be REAL, multi-class/multi-scene samples — single-image
     augmentations (crop/flip/brightness) barely help. Acquisition strategies + synthetic
     template → [`references/model_quantization.md`](references/model_quantization.md) § Calibration Data Acquisition.

   ```bat
   REM W8A8
   <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
     --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
     --output ${WORKSPACE}\<model_name>\output ^
     --precision w8a8 ^
     --calib_list ${WORKSPACE}\<model_name>\calib\calibration_list.txt

   REM W8A16
   <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
     --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
     --output ${WORKSPACE}\<model_name>\output ^
     --precision w8a16 ^
     --calib_list ${WORKSPACE}\<model_name>\calib\calibration_list.txt
   ```

   Calibration list format (**raw file paths, one per line — no `input:=` prefix**; that legacy prefix was for `qnn-onnx-converter` and is not accepted by `qairt-quantizer`):
   ```
   ${WORKSPACE}\<model_name>\calib\sample_0001.raw
   ${WORKSPACE}\<model_name>\calib\sample_0002.raw
   ```

   For details → [`references/model_quantization.md`](references/model_quantization.md)

6. **Context binary generation**
   - **`.bin` vs `.dlc` one-line decision**: same machine (host == inference target) → `.bin` (fastest p50); cross-device deployment
     (build once for many devices / possibly different HTP versions) → `.dlc` (`.bin` is locked to a single HTP version + host arch, not portable;
     `.dlc` is compiled on the device at first load); user explicitly names `.bin`/`.dlc` → do as asked. For the full decision tree →
     [`references/inference.md`](references/inference.md) § "Format selection".
   - `run_pipeline.py` generates the `.bin` automatically as the final step (Step 3: DLC → context binary via `qai_dev_gen_contextbin.py --model <file>.dlc`). No extra command needed.
   - **Legacy DLL→bin path (Flow C) — retained, use only when explicitly requested** (see Flow Selection Guide above). Route through `run_pipeline_legacy.py` for automation, or call `qai_dev_gen_contextbin.py --model <file>.dll` directly for a hand-crafted DLL. **Under the hood** — for the DLC path (default), the script auto-detects `.dlc` input and internally invokes the generator with `--model QnnModelDlc.dll --dlc_path <file>.dlc --soc_model <id>`, copies the extra DLC runtime DLLs (QnnModelDlc.dll / QnnHtpV{73,81}Stub.dll / QnnHtpPrepare.dll / QnnHtpNetRunExtensions.dll), and maps htp_version→soc_model (v73→60, v81→88). **Do NOT** pass a `.dlc` to the generator's `--model` directly (it tries to LoadLibrary the `.dlc` as a DLL → "load library failed" / Windows "bad image 0xc000012f"). For DLC inputs the script skips `--config_file` and uses `--soc_model` instead (a config_file would require a predictable `graph_names`, which DLC does not have). See [`references/context_binary.md`](references/context_binary.md#snpedlc-context-binary-generation)
   - **Quick validation without bin:** `QNNContext` can load `.dlc` directly (no bin needed; results numerically identical to `.bin`, ~21-27% slower p50).
   - For manual steps, HTP runtime files, backend config, graph_names rules -> [`references/context_binary.md`](references/context_binary.md)

7. **Inference + validation**

   > 🚨 **MANDATORY — USE WRAPPER ONLY:**
   > - ✅ **Always use `qai_runner.py`** (preferred) **or `qai_appbuilder` directly** (WoS ARM64).
   > - ❌ **NEVER call `qnn-net-run` directly.** `qai_runner.py` handles input/output tensors, NCHW/NHWC format, and result post-processing automatically. Using `qnn-net-run` bypasses all of this and produces unusable raw output.

   - Use `python_arm64_venv` Python (ARM64 3.13)
   - Use `qai_runner.py` wrapper OR `qai_appbuilder` directly (WoS ARM64)
   - **CRITICAL**: `qai_convert_fp.py` uses `--preserve_io` by default -> model keeps **NCHW** input format. Always check `model.getInputShapes()` before preparing input. Wrong format -> completely wrong results.
   - For full API reference, NCHW/NHWC details, templates -> [`references/inference.md`](references/inference.md)

   **MANDATORY: Save inference script to workspace**
   
   After successful inference, the agent MUST save a standalone inference script at:
   `${WORKSPACE}\{MODEL_NAME}\infer_{MODEL_NAME}.py`

   This script is consumed by `qai_pack_export.py` when generating the App Builder runner.
   
   Requirements for `infer_{MODEL_NAME}.py`:
    - Must be a self-contained script (imports `qai_appbuilder` / `QNNContext` for NPU inference; `onnxruntime` allowed for CPU baseline only, see step 8)
   - Must contain clearly separated functions or code blocks for:
     - **Preprocessing**: image/audio/text loading + normalization + format conversion
     - **Inference**: model loading + execution
     - **Postprocessing**: output interpretation (softmax/NMS/decode/etc.)
   - Must actually produce correct output (verified by running it)
   - Must include comments noting:
     - Input shape and format (e.g. `# Input: NCHW float32, shape (1, 3, 299, 299)`)
     - Output shape and interpretation (e.g. `# Output: (1, 1000) logits -> softmax -> Top-K`)
     - Any normalization applied (e.g. `# ImageNet mean/std normalization`)

    > The inference templates at `${APP_ROOT}/factory/chat_features/model-builder/scripts/inference/infer_classify.py`,
    > `infer_detect.py`, `infer_sr.py`, `infer_segment.py` can be used as starting points.
    > Customize for the specific model and save to the workspace.

   **MANDATORY: Generate `inference_manifest.json` after successful inference**

   After inference runs successfully and produces correct results, the agent MUST create:
   `${WORKSPACE}\{MODEL_NAME}\inference_manifest.json`

   This file is consumed by `qai_pack_export.py` to generate a fully working App Builder runner.
   Without it, the exported runner may have incorrect input dimensions or missing label files.

   It records: `model_name` / `precision` / `inference_script` / `context_binary` / `vendor`,
   an `input` block (`shape` / `format` NCHW|NHWC / `dtype` / `preprocessing`), an `output` block,
   and an `assets[]` list (label/vocab files the runner needs, relative to the workspace root).

   > **`output.type` is the CRITICAL field** — it decides which runner template is generated:
   > `"classification"` → softmax + Top-K; `"detection"` → YOLO-style NMS + boxes;
   > `"super_resolution"` → image upscale with tiling; `"segmentation"` → argmax mask + colorize;
   > others (`"text"` / `"audio"` / `"raw"`) → generic passthrough runner.
   > Allowed values: `"classification"` | `"super_resolution"` | `"detection"` | `"segmentation"` | `"text"` | `"audio"` | `"raw"`.

   Full JSON example + every field description (vendor / preprocessing resize_method / mean·std·scale /
   num_classes / postprocessing / detection-only top-level `postprocessing` thresholds / `assets[]` rules)
   → [`references/pack_export.md`](references/pack_export.md) § 1.

8. **Validation report (Phase 6 — MANDATORY after successful inference)**
   - **Must execute in batch mode** — do NOT stop after inference succeeds
   - **ONNX baseline comparison (MANDATORY):**
     1. Run ONNX inference using the same input image/data used in step 7 — use `onnxruntime` with **`CPUExecutionProvider` only** (CPU-only baseline; never route this baseline onto the NPU)
     2. Run QNN inference on the SAME input (already done in step 7 — reuse output)
     3. Compute cosine similarity between ONNX output tensor and QNN output tensor
     4. Example comparison code pattern:
        ```python
        import numpy as np
        cosine = np.dot(onnx_out.flatten(), qnn_out.flatten()) / (
            np.linalg.norm(onnx_out) * np.linalg.norm(qnn_out))
        ```
     5. Threshold: cosine >= 0.99 (FP16/FP32) or >= 0.95 (INT8/A16W8)
     6. **If cosine < threshold → Blocking Condition B6.** Do NOT auto-apply fixes.
        Run zero-cost diagnosis (e.g. calibration diversity check), then STOP and present
        the fix options to the user (calibration diversity / CLE / W8A16 / keep FP16 / accept).

        > 🧭 **Full B6 diagnosis flow, the 5 fix options with one-line principles each,
        > Large-Dynamic-Range channel-collapse trap, and calibration-data acquisition rules
   - **Task-specific accuracy, latency benchmark, and regression test** (per-task metric
     thresholds — Top-1 / mAP / PSNR·SSIM / WER / BLEU / mIoU; cold·p50·p95·throughput·peak-mem;
     >= 3 known-good regression inputs) → [`references/expected_output_artifacts.md`](references/expected_output_artifacts.md) § Validation Report.
   - Write `REPORT.md` in the project workdir with:
     - Cosine similarity score (ONNX vs QNN/SNPE)

       🚨 **MANDATORY FORMAT — read carefully, missing this triggers a Promote-to-AppBuilder warning**

       You **MUST** include a "Cosine Similarity Summary" section in `REPORT.md` containing **one plain-text line per precision variant** in **exactly** this format (no Markdown tables, no bold, no emoji on the value line):

       ```
       Cosine Similarity (ONNX vs <variant>): <value>
       ```

       Concrete required example (copy this section verbatim, replacing values):

       ```markdown
       ## Cosine Similarity Summary

       Cosine Similarity (ONNX vs FP16): 0.999988
       Cosine Similarity (ONNX vs W8A8): 0.934705
       ```

       Rules:
       1. **One line per variant**, starting with the literal text `Cosine Similarity (ONNX vs `.
       2. `<variant>` MUST be one of: `FP16`, `FP32`, `INT8`, `W8A8`, `W8A16`, `W4A16`, `W4A8`, `W8A8B8`, `A16W8`.
       3. `<value>` MUST be a decimal number (e.g., `0.999988`, not `99.9988%`).
        4. **Always include the FP16 line even if you only converted INT/W8A8** — `qai_pack_export.py` reads the matching variant for the `--precision` flag passed in.
       5. You **MAY** additionally include a Markdown comparison table elsewhere in the report (the parser also tolerates `| FP16 | 0.999988 | ... |` style rows as a fallback), but the plain-text "Cosine Similarity Summary" above is what guarantees no warning.

       If `REPORT.md` is missing this format, the exported Pack shows the warning:
       > Model accuracy validation not passed — REPORT.md does not contain valid Cosine Similarity values.

       Re-export after fixing `REPORT.md` will clear it.

     - Pass/fail verdict
     - Top predictions (classification) or sample outputs (other tasks)
   - **Update `plan.md` (MANDATORY — it is the agent's session work log):** each time a phase completes, immediately update that phase's Progress Summary (⬜ → ✅) and record Issue/Operator patch status; at the end of Phase 6 fill `END_TIME` (current timestamp) + `WORK_TIME` (END_TIME minus START_TIME) and mark all completed phases Done. Keep it updated so context can be restored across sessions.

### Expected Output Artifacts

For the list of artifacts the workspace should contain after a full pipeline run (given separately for Flow A/B/C) →
[`references/expected_output_artifacts.md`](references/expected_output_artifacts.md).
**Use it as a per-phase completion checklist**: against the list, confirm that each phase's `.onnx`/`.dlc`/`.bin`/
`infer_<model>.py`/`REPORT.md` were all generated before moving to the next phase.

---

## WoS ARM64 End-to-End Pipeline (QAIRT 2.45+)

A single `run_pipeline.py --precision fp16` completes the end-to-end conversion (**Flow A**, DLC-based; command examples are in Core Workflow Step 4 above);
for the full 3-step flow / manual debug steps → [`references/qnn_conversion.md`](references/qnn_conversion.md) § End-to-End Pipeline.
For multi-precision/multi-size batch runs, use the `${APP_ROOT}/factory/chat_features/model-builder/scripts/model_config.json` config.

**Need the legacy DLL pipeline (Flow C)?** Use `run_pipeline_legacy.py` at the same script path — identical CLI, but runs the old `ONNX → C++/BIN → DLL → .bin` path (requires VS ARM64 env). Only invoke it when the user explicitly asks for the DLL pipeline or specifically needs the `.dll` artifact.

> Backend config files (`backend_extensions.json`, `htp_backend_config_v73.json`) → see [`references/context_binary.md`](references/context_binary.md#backend-config-files-qairt-245-wos-v73)

---

## Project Configuration Variables

> These variables are set per-project in the workspace. Key variables that drive the pipeline:

| Variable | Purpose | Example | Derivation |
|----------|---------|---------|-----------|
| `MODEL_NAME` | Model identifier | `inception_v3` | User-specified |
| `FLOW` | Conversion framework | `QNN` or `SNPE` | User-specified |
| `PRECISION` | Target precision | `FP16`, `INT8`, `A16W8` | User-specified |
| `HOST_DEVICE` | Build machine type | `ARM WIN` | Auto-detected or user-specified |
| `TARGET_DEVICE` | Inference device | `ARM WIN` | User-specified |
| `HOST_ARCH` | Toolchain arch for compilation | `x86_64-windows-msvc` | Derived: ARM WIN → x86_64-windows-msvc |
| `TARGET_ARCH` | Target binary arch | `windows-aarch64` | Derived: ARM WIN → windows-aarch64 |
| `OUTPUT_DIR` | Absolute output path | `${WORKSPACE}\inception_v3\output` | Must be under `${WORKSPACE}\` |
| `CALIBRATION_DATA` | Quantization calibration source | image folder / raw folder / list file | Required for INT/A16W8 only |
| `RETMOE_DEVICE_INFO` | Remote device SSH info file | (optional) | For remote inference/validation |
| `MODE` | Execution mode | `batch` (default) or `interactive` | Controls autonomous vs interactive behavior |

**Architecture derivation rules:**
- `ARM WIN` → HOST_ARCH: `x86_64-windows-msvc`, TARGET_ARCH: `windows-aarch64`, SHELL: `powershell`
- `X86 LINUX` → HOST_ARCH: `x86_64-linux-clang`, TARGET_ARCH: `x86_64-linux-clang`, SHELL: `bash`
- `ARM LINUX` → HOST_ARCH: `aarch64-linux-gcc`, TARGET_ARCH: `aarch64-ubuntu-gcc9.4`, SHELL: `bash`

---

## Remote Device Execution (Optional)

When `RETMOE_DEVICE_INFO` (configured in `plan.md`) is enabled, context binary generation / inference / validation are executed on the
remote target device over SSH instead of locally. **Core rules**: ① `MODE=batch` + `RETMOE_DEVICE_INFO` is set
→ you MUST complete the remote deploy + inference + log collection before finishing (do not stop at local artifacts); ② remote unreachable
→ Blocking Condition **B5**, stop and ask the user; ③ use absolute paths on the remote device.

For the full `RETMOE_DEVICE_INFO` file format + SSH execution template → [`references/remote_execution.md`](references/remote_execution.md)

## Reference Map

Open only what you need:

| Topic | File |
|-------|------|
| Environment setup (Windows) | `references/win_qairt_setup.md` |
| Export + ONNX validation | `references/model_export_validation.md` |
| **Operator patching** | **`references/operator_patching.md`** |
| QNN conversion | `references/qnn_conversion.md` |
| SNPE conversion | `references/snpe_conversion.md` |
| Quantization | `references/model_quantization.md` |
| Context binary | `references/context_binary.md` |
| Inference | `references/inference.md` |
| Troubleshooting | `references/troubleshooting.md` |
| Quantization sensitivity (pre-conversion risk pre-flight) | `references/quantization-sensitivity.md` |
| Verification discipline (cheap-falsify-first / artifact-is-truth / host≠device) | `references/verification-discipline.md` |
| Pack Export & inference_manifest.json | `references/pack_export.md` |

> ### 🧭 子 SKILL（按需加载，见顶部「Problem Routing Index」）
>
> 遇到具体报错/精度/性能问题时，优先加载以下独立子 SKILL（比上表 references 更聚焦、更完整）：
>
> | 子 SKILL | 路径 | tier | 何时用 |
> |---|---|---|---|
> | operator-patching | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/operator-patching/SKILL.md` | base | 算子不支持/验证失败/dry-run 误报 |
> | conversion-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/conversion-troubleshooting/SKILL.md` | base | 转换/编译/context binary 非算子错误 |
> | inference-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/inference-troubleshooting/SKILL.md` | base | 运行时崩溃/stale/多模型/transport |
> | env-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/env-troubleshooting/SKILL.md` | base | 环境/依赖/VS/import 故障 |
> | sdk-integrity-recovery | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/sdk-integrity-recovery/SKILL.md` | base | SDK 写保护纪律 + 损坏恢复 |
> | export-troubleshooting | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/export-troubleshooting/SKILL.md` | base | ONNX 导出期兼容问题 |

## Script Index

> ✅ **The usage of ALL scripts below is fully documented in SKILL.md and `references/*.md`.**
> **Do NOT read script source files** — the reference docs are the authoritative usage guide.
> Only read a script if a specific parameter or behavior is genuinely absent from all reference docs,
> and update the relevant doc afterward so future tasks do not need to read the script again.

| Script | Purpose | Python env | Usage documented in |
|--------|---------|-----------|---------------------|
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_runner.py` | ONNX/QNN wrapper loader for inference/validation | ARM64 | `references/inference.md` — qai_runner.py Wrapper Usage |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_workspace_init.py` | Init workspace: backup existing dir + create fresh `output/` & `calib/` + copy plan.md | x64 | SKILL.md — Working Directory Convention |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_inspect_onnxio.py` | ONNX I/O inspection | x64 | SKILL.md — Core Workflow Step 2 |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_convert_fp.py` | **Legacy** QNN float conversion via `qnn-onnx-converter` (FP16/FP32). Used by `run_pipeline_legacy.py`. For the default DLC path use `run_pipeline.py`. | x64 | `references/qnn_conversion.md` — Float conversion (legacy section) |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_convert_int.py` | **Legacy** QNN quantized conversion via `qnn-onnx-converter` (INT8/A16W8/A8W8B8). Used by `run_pipeline_legacy.py`. For the default DLC path use `run_pipeline.py`. | x64 | `references/qnn_conversion.md` — Quantized conversion (legacy section) |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_convert_snpe.py` | SNPE conversion wrapper | x64 | `references/snpe_conversion.md` |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_dev_gen_contextbin.py` | Context binary generation from `.dll` OR `.dlc` (supports `--auto-config`; auto-detects `.dlc` → default Flow A DLC path). Invoked internally by `run_pipeline.py` (DLC input) and `run_pipeline_legacy.py` (DLL input). | x64 | `references/context_binary.md` § SNPE/DLC; for CLE path also `references/model_quantization.md` § Improving W8A8 Accuracy |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/run_pipeline.py` | **Default end-to-end pipeline (Flow A)**: ONNX → DLC → .bin. Reads `${APP_ROOT}\data\config\qairt_env.json`, invokes `qairt-converter` → optional `qairt-quantizer` → `qai_dev_gen_contextbin.py --model <file>.dlc`. No VS ARM64 env required. Supports CLE / per-channel / bf16 / w16a16. **DLC is cross-platform by default**; pass `--soc_optimized` to build a SoC-specific graph. Pure Python — no `.bat` wrapper needed. | x64 | SKILL.md — Core Workflow Step 4 & `references/qnn_conversion.md` — End-to-End Pipeline |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/run_pipeline_legacy.py` | **Legacy pipeline (Flow C)**: ONNX → C++/BIN → DLL → .bin. Retained for the rare case where the user explicitly requests the DLL path or needs the `.dll` artifact. Runs `qnn-onnx-converter` + `qnn-model-lib-generator` (requires VS ARM64 env, initializes internally via `cmd /c vcvarsall.bat arm64 && set`) + `qai_dev_gen_contextbin.py --model <file>.dll`. | x64 | SKILL.md — Core Workflow Step 4 (legacy note) |
| `${APP_ROOT}/factory/chat_features/model-builder/scripts/model_config.json` | Multi-precision/multi-size model config template | — | SKILL.md — WoS ARM64 End-to-End Pipeline |

### Inference Templates (`${APP_ROOT}/factory/chat_features/model-builder/scripts/inference/`)

> ⚠️ These are **reference templates**, not final scripts. Always inspect model I/O first
> with `infer_generic.py`, then customize the appropriate template for the specific model.
> **Do NOT read these script files** — their usage, arguments, and call patterns are fully
> described in `references/inference.md` — Using Inference Templates.

| Script | Model Type | Notes | Usage documented in |
|--------|-----------|-------|---------------------|
| `scripts/inference/infer_generic.py` | Any model | Start here — prints I/O shapes/dtypes, raw output | `references/inference.md` — Inference Script Templates |
| `scripts/inference/infer_classify.py` | Image classification | Softmax output, Top-K labels | `references/inference.md` — Inference Script Templates |
| `scripts/inference/infer_detect.py` | Object detection (YOLO/SSD) | YOLO output format, NMS | `references/inference.md` — Inference Script Templates |
| `scripts/inference/infer_segment.py` | Semantic segmentation | Mask overlay, color palette | `references/inference.md` — Inference Script Templates |
| `scripts/inference/infer_sr.py` | Super-resolution | Image upscaling, auto-detects NCHW/NHWC I/O | `references/inference.md` — Inference Script Templates |

> ⚠️ **Always use the wrapper scripts** (`qai_runner.py` for inference, `qai_convert_fp.py`/`qai_convert_int.py` for conversion) — never call `qnn-net-run` / `qnn-onnx-converter` / `qnn-model-lib-generator` directly (rules → Step 4 & Step 7; tool-path arch rules → "Tool Path Rules" above).

---

## Pack Export (Phase 7 · App Builder Integration)

> After Phase 6 validation passes (cosine meets threshold, `REPORT.md` written with `END_TIME`,
> `inference_manifest.json` present), optionally export a ready-to-import App Builder Pack candidate.
> Or use the "Promote to App Builder" button in the UI (auto-triggers export).

```bat
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_export.py ^
  --workdir ${WORKSPACE}\{MODEL_NAME} ^
  --model-name {MODEL_NAME} ^
  --precision {PRECISION}
```

> All other parameters (category, display name, input/output kinds) are auto-inferred from
> `inference_manifest.json` and model name.

For the full flow (When-to-Use gate / output `app_pack/` artifact list / `qai_pack_validate.py` validation /
Import to App Builder / Pack Export Script Index) → [`references/pack_export.md`](references/pack_export.md) § 2.

---

## ⚠️ CRITICAL: Python Environment Management (WoS ARM64)

Two separate Python environments — **not interchangeable**. Both paths come from `${APP_ROOT}\data\config\qairt_env.json` (auto-generated by `Setup.bat`). **Never hardcode paths.**

| Environment | Key in `${APP_ROOT}\data\config\qairt_env.json` | Python | Role |
|-------------|-------------------------------|--------|------|
| Conversion env | `python_x64_venv` | x86_64 3.10 | ONNX export, `qnn-onnx-converter`, `qnn-model-lib-generator` |
| Inference env | `python_arm64_venv` | ARM64 3.13 | `qai_appbuilder`, `QNNContext` inference |

> ### ⚠️ Default rule for tools NOT described in SKILL.md
>
> **If a tool or script is not explicitly listed in SKILL.md and it is unclear which env to use:**
> 1. **Default to `python_x64_venv`** (x86_64 3.10) — the conversion env is the safe default because
>    most QAIRT-adjacent tools (converters, builders, exporters) are compiled against `python310.dll`
>    and will fail with `ImportError: ... conflicts with this version of Python` under ARM64 3.13.
> 2. **Only use `python_arm64_venv`** when the tool explicitly imports `qai_appbuilder` / `QNNContext`
>    OR the task is purely running inference on a pre-built `.bin` / `.dlc`.
> 3. **If still uncertain → trigger B10** (stop and ask the user). Never silently guess.
>
> **Real example:** `GenAIBuilderFactory` / `libPyNetRun.pyd` links `python310.dll` → must use
> `python_x64_venv`. Running it under `python_arm64_venv` causes
> `ImportError: Module use of python310.dll conflicts with this version of Python`.

**Setup**: Run `Setup.bat` (generates `${APP_ROOT}\data\config\qairt_env.json`)

**If either env is missing:**
```
python_x64_venv missing → Run: Setup.bat
python_arm64_venv missing → Run: Setup.bat
Both scripts must be run from the QAIModelBuilder project directory.
```

> ℹ️ **Verify `qai_appbuilder` install** with an import check: `<python_arm64_venv>\Scripts\python.exe -c "import qai_appbuilder; print('OK')"`

> ℹ️ **`qai_appbuilder` offline installation** — if not available on PyPI, install from local vendor wheel:
> ```bat
> data\bin\uv\uv.exe pip install vendor\whl\qai_appbuilder-*.whl
> ```

> ℹ️ **`PYTHONPATH` must include QAIRT SDK Python modules** when running `qnn-onnx-converter` directly
> (the `qai_convert_fp.py` wrapper sets this automatically from `${APP_ROOT}\data\config\qairt_env.json`):
> ```bat
> REM Read qairt_sdk_root from ${APP_ROOT}\data\config\qairt_env.json, then:
> set PYTHONPATH=<qairt_sdk_root>\lib\python
> ```

**PyTorch / torchvision — `--index-url` rules:**

| Env | Package | Command |
|-----|---------|---------|
| `python_x64_venv` (conversion) | torch/torchvision | `<python_x64_venv>\Scripts\python.exe -m pip install torch torchvision` — **no `--index-url`** (win_amd64 wheels on PyPI) |
| `python_arm64_venv` (inference) | torch/torchvision | `data\bin\uv\uv.exe pip install torch torchvision --index-url https://download.pytorch.org/whl` — **MUST use `--index-url`** |
| `python_arm64_venv` (inference) | All other packages | `data\bin\uv\uv.exe pip install <pkg>` — **no `--index-url`** |

> ⚠️ Using `--index-url` when not needed will break other package installations.

**Common inference deps in the ARM64 venv (`python_arm64_venv`):**

| Package | Status on a properly-installed machine | Notes |
|---------|----------------------------------------|-------|
| `numpy` | Pre-installed by `Setup.bat` (from `vendor\whl\`) | Do not reinstall. |
| `opencv-python-headless` (`import cv2`) | Pre-installed by `Setup.bat` (from `vendor\whl\`) | **Headless** build (no GUI). `import cv2` works; `cv2.imshow` does not. **Do NOT install `opencv-python` (the GUI build) — it conflicts with the headless one.** |
| `Pillow` (`from PIL import Image`) | Installed via the editable `pip install -e .` | Official `win_arm64` cp313 wheels on PyPI. |
| `torch` / `torchvision` | **Not pre-installed** — see `--index-url` rule above | ARM64 torch is large; install only if truly needed for inference. |

> If `import cv2` / `from PIL import Image` fails on a machine, it almost always
> means `Setup.bat` did not complete (the ARM64 wheel pre-install in Step 4a was
> skipped or interrupted). Re-run `Setup.bat` rather than `pip install opencv-python`
> (the GUI build), which would introduce a conflicting second OpenCV.

For full environment setup details → [`references/win_qairt_setup.md`](references/win_qairt_setup.md)

> 🧭 **When the environment is broken** (VCTargetsPath / No CMAKE_C_COMPILER / `import cv2`·Pillow
> failures / `qai_appbuilder` import errors / wrong-venv `python310.dll` conflict → B10) →
> load the `env-troubleshooting` sub-SKILL for the diagnosis + fix decision tree:
> `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/env-troubleshooting/SKILL.md`
> (this section is the *normal-run* env config; the sub-SKILL is the *failure* handbook).
