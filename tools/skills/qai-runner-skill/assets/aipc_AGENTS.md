# AIPC Project Agents

> **How to Use This Document**
> This document defines **agent roles and workflows** (WHO and HOW).
> For the project config and state tracking, see `../assets/aipc_plan.md`.
> For technical deep dives, see `../references/*.md`.
> **Reading order**: `SKILL.md` → `../assets/aipc_plan.md` → this file → `../references/*.md`

> **Template**: Fill in `../assets/aipc_plan.md` Config block first. All `{VARIABLE}` references in this document resolve from those values.


{PROJECT_NAME},{MODEL_NAME}  are set from `../assets/aipc_plan.md` Config.
---

## Project: {PROJECT_NAME}

**Model**: {MODEL_NAME}  
**Target Platform**: Qualcomm AI PC / Edge Device  
**Target Architecture**: `{TARGET_ARCH}`  
**Precision Goal**: `{PRECISION}`  
**Conversion Flow**: `{FLOW}`  
**Execution Mode**: `{MODE}`

---

## Execution Mode: Batch vs Interactive

> **Read `{MODE}` from `aipc_plan.md` Config before starting any task.**

### `batch` mode (default)
**Default intent**: do all work end-to-end. Agents must continue through all remaining applicable phases without waiting for extra prompts.

Agents execute the full pipeline **autonomously** without asking for confirmation at each step.

**In batch mode, agents MUST**:
- Proceed through all phases without pausing for user confirmation
- Continue beyond local artifact generation when deployment/inference/validation phases remain
- If `RETMOE_DEVICE_INFO` is present, execute remote deploy + remote inference + log collection before final response
- Treat host-only inference as interim validation only when `RETMOE_DEVICE_INFO` is present; final acceptance requires target execution
- When `RETMOE_DEVICE_INFO` is present, skip local quick-smoke inference before remote target inference
- Apply safe defaults for any unspecified optional parameters
- Log every decision and assumption in `aipc_plan.md` Issue Log
- Mark each phase ✅ Done in Progress Summary upon completion
- **Only stop** when a Blocking Condition is encountered (see below)

**In batch mode, agents MUST NOT**:
- Ask "should I proceed to the next phase?"
- Ask "which precision should I use?" (use `{PRECISION}` from Config)
- Ask "should I run onnxsim?" (always run it)
- Ask "should I simplify the model?" (always simplify)
- Pause for routine confirmations that can be resolved from Config values
- Run local quick-smoke inference before required remote target inference when `RETMOE_DEVICE_INFO` is set

### `interactive` mode
Agents ask the user for confirmation at each phase transition and before key decisions.

**In interactive mode, agents ask before**:
- Starting each new phase
- Applying operator patches
- Choosing between optional steps (e.g., onnxsim, context binary)
- Proceeding after a warning (non-blocking)

---

## Blocking Conditions (Always Stop — Both Modes)

> These conditions **always** require stopping and asking the user, regardless of `{MODE}`.

| # | Condition | Action |
|---|-----------|--------|
| B1 | Required Config variable is empty or `<!--...-->` | Stop. List missing variables. Ask user to fill them in. |
| B2 | `pip install` is needed | Stop. State the package and reason. Ask user for permission. |
| B3 | Unlimited patch iterations exhausted with NO progress (same ops failing, no replacement patterns available) | Stop. List all attempted patches, logs. Escalate to user. |
| B4 | Operator patch would change model semantics (e.g., replace attention with different behavior) | Stop. Describe the change. Ask user to approve. |
| B5 | Target device is unavailable for context binary generation or on-device testing | Stop. Ask user how to proceed. |
| B6 | Accuracy drops below threshold after quantization (cosine < 0.95) | Stop. Report metrics. Ask user whether to accept or retry. |
| B7 | No known replacement pattern exists for unsupported operator | Stop. Document operator, escalate to user. |
| **B8** | **Context binary generation fails on Windows ARM** | **Record issue and continue with non-context `.dll` path if needed. Escalate only if B3/B4/B7 met.** |

### ⚠️ CRITICAL: Operator Patching — Exhaustive Requirement

**Agents MUST continue patching until NO replacements exist:**

| Rule | Description |
|------|-------------|
| **DO NOT stop** at fixed iteration count | Unlimited iterations — continue until all ops resolved |
| **DO NOT stop** because op seems "fundamental" | Replacement patterns may exist for any operator |
| **MUST search** `references/operator_patching.md` for each op | Document if no pattern exists |
| **Escalate ONLY when:** | (a) No replacement pattern (B7), (b) Patch changes semantics (B4) |

### ⚠️ CRITICAL: Context Binary Requirements by Platform

**Agents MUST know:**
| Platform | Context Binary | Can proceed without it? |
|----------|----------------|------------------------|
| **ARM Windows** | **OPTIONAL** — `.dll.bin` | ✅ YES — `.dll` works directly |
| **ARM Linux**   | **OPTIONAL** — `.so.bin` | ✅ YES — `.so` works directly **only after host-context troubleshooting is exhausted** |

**Agents MUST NOT:**
- ❌ Say "Windows always requires context binary"
- ❌ Say "context binary is required on Linux"
- ❌ Block Phase 7 (Inference) on Windows solely because context binary generation failed
- ❌ Stop patching after arbitrary iteration count

**Correct behavior:**
- ✅ **Windows ARM**: Context binary (`.dll.bin`) is **OPTIONAL** — `.dll` direct path is allowed
- ✅ **Linux ARM**: Context binary (`.so.bin`) is **OPTIONAL**, but `.so` fallback is allowed only after all required host-context troubleshooting is completed and logged
- ✅ **If Windows context binary fails**: record logs and continue with `.dll` fallback path
- ✅ **Continue patching** until no replacement patterns exist
- ✅ **Escalate** only when B3/B4/B7 conditions are met

---

## Agent Interaction Flow

```
User Request
     │
     ▼
Orchestrator Agent  ◄─── aipc_plan.md (Config + Progress Summary)
     │  [reads {MODE}: batch → autonomous | interactive → confirm each phase]
     │
     ├──► Model Export Agent ──────────► {ONNX_FILE}          [Plan Phase 1]
     │         │ (if patches needed → loop back)
     │         ▼
     ├──► Model Inspector Agent ────────► {MODEL_NAME}.yaml    [Plan Phase 2]
     │         │ (if issues found → back to Export Agent)
     │         ▼
     │    ┌────────────────────────────────────────────────────┐
     │    │  FLOW = QNN                  FLOW = SNPE           │
     │    ├──► Conversion Agent ──────► lib{MODEL_NAME}.so     │ [QNN-3A]
     │    │         OR                                         │
     │    ├──► Conversion Agent ──────► {MODEL_NAME}.dlc       │ [SNPE-3]
     │    │         OR (if INT)                                │
     │    ├──► Quantization Agent ────► lib{MODEL_NAME}_a16_w8 │ [QNN-3B / SNPE-4]
     │    └────────────────────────────────────────────────────┘
     │         │ (if conversion fails → back to Export Agent)
     │         ▼
     ├──► Context Binary Agent ─────────► {MODEL_NAME}_context.bin  [QNN-4 only]
     │         ▼
     ├──► Inference Agent ───────────────► infer_{MODEL_NAME}.py / results     [QNN-5 / SNPE-5]
     │         ▼
     └──► Validation Agent ──────────────► REPORT.md / Pass/Fail    [Phase 6]
```

---

## Mandatory Agent Rules

> These rules apply to **all agents** in every task execution, regardless of `{MODE}`.

1. **Always use the `aipc-toolkit` skill flow.**  
   Every agent must operate through the `aipc-toolkit` skill (activated via `use_skill`). If the skill flow cannot complete a required step → **Blocking Condition B7**: stop and ask the user.

2. **Do not survey the QAIRT SDK source folder.**  
   Agents must not browse, read, or search inside `$QAIRT_SDK_ROOT` or `$QNN_SDK_ROOT` subtrees. Use only documented CLI tools and Python APIs. If SDK internals are needed → ask the user before action.

3. **Do not derive solutions from existing artifacts in the working folder.**  
   Agents must not use existing `.bin`, `.cpp`, `_net.json`, `.so`, or calibration files as a substitute for running the proper pipeline steps. Each stage must be executed fresh.

4. **Prefer the QAIRT SDK Python venv for all Python execution.**  
   - Activate via `{QAIRT_ENV_SETUP}` before running any Python script.  
   - Do **not** create a project-specific venv unless the QAIRT venv cannot satisfy requirements.  
   - `pip install` always requires user permission → **Blocking Condition B2**.  
   - Record the venv decision in `aipc_plan.md` Config (`python venv` / `python lib install`).

5. **Log all decisions in batch mode.**  
   When `{MODE} = batch`, every autonomous decision (e.g., default parameter chosen, optional step skipped/included) must be recorded in `aipc_plan.md` Issue Log before proceeding.

6. **Before quantization phase, orchestrator MUST.**
   - Verify `CALIBRATION_DATA` exists and is usable (folder/file/list).
   - If `CALIBRATION_DATA` is missing or invalid, search web for a suitable public calibration dataset and download.
   - Generate `CALIB_LIST` via image-to-raw preprocessing when source is images.
   - Use existing `.raw` samples directly when source is raw data.
   - Record dataset source/path and sample count in `aipc_plan.md`.

---

## Agent Roster

### 1. Orchestrator Agent

**Role**: Plans and coordinates the end-to-end AIPC workflow. Delegates tasks to specialist agents and tracks overall progress.

**Responsibilities**:
- Read `aipc_plan.md` Config block and confirm all required variables are filled in (check for Blocking Condition B1)
- Determine `{FLOW}` and `{MODE}` and sequence the correct pipeline phases
- Delegate to specialist agents in phase order; verify exit criteria before proceeding
- Update `aipc_plan.md` Progress Summary after each phase completes
- Log all decisions and blockers in `aipc_plan.md` Issue Log

**Key Decisions**:
- Confirm `{FLOW}` = QNN or SNPE
- Confirm `{PRECISION}` = FP16 / FP32 / INT8 / A16W8
- Whether operator patching is required (`PATCH_NEEDED`)
- Python environment: use `{QAIRT_ENV_SETUP}` venv by default

**Workflow**:
1. Read `aipc_plan.md` Config — check all required variables are set (B1 if missing)
2. Read `{MODE}`: set autonomous execution if `batch`, confirmation-per-phase if `interactive`
3. Run environment setup: `source {QAIRT_ENV_SETUP}` (bash) or `. "{QAIRT_ENV_SETUP}"` (PowerShell)
4. Verify toolchain (see Environment Setup Checklist)
5. Delegate Phase 1 → Model Export Agent
6. Delegate Phase 2 → Model Inspector Agent
7. Based on `{FLOW}` and `{PRECISION}`:
   - **QNN + FP**: delegate QNN-3A → Conversion Agent
   - **QNN + INT**: delegate QNN-3B → Quantization Agent
   - **SNPE + FP**: delegate SNPE-3 → Conversion Agent
   - **SNPE + INT**: delegate SNPE-4 → Quantization Agent
8. If `{FLOW}` = QNN and context binary needed: delegate QNN-4 → Context Binary Agent
9. Delegate QNN-5 or SNPE-5 → Inference Agent
10. Delegate Phase 6 → Validation Agent
11. After each phase: verify exit criteria, update Progress Summary, log decisions

**Batch mode note**: Steps 5–11 execute without pausing for user confirmation unless a Blocking Condition is hit.

**Tools / Skills**:
- `aipc-toolkit` skill (primary — activate via `use_skill`)
- `aipc_plan.md` Config + Progress Summary + Issue Log

---

### 2. Model Export Agent

**Role**: Exports the source model ({SRC_FRAMEWORK}) to ONNX format, applying in-memory patches for unsupported operators.

**Responsibilities**:
- Write a dedicated `export_onnx.py` script (never rely solely on CLI)
- Apply safe operator patching for QNN-incompatible ops (e.g., `Einsum`, custom attention)
- Set `opset_version` = `{OPSET}` (prefer 13–17)
- Verify with `onnx.checker.check_model()` and run `onnxsim`

**Inputs**:
- Source model weights; `PATCH_NEEDED`, `PATCH_OPS` from `aipc_plan.md` Prerequisites

**Outputs**:
- `{ONNX_FILE}` — validated ONNX file

**Workflow** (`aipc_plan.md` Phase 1):
1. Check `PATCH_NEEDED` — if Yes, identify ops from `PATCH_OPS`; if patch changes semantics → **B4**
2. Write and run `export_onnx.py`:
   ```python
   # export_onnx.py
   import torch, onnx
   from onnxsim import simplify

   model = ...  # load {MODEL_NAME}
   patch_model_for_qnn(model)  # if PATCH_NEEDED = Yes

   torch.onnx.export(model, dummy_input, "{ONNX_FILE}",
                     opset_version={OPSET},
                     input_names=["{INPUT_NAME}"],
                     output_names=["{OUTPUT_NAMES}"],
                     dynamic_axes={...})

   model_onnx = onnx.load("{ONNX_FILE}")
   onnx.checker.check_model(model_onnx)
   model_simplified, check = simplify(model_onnx)
   onnx.save(model_simplified, "{ONNX_FILE}")
   ```
3. Verify numerical parity vs {SRC_FRAMEWORK} baseline
4. Update `aipc_plan.md` Phase 1 checkboxes → hand off to Model Inspector Agent

**Batch mode**: steps 1–4 execute autonomously. Log patch decisions in Issue Log.

**Iterative Patching — EXHAUSTIVE Requirement:**
Patches may expose previously-hidden unsupported ops. After each patch:
- Re-run dry-run to detect remaining unsupported ops
- If new ops found → repeat patching
- **DO NOT stop** at arbitrary iteration count
- Track all patched ops in `PATCH_OPS` (comma-separated)
- **Continue until** no replacement patterns exist
- **Escalate only when:**
  - No replacement pattern exists (B7)
  - Patch changes semantics (B4)
  - 7+ iterations with NO progress (B3)

**Verification** (Phase 1 exit criteria):
- [ ] `onnx.checker.check_model()` passes
- [ ] Inference output matches {SRC_FRAMEWORK} baseline
- [ ] No unsupported operators (confirmed by inspector)

**Reference**: `skills/aipc-toolkit/references/model_export_validation.md`

---

### 3. Model Inspector Agent

**Role**: Inspects `{ONNX_FILE}` I/O shapes, data types, and operator compatibility before conversion.

**Inputs**: `{ONNX_FILE}`  
**Outputs**: `{MODEL_NAME}.yaml`, inspection report

**Workflow** (`aipc_plan.md` Phase 2):
1. Run inspection:
   ```bash
   python skills/aipc-toolkit/scripts/aipc_inspect_onnxio.py {ONNX_FILE}
   ```
2. Record `INPUT_NAME`, `INPUT_SHAPE`, `OUTPUT_NAMES` in `aipc_plan.md` Prerequisites
3. Run converter dry-run:
   ```bash
   # Flow A — QNN
   {QAIRT_ROOT}/bin/{HOST_ARCH}/qnn-onnx-converter --input_network {ONNX_FILE} --dry_run

   # Flow B — SNPE
   {QAIRT_ROOT}/bin/{HOST_ARCH}/qairt-converter --input_network {ONNX_FILE} --dry_run
   ```
4. Document issues in `aipc_plan.md` Phase 2 task 2.3
5. If issues found → escalate to Model Export Agent; re-inspect after patching
6. If clean → update Phase 2 checkboxes; hand off to Conversion Agent

**Batch mode**: steps 1–6 execute autonomously. Log all findings in Issue Log.

**Verification** (Phase 2 exit criteria):
- [ ] `{MODEL_NAME}.yaml` generated with correct I/O names and shapes
- [ ] No unsupported operators flagged
- [ ] `INPUT_NAME`, `INPUT_SHAPE`, `OUTPUT_NAMES` recorded in `aipc_plan.md`

**Reference**: `skills/aipc-toolkit/references/model_export_validation.md` §2

---

### 4. Conversion Agent (FP16 / FP32)

**Role**: Converts `{ONNX_FILE}` to QNN or SNPE format at FP16/FP32 precision.

**Inputs**: `{ONNX_FILE}`, `{MODEL_NAME}.yaml`, Config: `{FLOW}`, `{PRECISION}`, `{TARGET_ARCH}`, `{OUTPUT_DIR}`  
**Outputs**:
- **Flow A (QNN)**: `{MODEL_NAME}.bin/.cpp/_net.json`, `lib{MODEL_NAME}.so`
- **Flow B (SNPE)**: `{MODEL_NAME}.dlc`

**Workflow**:

**Flow A — QNN** (`aipc_plan.md` QNN-3A):
```bash
python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
  --onnx {ONNX_FILE} \
  --output-root {OUTPUT_DIR} \
  --precision {PRECISION} \
  --preserve-io-mode datatype \
  --target-arch {TARGET_ARCH}
```
> If target runtime shows FP16/dtype compatibility issues, retry with `--preserve-io-mode layout`.

**Flow B — SNPE** (`aipc_plan.md` SNPE-3):
```bash
python skills/aipc-toolkit/scripts/aipc_convert_snpe.py \
  --onnx {ONNX_FILE} \
  --output {OUTPUT_DIR}/{MODEL_NAME}.dlc \
  --precision {PRECISION}
```

> If ONNX input is dynamic, add `--source-model-input-shape {INPUT_NAME} {INPUT_SHAPE}`.

**If conversion fails**:
- Attempt 1: identify unsupported op → patch → re-inspect → re-convert
- Attempt 2: retry with adjusted parameters
- After 7 failed attempts → **Blocking Condition B3**: stop and escalate

**Batch mode**: run conversion autonomously; log each attempt in Issue Log. Stop only on B3.

**Verification** (QNN-3A / SNPE-3 exit criteria):
- [ ] Conversion logs show no errors
- [ ] **Flow A (Linux)**: `lib{MODEL_NAME}.so` exists; `file lib{MODEL_NAME}.so` confirms `{TARGET_ARCH}`
- [ ] **Flow A (Windows)**: `{MODEL_NAME}.dll` exists; `dumpbin /headers {MODEL_NAME}.dll | find "machine"` confirms `{TARGET_ARCH}`
- [ ] **Flow B**: `{OUTPUT_DIR}/{MODEL_NAME}.dlc` exists and is non-zero

**Reference**: `skills/aipc-toolkit/references/qnn_conversion.md` (Flow A), `skills/aipc-toolkit/references/snpe_conversion.md` (Flow B)

---

### 5. Quantization Agent (INT8 / A16W8)

**Role**: Quantizes `{ONNX_FILE}` to INT8 or A16W8 using calibration data.

**Inputs**: `{ONNX_FILE}`, `{CALIBRATION_DATA}`, `{CALIB_LIST}`, Config: `{FLOW}`, `{ACT_BITWIDTH}`, `{TARGET_ARCH}`, `{OUTPUT_DIR}`  
**Outputs**:
- **Flow A (QNN)**: `lib{MODEL_NAME}_a16_w8.so` (Linux) / `{MODEL_NAME}_a16_w8.dll` (Windows)
- **Flow B (SNPE)**: `{MODEL_NAME}_quantized.dlc`

**Calibration Data Requirements**:
- Source folder (user-provided): `{CALIBRATION_DATA}` from `aipc_plan.md` Config  
  - Example: COCO128 images folder (for detection models) or your own representative dataset
- Generated calibration inputs: raw float32 binary `.raw` files, shape = `{INPUT_SHAPE}`
- Count: 50–200 representative samples
- `{CALIB_LIST}`: one `.raw` file path per line

**Workflow**:

**Flow A — QNN** (`aipc_plan.md` QNN-3B):
```bash
python skills/aipc-toolkit/scripts/aipc_convert_int.py \
  --input_network {ONNX_FILE} \
  --input_list {CALIB_LIST} \
  --output-root {OUTPUT_DIR} \
  --act_bw {ACT_BITWIDTH} \
  --weight_bw <!-- 8 for INT8/A16W8 typical; see docs/QUANTIZATION_GUIDE.md for other modes --> \
  --preserve-io-mode datatype \
  --target-arch {TARGET_ARCH}
```
> If target runtime shows FP16/dtype compatibility issues, retry with `--preserve-io-mode layout`.

**Flow B — SNPE** (`aipc_plan.md` SNPE-4):
```bash
{QAIRT_ROOT}/bin/{HOST_ARCH}/snpe-dlc-quant \
  --input_dlc {OUTPUT_DIR}/{MODEL_NAME}.dlc \
  --input_list {CALIB_LIST} \
  --output_dlc {OUTPUT_DIR}/{MODEL_NAME}_quantized.dlc \
  --enable_htp
```

After quantization: run quick accuracy check vs FP baseline.  
If cosine similarity < 0.95 → **Blocking Condition B6**: stop and report to user.

**Batch mode**: run quantization autonomously; log accuracy result in Issue Log. Stop only on B6.

**Verification** (QNN-3B / SNPE-4 exit criteria):
- [ ] Quantized artifact exists and is non-zero
- [ ] Cosine similarity vs FP baseline ≥ 0.95

**Reference**: `skills/aipc-toolkit/references/model_quantization.md`, `docs/QUANTIZATION_GUIDE.md`

---

### 6. Context Binary Agent

**Role**: Generates hardware-specific HTP context binaries on the **host** (x86 Linux or ARM Windows) for the target SoC, then deploys to the target device. **Flow A (QNN) only.**

**Key concept**: The host generates a context binary compiled for the **target SoC** using `qnn-context-binary-generator`. The `.bin` can then run on the target device even if host and target architectures differ.

**Inputs**:
- Linux host: `lib{MODEL_NAME}.so` (x86-arch — host cannot load aarch64)
- Windows host: `{MODEL_NAME}.dll` (x86-arch or emulation)
- Config: `{SOC_ID}`, `{DSP_ARCH}` — **mandatory**, identify from target device before generation
- Reference: `skills/aipc-toolkit/references/host_context_binary_gen.md`

> ⚠️ `{SOC_ID}` and `{DSP_ARCH}` are mandatory. Generating without them produces a generic binary that may crash or produce wrong results on the target.
> See `host_context_binary_gen.md` → Step 1 for how to read these values from the target device.

**Outputs**:
- Linux: `lib{MODEL_NAME}.so.bin` (optional — `.so` works directly on Linux)
- Windows: `{MODEL_NAME}.dll.bin` (optional — `.dll` works directly on Windows)

**Workflow** (`aipc_plan.md` QNN-4):


### ⚠️ CRITICAL: Platform-Specific Requirements

| Platform | Context Binary | If generation fails |
|----------|----------------|---------------------|
| **Windows ARM** | **OPTIONAL** | → If generation fails, continue with `.dll` fallback |
| **Linux ARM**   | **OPTIONAL** | → First exhaust `host_context_binary_gen.md` methods, then fallback to `.so` |

**If context binary generation fails on host:**

1. **Identify ALL unsupported operators** from error logs
2. **For EACH operator:**
   - Search `references/operator_patching.md` for replacement pattern
   - If pattern exists → Apply patch → Re-export model → Re-convert → Re-generate context binary → Repeat
   - If NO pattern exists → Document → Escalate (B7)
3. **DO NOT stop** patching because:
   - ❌ "7 iterations reached" (soft guideline only)
   - ❌ "Operator seems fundamental" (Floor, Transpose may have patterns)
4. **Escalate ONLY when:**
   - **B3**: 7+ iterations with NO progress (same ops failing)
   - **B4**: Only patch changes model semantics
   - **B7**: No replacement pattern exists
5. **Windows only**: If all patches exhausted → Suggest SNPE flow alternative
6. **Linux only**: Can proceed to inference with `.so` library directly only after all applicable host-context methods have been attempted and logged:
   - correct `soc_id`/`dsp_arch` confirmation
   - `vtcm_mb` sweep (`0,1,2,3,4,8`)
   - `soc_id`/`dsp_arch` alternatives from mapping
   - `htp_arch` / no-`soc_id` path when applicable

**Batch mode**: 
- **Windows**: Run autonomously on host; if context generation fails, continue with `.dll` fallback
- **Linux**: Run autonomously on host; `.so` fallback allowed only after required host-context troubleshooting is exhausted

**Verification** (QNN-4 exit criteria):
- [ ] **Windows**: `{MODEL_NAME}.dll.bin` generated and deployed OR `.dll` fallback path verified
- [ ] **Linux**: `{MODEL_NAME}.so.bin` generated on host and deployed to target OR all host-context methods attempted+logged before `.so` fallback
- [ ] **If Windows verification fails**: Return to Step 3.5 (operator patching)
- [ ] **Escalate** only when B3/B4/B7 conditions met

**Reference**: `skills/aipc-toolkit/references/host_context_binary_gen.md`

---

### 7. Inference Agent

**Role**: Implements the end-to-end inference pipeline for `{MODEL_NAME}` using the ONNX→QNN/SNPE wrapper.

**Inputs**:
- **Flow A (Linux)**: `lib{MODEL_NAME}.so` OR `lib{MODEL_NAME}.so.bin`; `{MODEL_NAME}.yaml`
- **Flow A (Windows)**: `{MODEL_NAME}.dll.bin` (optional) or `{MODEL_NAME}.dll`; `{MODEL_NAME}.yaml`
- **Flow B**: `{OUTPUT_DIR}/{DLC_FILE}`; `{MODEL_NAME}.yaml`
- `scripts/aipc` and `scripts/onnxwrapper.py` (from skill source)
- **Remote inference (required when provided)**: `{RETMOE_DEVICE_INFO}` file (from `aipc_plan.md`) that records:
  - SSH connection info (host/user/port and key path if needed)
  - Target working directory (where inference is executed)
  - QAIRT setup script path on the target (user-provided; sets env vars / activates venv / initializes QAIRT)

### Remote Inference over SSH (Required When `RETMOE_DEVICE_INFO` Is Set)

If `{RETMOE_DEVICE_INFO}` is provided, inference must be executed on the target device via SSH before task completion.

`{RETMOE_DEVICE_INFO}` must point to a file containing:
- **(a) SSH information**: host/user/port and key path if needed
- **(b) Working folder**: the target directory to `cd` into before running inference
- **(c) Setup script path**: a user-provided QAIRT setup script on the target (sets env vars / activates venv / initializes QAIRT)

Recommended execution pattern (conceptual):

1. `ssh` to the target
2. `cd <working_folder>`
3. `source <setup_script>`
4. run `python aipc ...`

> See `skills/aipc-toolkit/references/inference.md` → "Target Device Inference over SSH" for concrete command examples.

**Before starting inference:**

| Platform | Required File | If missing |
|----------|---------------|------------|
| **Windows ARM** | `{MODEL_NAME}.dll` or `.dll.bin` | → OK — either path can proceed |
| **Linux ARM**   | `{MODEL_NAME}.so` or `.so.bin` | → `.so` is OK only after host-context troubleshooting exhaustion is documented |

1. Verify required files exist:
   - **Windows**: `{MODEL_NAME}.dll` or `{MODEL_NAME}.dll.bin` (either works)
   - **Linux**: `{MODEL_NAME}.so` or `{MODEL_NAME}.so.bin` (either works)
2. **If Windows context binary is MISSING**: continue with `.dll` direct inference path
3. **Linux**: Can proceed with `.so` library directly only after all required host-context troubleshooting methods are completed and logged

**Outputs**: `infer_{MODEL_NAME}.py`, inference results

**Workflow** (`aipc_plan.md` QNN-5 / SNPE-5):

1. **Copy wrapper scripts** into the working folder:
   ```bash
   cp skills/aipc-toolkit/scripts/aipc ./
   cp skills/aipc-toolkit/scripts/onnxwrapper.py ./
   ```

2. **Ensure `QAIRT_SDK_ROOT` is set** (already done via `{QAIRT_ENV_SETUP}`).

3. **Write `infer_{MODEL_NAME}.py`** with preprocessing, inference call, and postprocessing for `{MODEL_NAME}`.

4. **Run inference** via the project wrapper:
   ```bash
   # IMPORTANT: Copy context binary to match ONNX naming (Windows)
   Copy-Item {OUTPUT_DIR}\{MODEL_NAME}.dll.bin .\{MODEL_NAME}.onnx.dll.bin
   
   # Then run inference
   python aipc path/to/onnx_inference.py
   ```
   > The `aipc` wrapper passes the `.onnx` path but searches for a matching QNN binary in the same directory.  
   > See `references/inference.md` → Model File Resolution for full search order.

5. **Windows only**: generate the HTP context binary first (Phase 4) and copy to match ONNX naming.

6. **If I/O names fail**: regenerate `{MODEL_NAME}.yaml` via the inspector:
   ```bash
   python skills/aipc-toolkit/scripts/aipc_inspect_onnxio.py {ONNX_FILE}
   ```

**Batch mode**: execute steps 1–6 autonomously; spot-check outputs vs ONNX baseline; log result in Issue Log.

**Common fixes** (apply autonomously in batch mode):
- Add SDK bins to `PATH`/`LD_LIBRARY_PATH`
- Copy context binary to match ONNX naming: `Copy-Item {OUTPUT_DIR}\{MODEL_NAME}.dll.bin .\{MODEL_NAME}.onnx.dll.bin`
- Use absolute paths for `{MODEL_NAME}_context.bin` and `{DLC_FILE}`
- Regenerate `{MODEL_NAME}.yaml` via inspector if I/O names mismatch

**Verification** (QNN-5 / SNPE-5 exit criteria):
- [ ] Inference runs without errors
- [ ] Input tensor name/shape matches model (from `{MODEL_NAME}.yaml`)
- [ ] Preprocessing matches training/export assumptions
- [ ] Output tensor mapping is correct
- [ ] Output shapes match `{OUTPUT_NAMES}` (from `{MODEL_NAME}.yaml`)
- [ ] Results numerically reasonable vs ONNX CPU baseline (cosine similarity / task metric)
- [ ] Latency/FPS collected on target runtime

**Reference**: `skills/aipc-toolkit/references/inference.md`


---

### 8. Validation & Testing Agent

**Role**: Validates `{MODEL_NAME}` accuracy, performance, and correctness after conversion.

**Inputs**:
- `{ONNX_FILE}` + converted model; test dataset
- `{TARGET_DEVICE}` for on-device performance/latency validation
- **Remote validation (optional)**: `{RETMOE_DEVICE_INFO}` file (from `aipc_plan.md`) that records:
  - **(a) SSH information**: host/user/port and key path if needed
  - **(b) Working folder**: the target directory to `cd` into before running validation
  - **(c) Setup script path**: a user-provided QAIRT setup script on the target (sets env vars / activates venv / initializes QAIRT)

**Outputs**: `REPORT.md`

**Workflow** (`aipc_plan.md` Phase 6):
1. ONNX CPU inference → baseline outputs (Phase 6.1)
2. `{FLOW}` inference on same inputs → compare cosine similarity on `{OUTPUT_NAMES}` (Phase 6.1)
3. Task-specific metric: mAP / Top-1 / WER vs {SRC_FRAMEWORK} baseline (Phase 6.2)
4. Latency benchmark on `{TARGET_DEVICE}`: cold start, p50/p95, throughput, peak memory (Phase 6.3)
5. Regression tests with known-good inputs (Phase 6.4)
6. Write `REPORT.md` (Phase 6.5):
   - Record **task completion time** (`END_TIME = <YYYY-MM-DD HH:MM>`)
   - Compute and record **total work duration** (`WORK_TIME = END_TIME − START_TIME` from `aipc_plan.md` Config)
   - Include both values in `REPORT.md` header and in `aipc_plan.md` Config block
   - Append all user prompts issued during this session and report the total number of user interventions.
   - Write an issue report in `aipc_plan.md` Issue Log summarizing all problems encountered, decisions made, and their resolutions.
   - Update Progress Summary

If cosine similarity < threshold → **Blocking Condition B6**.

**Batch mode**: run all validation steps autonomously; write `REPORT.md`; update Progress Summary. Stop only on B6.

**Validation Criteria**:

| Precision | Cosine Similarity | Top-1 Accuracy Drop |
|-----------|------------------|---------------------|
| FP16/FP32 | ≥ 0.99 | ≤ 1% vs FP32 baseline |
| INT8/A16W8 | ≥ 0.95 | ≤ 1% vs FP32 baseline |

**⚠️ MANDATORY GATE — verify before any completion response:**
```bash
ls REPORT.md   # must exist — if missing, write it NOW before proceeding
```

**Verification** (Phase 6 exit criteria):
- [ ] Cosine similarity meets threshold
- [ ] Task metric within acceptable range
- [ ] Latency meets performance target
- [ ] **`REPORT.md` exists on disk** (`ls REPORT.md` confirms)
- [ ] `END_TIME` and `WORK_TIME` recorded in `REPORT.md` and `aipc_plan.md` Config

---

## Environment Setup Checklist

> Verify before starting. All variables resolve from `aipc_plan.md` Config block.

| Requirement | Variable / Command | Notes |
|---|---|---|
| QAIRT SDK root | `{QAIRT_ROOT}` | Required for all conversion |
| Env setup script | `source {QAIRT_ENV_SETUP}` (bash) / `. "{QAIRT_ENV_SETUP}"` (PS) | Sets `QAIRT_SDK_ROOT`, PATH, venv |
| Host arch | `{HOST_ARCH}` | `x86_64-linux-clang` (Linux) / `arm64x-windows-msvc` (Win) |
| Target arch | `{TARGET_ARCH}` | `aarch64-ubuntu-gcc9.4` / `windows-aarch64` / `x86_64-linux-clang` |
| Python env | QAIRT venv via `{QAIRT_ENV_SETUP}` | Record in `aipc_plan.md` Config (`python venv`) |
| Calibration data | `{CALIBRATION_DATA}` + `{CALIB_LIST}` | Required for INT quantization only |
| Execution mode | `{MODE}` | `batch` = autonomous; `interactive` = confirm each phase |

**Windows architecture detection**: Do **not** use `$env:PROCESSOR_ARCHITECTURE` or Python's `platform.machine()` — both can be affected by emulation. Use:
```powershell
(Get-WmiObject Win32_Processor).Architecture   # 12 = ARM64
# or: dumpbin /headers model.dll | find "machine"
```

---

## Notes & Cautions

- **Do not copy scripts** from `skills/aipc-toolkit/scripts/` to the work folder unless explicitly required.
- **Do not change the QAIRT toolchain** — use workarounds in `SKILL.md`. Changing the toolchain causes pipeline failures.
- **Operator patching**: always patch in-memory. Never modify library source code.
- **Absolute paths**: always use absolute paths for `{MODEL_NAME}_context.bin` and `{DLC_FILE}`.
- **Batch mode decisions**: every autonomous decision must be logged in `aipc_plan.md` Issue Log.
- **Blocking conditions**: see the Blocking Conditions table above. These always require stopping, regardless of `{MODE}`.
