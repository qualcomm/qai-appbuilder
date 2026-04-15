# AIPC Project Plan

> **How to use this template**:
> 1. Fill in the **Variables** section below — each variable is defined **once**.
> 2. Throughout this document, `{VARIABLE}` references are already resolved by your definitions above.
> 3. Choose **Flow A (QNN)** or **Flow B (SNPE)** and follow only that flow's phases.

---

## Config 

> Fill these in once. All `{VAR}` references in this document use these values. If a value is not set, the default shown in its comment applies.

```
PROJECT_NAME  = <!-- your project name -->
MODEL_NAME    = <!-- model identifier, e.g. yolov8n, whisper-tiny, lprnet -->
QAIRT_ENV_SETUP = <!-- path to the project-level env-setup script; sources {QAIRT_ROOT}/bin/envsetup.sh (or .ps1), activates the QAIRT Python venv, and extends PATH / proxy as needed -->

FLOW          = <!-- QNN  or  SNPE(default) -->

SRC_FRAMEWORK = <!-- PyTorch(default) /   ONNX -->
TARGET_DEVICE = <!-- ARM WIN  (QCOM) / x86 Linux/ ARM Linux (QCOM) -->

PRECISION     = <!-- FP32 / FP16 (default)/ INT8 / A16W8 /INT4/ A8W4 -->
HOST_DEVICE     = <!-- ARM WIN  (default )/  X86 LINUX  /  ARM LINUX -->

RETMOE_DEVICE_INFO = <!-- Optional. Leave empty for local inference.
                         For remote (target-device) inference, you must provide a file (text/YAML) that records:
                           a) SSH information (host/user/port and key path if needed)
                           b) Target working directory (where inference is executed)
                           c) QAIRT setup script path on the target (user-provided; sets env vars / activates venv / initializes QAIRT)
                      -->

# Required when PRECISION is INT8/A16W8/INT4/A8W4 (i.e., not FP32/FP16).
# Accepted formats:
#   1) image folder
#   2) raw folder
#   3) list file (one absolute path per line)
# Notes:
# - If source is images, convert ALL valid samples to float32 .raw and generate CALIB_LIST.
# - If source is raw folder, include ALL valid raws in CALIB_LIST.
# - If source is list file, validate entries and use it directly.
CALIBRATION_DATA = <!-- calibration source: image folder / dateset from internet / list file -->
CALIB_RAW_DIR    =  <!-- generated raw output dir when source is images. calib_data(dafault) -->
CALIB_LIST       = <!-- one absolute sample path per line.  calibration_list.txt (default)-->


OUTPUT_DIR    = <!-- e.g. qairt_output(default)-->
OWNER         = <!-- name / team / aipc(default)-->
START_TIME    = <!-- YYYY-MM-DD HH:MM get current system time -->
END_TIME      = <!-- YYYY-MM-DD HH:MM  — filled in by Validation Agent at Phase 6.5 -->
WORK_TIME     = <!-- e.g. 2h 30m       — END_TIME minus START_TIME -->
python venv   = <!-- qairt (default) | project (only if qairt venv is insufficient) -->
python lib install = <!-- ask (default) | yes | no  — always ask before pip install -->


QAIRT_ROOT    = <!--if QAIRT_ENV_SETUP is provided, derive this value from $QAIRT_SDK_ROOT after sourcing that script. /absolute path to the versioned QAIRT SDK root>

ONNX_FILE     = {MODEL_NAME}.onnx
HOST_ARCH      = <!-- can derived from HOST_DEVICE:
                     ARM WIN    → x86_64-windows-msvc  (emulation — qairt ARM WIN toolchain uses x86_64 emulation)
                     X86 LINUX  → x86_64-linux-clang
                     ARM LINUX  → aarch64-linux-gcc -->

                     
SHELL         = <!-- derived from HOST_DEVICE:
                     ARM WIN    → powershell
                     X86 LINUX  → bash
                     ARM LINUX  → bash -->
TARGET_ARCH   = <!-- derived from TARGET_DEVICE target OS/arch:
                     ARM Linux  → aarch64-ubuntu-gcc9.4
                     x86 Linux  → x86_64-linux-clang
                     ARM WIN  → windows-aarch64 -->


```

---


## Project Overview

| Field | Value |
|---|---|
| **Project Name** | {PROJECT_NAME} |
| **Model** | {MODEL_NAME} |
| **Source Framework** | {SRC_FRAMEWORK} |
| **Target Device** | {TARGET_DEVICE} |
| **Host Device** | {HOST_DEVICE} |
| **Conversion Flow** | {FLOW} |
| **Precision** | {PRECISION} |
| **Host Environment toolchian** | {HOST_ARCH} |
| **Target Architecture toolchain** | {TARGET_ARCH} |
| **Start Time** | {START_TIME} |
| **Owner** | {OWNER} |

---

## Flow Selection Guide

```
                    ┌─────────────────────────────────────────────┐
                    │         Choose Conversion Flow               │
                    └─────────────────────────────────────────────┘
                                        │
              ┌─────────────────────────┴──────────────────────────┐
              │                                                      │
              ▼                                                      ▼
   ┌─────────────────────┐                             ┌─────────────────────┐
   │   Flow A — QNN      │                             │   Flow B — SNPE     │
   │                     │                             │                     │
   │ • AI PC / Linux ARM │                             │ • Android / DSP     │
   │ • aipc wrapper      │                             │ • aipc wrapper      │
   │ • .so / .dll lib    │                             │ • .dlc file         │
   │ • HTP / CPU / GPU   │                             │ • DSP / CPU / GPU   │
   │ • Context binary    │                             │ • .dlc / ctx binary │
   └─────────────────────┘                             └─────────────────────┘
```

| Criteria | Flow A — QNN | Flow B — SNPE |
|---|---|---|
| Output format | `.bin` + `.cpp` + `.so`/`.dll` | `.dlc` |
| Inference API | `aipc` wrapper (`python aipc`) | `aipc` wrapper (`python aipc`) |
| Supported runtimes | HTP, CPU, GPU | DSP, CPU, GPU |
| Context binary | ✅ Supported | ⚙️ Optional |
| Quantization | FP16, FP32, INT8, A16W8 | FP16, FP32, INT8, A16W8  |
| Primary target | AI PC, ARM Linux | Android, Embedded Linux |
| Converter tool | `qnn-onnx-converter` | `qairt-converter` |
| Script | `aipc_convert_fp.py` / `aipc_convert_int.py` | `aipc_convert_snpe.py` |

---

## Objectives

- [ ] Export `{MODEL_NAME}` to ONNX format with QNN-compatible operators
- [ ] Inspect ONNX model I/O and operator compatibility
- [ ] Convert `{ONNX_FILE}` using **{FLOW}** flow
- [ ] (Optional) Quantize model to `{PRECISION}` with calibration data
- [ ] (Required on ARM WIN / Optional on Linux — QNN only) Generate context binary for `{TARGET_DEVICE}`
- [ ] Implement end-to-end inference pipeline using aipc launcher
- [ ] Validate accuracy and performance against baseline
- [ ] If `RETMOE_DEVICE_INFO` is set, complete remote deployment + target inference + runtime log collection

---

## Prerequisites (Common)

### Operator Patching Status (Fill during Phase 1)

> **Update these fields as you discover and patch unsupported operators:**

```
PATCH_NEEDED       = <!-- Yes / No — after dry-run inspection -->
PATCH_OPS          = <!-- comma-separated list, e.g., Mod, Einsum -->
PATCH_APPROACH     = <!-- 1 / 2 / 3 — after selecting strategy -->
PATCH_ITERATIONS   = <!-- 0 — increment after each patch attempt -->
PATCH_LAST_UPDATE  = <!-- YYYY-MM-DD HH:MM --
```

### Model-Specific Notes

> Fill these in during Phase 1 discovery:

```
INPUT_NAME    = <!-- e.g. images -->
INPUT_SHAPE   = <!-- e.g. [1, 3, 640, 640] -->
OUTPUT_NAMES  = <!-- e.g. output0, output1 -->
OPSET         = <!-- e.g. 13 -->
```

### Environment Setup

- [ ] Run the project QAIRT environment setup script (`{QAIRT_ENV_SETUP}`):
  > This script is the **project-level** env initialiser — it sources `{QAIRT_ROOT}/bin/envsetup.sh` (or `.ps1`) and performs any additional project-specific setup (Python venv activation, PATH extensions, proxy settings, etc.).
  ```bash
  # bash (x86 Linux / ARM Linux)
  source {QAIRT_ENV_SETUP}

  # PowerShell (ARM Windows)
  . "{QAIRT_ENV_SETUP}"
  ```
- [ ] Verify key variables are set after running the script:
  ```bash
  # bash
  echo $QAIRT_SDK_ROOT
  echo $PATH | tr ':' '\n' | grep qairt

  # PowerShell
  echo $env:QAIRT_SDK_ROOT
  $env:PATH -split ';' | Select-String qairt
  ```
- [ ] Python environment — **use QAIRT venv by default**:
  ```bash
  # Activate the QAIRT venv via the project env-setup script
  source {QAIRT_ENV_SETUP}          # bash
  # . "{QAIRT_ENV_SETUP}"           # PowerShell

  # Verify the active Python is from the QAIRT venv
  which python   # should resolve inside QAIRT venv path
  python --version
  ```
  > ⚠️ Do **not** create a project-specific venv unless the QAIRT venv cannot satisfy requirements.  
  > ⚠️ Before running `pip install`, **ask the user** for permission. Record the decision in `python lib install` above.

  If additional packages are needed and the user approves:
  ```bash
  pip install onnx onnxsim onnxruntime torch
  # For QNN inference (Flow A): pip install qai_appbuilder
  ```
- [ ] QAIRT toolchain verified:
  ```bash
  # Flow A — QNN  (x86 Linux host)
  {QAIRT_ROOT}/bin/x86_64-linux-clang/qnn-onnx-converter --version
  # Flow A — QNN  (ARM Windows host — uses x86_64-windows-msvc emulation to prevent QAIRT issue)
  python {QAIRT_ROOT}/bin/x86_64-windows-msvc/qnn-onnx-converter --version

  # Flow B — SNPE  (x86 Linux host)
  {QAIRT_ROOT}/bin/x86_64-linux-clang/qairt-converter --version
  # Flow B — SNPE  (ARM Windows host — uses x86_64-windows-msvc emulation)
  python {QAIRT_ROOT}/bin/x86_64-windows-msvc/qairt-converter --version
  ```
- [ ] Source model weights available
- [ ] Test input data available for validation

### Quantization Data Precheck (INT8/A16W8/INT4/A8W4 only)

- [ ] `CALIBRATION_DATA` exists and is usable (folder/file/list)
- [ ] If `CALIBRATION_DATA` is missing or invalid: resolve a suitable public dataset via web and download
- [ ] If source is images: convert ALL valid images to `.raw` into `{CALIB_RAW_DIR}`
- [ ] Build `{CALIB_LIST}` from ALL valid samples (absolute paths, one per line)
- [ ] If source is raw/list: validate and include ALL valid entries in `{CALIB_LIST}`
- [ ] Record calibration source/path and total sample count in Issue Log

### Architecture Reference

| Host OS | `HOST_ARCH` value | `HOST_ENV` value | Target Device | `TARGET_ARCH` value |
|---|---|---|---|---|
| **x86 Linux** | `X86 LINUX` | `x86_64-linux-clang` | ARM Linux | `aarch64-ubuntu-gcc9.4` |
| **x86 Linux** | `X86 LINUX` | `x86_64-linux-clang` | x86 Linux | `x86_64-linux-clang` |
| **ARM Windows** | `ARM WIN` | `x86_64-windows-msvc` | Windows ARM | `windows-aarch64` |
| **ARM Windows** | `ARM WIN` | `x86_64-windows-msvc` | ARM Linux | `aarch64-ubuntu-gcc9.4` |
| **ARM Linux** | `ARM LINUX` | `aarch64-linux-gcc` | ARM Linux | `aarch64-ubuntu-gcc9.4` |

> ⚠️ **ARM Windows**: Do **not** use `$env:PROCESSOR_ARCHITECTURE` or Python's `platform.machine()` — both can be affected by emulation. Use `(Get-WmiObject Win32_Processor).Architecture` (0 = x86, 5 = ARM, 9 = x64/AMD64, 12 = ARM64) or `dumpbin /headers model.dll | find "machine"` to reliably detect host arch.  
> ⚠️ **ARM Windows**: QAIRT converter scripts (`qnn-onnx-converter`, `qairt-converter`) live under `{QAIRT_ROOT}/bin/x86_64-windows-msvc/` and are Python scripts — invoke with `python <path>`, not directly.  
> ⚠️ **ARM Windows**: Context binary generation uses `aarch64-windows-msvc` backend (`QnnHtp.dll`). See `aipc_dev_gen_contextbin.py`.  
> ⚠️ **x86 Linux**: Toolchain binaries live under `{QAIRT_ROOT}/bin/x86_64-linux-clang/`.  
> ⚠️ **ARM Linux**: Toolchain binaries live under `{QAIRT_ROOT}/bin/aarch64-linux-gcc/`.

---

## Phase 1: Model Export to ONNX (Common)

**Agent**: Model Export Agent  
**Reference**: `skills/aipc-toolkit/references/model_export_validation.md`

### Tasks

- [ ] **1.1** Review `{MODEL_NAME}` architecture for QNN-incompatible operators
  - Known problematic ops: `Einsum`, custom attention, `GridSample`, `ScatterND`
  - Update `PATCH_NEEDED` and `PATCH_OPS` in Variables above

- [ ] **1.2** Write `export_onnx.py` with in-memory operator patches (if `PATCH_NEEDED = Yes`)
  ```python
  # Patch in-memory only — never modify library source code
  # patch_model_for_qnn(model) before torch.onnx.export()
  ```

- [ ] **1.3** Export `{MODEL_NAME}` to `{ONNX_FILE}`
  ```python
  torch.onnx.export(model, dummy_input, "{ONNX_FILE}",
                    opset_version={OPSET},
                    input_names=["{INPUT_NAME}"],
                    output_names=["{OUTPUT_NAMES}"])
  ```

- [ ] **1.4** Validate: `onnx.checker.check_model("{ONNX_FILE}")`

- [ ] **1.5** Simplify with `onnxsim`
  ```bash
  python -m onnxsim {ONNX_FILE} {ONNX_FILE}
  ```

- [ ] **1.6** ONNX inference sanity check — compare output with {SRC_FRAMEWORK} baseline

- [ ] **1.7** Iterative patching (if needed)
  - If dry-run shows new unsupported ops after patch → repeat Tasks 1.2–1.6
  - Continue patching until ALL unsupported operators are resolved (unlimited iterations)
  - Record ALL patched operators in `PATCH_OPS` (comma-separated list)
  - Escalate only when: (a) no replacement pattern exists for an operator (B7), or (b) patch would change model semantics (B4)

**Exit Criteria**: `{ONNX_FILE}` passes `onnx.checker` and produces correct outputs.

---

## Phase 2: Model Inspection (Common)

**Agent**: Model Inspector Agent  
**Reference**: `skills/aipc-toolkit/references/model_export_validation.md` §2

### Tasks

- [ ] **2.1** Inspect `{ONNX_FILE}` I/O shapes and dtypes
  ```bash
  python skills/aipc-toolkit/scripts/aipc_inspect_onnxio.py {ONNX_FILE}
  ```
  > Record results in `INPUT_NAME`, `INPUT_SHAPE`, `OUTPUT_NAMES` in Variables above.

- [ ] **2.2** Run converter dry-run to detect unsupported operators
  ```bash
  # Flow A — QNN
  {QAIRT_ROOT}/bin/x86_64-linux-clang/qnn-onnx-converter \
    --input_network {ONNX_FILE} --dry_run

  # Flow B — SNPE
  {QAIRT_ROOT}/bin/x86_64-linux-clang/qairt-converter \
    --input_network {ONNX_FILE} --dry_run
  ```

- [ ] **2.3** Document and resolve issues found
  - Issue 1: <!-- description → resolution -->
  - Issue 2: <!-- description → resolution -->

**Exit Criteria**: No unsupported operators. All shapes confirmed correct.

---

---

# ═══════════════════════════════════════════════
# FLOW A — QNN PATH
# ═══════════════════════════════════════════════

> **Use when `{FLOW} = QNN`** — AI PC, ARM Linux, Windows ARM 

---

## [QNN] Phase 3A: FP16 / FP32 Conversion

**Agent**: Conversion Agent  
**Script**: `skills/aipc-toolkit/scripts/aipc_convert_fp.py`

> Skip if going directly to INT8/A16W8 → proceed to Phase 3B.

### Tasks

- [ ] **QNN-3A.1** Run FP conversion
  ```bash
  python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
    --onnx {ONNX_FILE} \
    --output-root {OUTPUT_DIR} \
    --precision <!-- 16 or 32 --> \
    --target-arch {TARGET_ARCH}
  ```

- [ ] **QNN-3A.2** Verify conversion outputs in `{OUTPUT_DIR}`:
  - `{MODEL_NAME}.bin` ✓
  - `{MODEL_NAME}.cpp` ✓
  - `{MODEL_NAME}_net.json` ✓

- [ ] **QNN-3A.3** Compile shared library via libgen
  - Output: `lib{MODEL_NAME}.so` (Linux) / `lib{MODEL_NAME}.dll` (Windows)

- [ ] **QNN-3A.4** Verify library file is non-zero size

**Exit Criteria**: `lib{MODEL_NAME}.so` compiled successfully.

---

## [QNN] Phase 3B: INT8 / A16W8 Quantization (Optional)

**Agent**: Quantization Agent  
**Script**: `skills/aipc-toolkit/scripts/aipc_convert_int.py`  
**Reference**: `skills/aipc-toolkit/references/model_quantization.md`, `docs/QUANTIZATION_GUIDE.md`

> Use when FP16 accuracy is insufficient or targeting HTP for maximum performance.

### Additional Variables

```
CALIBRATION_DATA = {CALIBRATION_DATA}
CALIB_LIST        = <!-- path/to/calibration_list.txt -->

# Activation bitwidth (quantization target):
# - INT8: use 8
# - A16W8: use 16
# - Other modes (e.g., A8W4/INT4): set per tool/script support in docs/QUANTIZATION_GUIDE.md
ACT_BITWIDTH      = <!-- 8 or 16 (typical); see note above for other modes -->

# Weight bitwidth (quantization target):
# - INT8: typically 8
# - A16W8: 8
# - Other modes (e.g., A8W4/INT4): set per tool/script support in docs/QUANTIZATION_GUIDE.md
WEIGHT_BITWIDTH   = <!-- 8 (typical); see note above for other modes -->
```

### Tasks

- [ ] **QNN-3B.1** Prepare calibration dataset (50–200 representative samples)
  - Format: raw float32 binary `.raw` files, shape matching `{INPUT_SHAPE}`
  - Reference: `docs/CALIBRATION_DATA_PREPARATION.md`

- [ ] **QNN-3B.2** Generate `{CALIB_LIST}`
  ```
  # One file path per line
  calibration_raw/sample_001.raw
  calibration_raw/sample_002.raw
  ...
  ```

- [ ] **QNN-3B.3** Run INT quantization
  ```bash
  python skills/aipc-toolkit/scripts/aipc_convert_int.py \
    --input_network {ONNX_FILE} \
    --input_list {CALIB_LIST} \
    --output-root {OUTPUT_DIR} \
    --act_bw {ACT_BITWIDTH} \
    --weight_bw {WEIGHT_BITWIDTH} \
    --target-arch {TARGET_ARCH}
  ```

- [ ] **QNN-3B.4** Verify quantized outputs in `{OUTPUT_DIR}`:
  - `{MODEL_NAME}_a16_w8.bin` ✓
  - `{MODEL_NAME}_a16_w8.cpp` ✓
  - `{MODEL_NAME}_a16_w8_net.json` ✓
  - `lib{MODEL_NAME}_a16_w8.so` ✓

- [ ] **QNN-3B.5** Quick accuracy check vs. FP baseline (cosine similarity ≥ 0.95)

**Exit Criteria**: Quantized library compiled. Accuracy within acceptable threshold.

---

## [QNN] Phase 4: Context Binary Generation

**Agent**: Context Binary Agent  
**Script**: `skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py`

> **ARM Windows (ARM WIN)**: ⚠️ **Required** — `.dll` cannot be used directly for inference; context binary must be generated first.  
> **Linux (X86 LINUX / ARM LINUX)**: ⚙️ Optional — use when deploying to a specific SoC without on-device compilation.

### Tasks

- [ ] **QNN-4.1** Generate context binary for `{TARGET_DEVICE}`
  ```bash
  # Linux — input: lib{MODEL_NAME}.so → output: lib{MODEL_NAME}.so.bin
  python skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py \
    --model_lib lib{MODEL_NAME}.so \
    --output lib{MODEL_NAME}.so.bin

  # Windows — input: {MODEL_NAME}.dll → output: {MODEL_NAME}.dll.bin
  python skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py \
    --model_lib {MODEL_NAME}.dll \
    --output {MODEL_NAME}.dll.bin
  ```

- [ ] **QNN-4.2** Verify context binary loads on target device:
  - Linux: `lib{MODEL_NAME}.so.bin` ✓
  - Windows: `{MODEL_NAME}.dll.bin` ✓

**Exit Criteria**: Context binary generated (output = input filename + `.bin` postfix) and verified on `{TARGET_DEVICE}`.

---

## [QNN] Phase 5: Inference Implementation

**Agent**: Inference Agent  
**Reference**: `skills/aipc-toolkit/references/inference.md`
> **ARM Windows (ARM WIN)**: ⚠️ **Required** — `.dll` cannot be used directly for inference; context binary must be generated first.  
### Tasks

- [ ] **QNN-5.1** Write pre-processing pipeline
  - Input: `{INPUT_NAME}`, shape `{INPUT_SHAPE}`
  - Operations: <!-- resize, normalize, channel reorder, etc. -->
  - Output: `numpy.ndarray float32`

- [ ] **QNN-5.2** Run inference via `aipc` wrapper
  ```bash
  # Ensure QAIRT_SDK_ROOT is set (source {QAIRT_ENV_SETUP} first)
  
  # IMPORTANT: Copy context binary to match ONNX naming (Windows)
  Copy-Item {OUTPUT_DIR}\{MODEL_NAME}.dll.bin .\{MODEL_NAME}.onnx.dll.bin
  # OR (Linux)
  cp {OUTPUT_DIR}/lib{MODEL_NAME}.so.bin ./{MODEL_NAME}.onnx.so.bin
  
  # Then run inference
  python aipc path/to/onnx_inference.py
  ```
  > The `aipc` wrapper passes the `.onnx` path but searches for a matching QNN binary in the same directory.  
  > See `references/inference.md` → Model File Resolution for full search order.  
  > If I/O names fail, regenerate the model YAML.

- [ ] **QNN-5.3** Write post-processing pipeline
  - Outputs: `{OUTPUT_NAMES}`
  - Operations: <!-- softmax / NMS / decode boxes / etc. -->

- [ ] **QNN-5.4** Validate against ONNX baseline
  - input tensor name/shape match model
  - preprocessing matches training/export assumptions
  - output tensor mapping is correct
  - cosine similarity vs. ONNX baseline ≥ 0.99 (FP) / ≥ 0.95 (INT8)
  - collect latency / FPS on target runtime

**Exit Criteria**: `infer_{MODEL_NAME}.py` runs end-to-end and produces correct results.

---

---

# ═══════════════════════════════════════════════
# FLOW B — SNPE PATH
# ═══════════════════════════════════════════════

> **Use when `{FLOW} = SNPE`** — Android devices, DSP-accelerated inference — output is `.dlc`

---

## [SNPE] Phase 3: DLC Conversion (FP16 / FP32)

**Agent**: Conversion Agent  
**Script**: `skills/aipc-toolkit/scripts/aipc_convert_snpe.py`

### Tasks

- [ ] **SNPE-3.1** Run SNPE DLC conversion
  ```bash
  python skills/aipc-toolkit/scripts/aipc_convert_snpe.py \
    --onnx {ONNX_FILE} \
    --output {OUTPUT_DIR}/{MODEL_NAME}.dlc \
    --precision <!-- fp16 or fp32 -->
  ```
  > Invokes `qairt-converter` from `{QAIRT_ROOT}/bin/<host_toolchain>/`

- [ ] **SNPE-3.2** Verify `{MODEL_NAME}.dlc` exists and is non-zero

- [ ] **SNPE-3.3** Inspect DLC (optional)
  ```bash
  {QAIRT_ROOT}/bin/x86_64-linux-clang/snpe-dlc-info -i {OUTPUT_DIR}/{MODEL_NAME}.dlc
  ```

**Exit Criteria**: `{MODEL_NAME}.dlc` generated successfully.

---

## [SNPE] Phase 4: DLC Quantization (Optional)

**Agent**: Quantization Agent  
**Reference**: `skills/aipc-toolkit/references/model_quantization.md`

> Use when targeting DSP runtime for maximum performance on Android.

### Additional Variables

```
CALIBRATION_DATA = {CALIBRATION_DATA}
CALIB_LIST        = <!-- path/to/calibration_list.txt -->
```

### Tasks

- [ ] **SNPE-4.1** Prepare calibration dataset (50–200 representative samples)
  - Format: raw float32 binary `.raw` files, shape matching `{INPUT_SHAPE}`
  - Reference: `docs/CALIBRATION_DATA_PREPARATION.md`

- [ ] **SNPE-4.2** Generate `{CALIB_LIST}`
  ```
  # One file path per line
  calibration_raw/sample_001.raw
  calibration_raw/sample_002.raw
  ...
  ```

- [ ] **SNPE-4.3** Run DLC quantization
  ```bash
  {QAIRT_ROOT}/bin/x86_64-linux-clang/snpe-dlc-quant \
    --input_dlc {OUTPUT_DIR}/{MODEL_NAME}.dlc \
    --input_list {CALIB_LIST} \
    --output_dlc {OUTPUT_DIR}/{MODEL_NAME}_quantized.dlc \
    --enable_htp
  ```

- [ ] **SNPE-4.4** Verify `{MODEL_NAME}_quantized.dlc` ✓

- [ ] **SNPE-4.5** Quick accuracy check vs. FP baseline (cosine similarity ≥ 0.95)

**Exit Criteria**: Quantized DLC generated. Accuracy within acceptable threshold.

---

## [SNPE] Phase 5: Inference Implementation

**Agent**: Inference Agent  
**Reference**: `skills/aipc-toolkit/references/inference.md`

### Additional Variables

```
DLC_FILE      = <!-- {MODEL_NAME}.dlc  or  {MODEL_NAME}_quantized.dlc -->
```

### Tasks

- [ ] **SNPE-5.1** Write pre-processing pipeline
  - Input: `{INPUT_NAME}`, shape `{INPUT_SHAPE}`
  - Operations: <!-- resize, normalize, channel reorder, etc. -->
  - Output: `numpy.ndarray float32`

- [ ] **SNPE-5.2** Run inference via `aipc` wrapper
  ```bash
  # Ensure QAIRT_SDK_ROOT is set (source {QAIRT_ENV_SETUP} first)
  python aipc path/to/onnx_inference.py
  ```
  > If I/O names fail, regenerate the model YAML.

- [ ] **SNPE-5.3** Write post-processing pipeline
  - Outputs: `{OUTPUT_NAMES}`
  - Operations: <!-- softmax / NMS / decode boxes / etc. -->

- [ ] **SNPE-5.4** Validate against ONNX baseline
  - input tensor name/shape match model
  - preprocessing matches training/export assumptions
  - output tensor mapping is correct
  - cosine similarity vs. ONNX baseline ≥ 0.99 (FP) / ≥ 0.95 (INT8)
  - collect latency / FPS on target runtime

**Exit Criteria**: Inference script runs end-to-end and produces correct results.

---

---

# ═══════════════════════════════════════════════
# PHASE 6 — VALIDATION & TESTING (Common)
# ═══════════════════════════════════════════════

**Agent**: Validation & Testing Agent

## Tasks

- [ ] **6.1** Accuracy comparison: ONNX vs. {FLOW} output
  - Method: cosine similarity on `{OUTPUT_NAMES}` tensors
  - FP16/FP32 threshold: ≥ 0.99
  - INT8/A16W8 threshold: ≥ 0.95
  - Result: <!-- PASS / FAIL, score: value -->

- [ ] **6.2** Task-specific accuracy (if applicable)
  - Metric: <!-- mAP / Top-1 Acc / WER / BLEU / etc. -->
  - Baseline ({SRC_FRAMEWORK}): <!-- value -->
  - {FLOW} {PRECISION}: <!-- value -->
  - Acceptable drop: ≤ 1%

- [ ] **6.3** Latency benchmark on `{TARGET_DEVICE}`
  - Runtime: <!-- HTP / DSP / CPU / GPU -->
  - Batch size: 1
  - Avg latency: <!-- ms -->
  - Throughput: <!-- FPS -->

- [ ] **6.4** Regression test with known-good inputs
  - Test cases: <!-- N --> / Pass: <!-- N --> / Fail: <!-- 0 -->

- [ ] **6.5** Document results in `REPORT.md`
- [ ] **6.6** Record completion fields in Config (`END_TIME`, `WORK_TIME`) and final pass/fail status
- [ ] **6.7** Verify runtime validation execution on configured target (local or remote)

**Exit Criteria**: All accuracy thresholds met. Latency meets project requirements.

---

## Deliverables

### Flow A — QNN

| Artifact | Path | Status |
|---|---|---|
| ONNX model | `{ONNX_FILE}` | ⬜ |
| QNN binary | `{OUTPUT_DIR}/{MODEL_NAME}.bin` | ⬜ |
| QNN library (FP) | `lib{MODEL_NAME}.so` | ⬜ |
| QNN library (INT) | `lib{MODEL_NAME}_a16_w8.so` | ⬜ |
| Context binary (Linux) | `lib{MODEL_NAME}.so.bin` | ⬜ |
| Context binary (Windows) | `{MODEL_NAME}.dll.bin` | ⬜ |
| Export script | `export_onnx.py` | ⬜ |
| Inference script | `infer_{MODEL_NAME}.py` | ⬜ |
| Calibration data | `{CALIB_LIST}` + `calibration_raw/` | ⬜ |
| Project report | `REPORT.md` | ⬜ |

### Flow B — SNPE

| Artifact | Path | Status |
|---|---|---|
| ONNX model | `{ONNX_FILE}` | ⬜ |
| DLC (FP) | `{OUTPUT_DIR}/{MODEL_NAME}.dlc` | ⬜ |
| DLC (quantized) | `{OUTPUT_DIR}/{MODEL_NAME}_quantized.dlc` | ⬜ |
| Export script | `export_onnx.py` | ⬜ |
| Inference script | `infer_{MODEL_NAME}.py` | ⬜ |
| Calibration data | `{CALIB_LIST}` + `calibration_raw/` | ⬜ |
| Project report | `REPORT.md` | ⬜ |

---

## Issue Log

### Operator Patching Log

**Summary:**
| Metric | Value |
|--------|-------|
| Total iterations | {n} (unlimited - continue until all ops resolved) |
| Operators resolved | {n} |
| Operators remaining | {n} |
| Blocking condition | {None/B3/B4/B7} |

**Iteration History:**

#### Iteration 1
| Unsupported Op | Count | Approach | Pattern | Result |
|----------------|-------|----------|---------|--------|
| {op_name}      | {n}   | {approach}| {pattern}| ✅/❌ |

Validation:
- [ ] ONNX checker passed
- [ ] Dry-run passed
- [ ] Numerical parity verified

New ops discovered: {list or "none"}

#### Iteration 2
| Unsupported Op | Count | Approach | Pattern | Result |
|----------------|-------|----------|---------|--------|
| {op_name}      | {n}   | {approach}| {pattern}| ✅/❌ |

Validation:
- [ ] ONNX checker passed
- [ ] Dry-run passed
- [ ] Numerical parity verified

New ops discovered: {list or "none"}

#### Iteration 3, 4, 5... (continue until all operators resolved)
> **Note**: Continue documenting each iteration. There is no limit - patch until ALL unsupported operators are resolved. Escalate only when: (a) no replacement pattern exists (B7), or (b) patch changes semantics (B4).

... (repeat for each iteration until complete)

### Final Patch Summary

**Operators patched (final list):**
1. {op1} → {replacement_pattern}
2. {op2} → {replacement_pattern}

**Files modified:**
- {file1}: {description}

**Artifacts generated:**
- {artifact1}: {path}
- {artifact2}: {path}

---

### General Issues

| # | Phase | Flow | Issue | Status | Resolution |
|---|---|---|---|---|---|
| 1 | | {FLOW} | | Open | |

---

## Progress Summary

| Phase | Description | Flow | Status |
|---|---|---|---|
| 0 | Prerequisites & Environment Setup | Common | ⬜ Not Started |
| 1 | Model Export to ONNX | Common | ⬜ Not Started |
| 2 | Model Inspection | Common | ⬜ Not Started |
| QNN-3A | FP16/FP32 Conversion | QNN | ⬜ Not Started |
| QNN-3B | INT8/A16W8 Quantization | QNN | ⬜ Not Started |
| QNN-4 | Context Binary Generation | QNN | ⬜ Not Started |
| QNN-5 | Inference (aipc wrapper) | QNN | ⬜ Not Started |
| SNPE-3 | DLC Conversion | SNPE | ⬜ Not Started |
| SNPE-4 | DLC Quantization | SNPE | ⬜ Not Started |
| SNPE-5 | Inference (aipc wrapper) | SNPE | ⬜ Not Started |
| 6 | Validation & Testing | Common | ⬜ Not Started |

> Status legend: ⬜ Not Started · 🔄 In Progress · ✅ Done · ❌ Blocked

---

## References

| Resource | Path |
|---|---|
| AIPC Skill (main) | `../SKILL.md` |
| Agent Definitions | `../assets/aipc_AGENTS.md` |
| Model Export Guide | `../references/model_export_validation.md` |
| **Operator Patching** | **`../references/operator_patching.md`** |
| Quantization Guide | `../references/model_quantization.md` |
| Inference Reference | `../references/inference.md` |
| QNN Conversion | `../references/qnn_conversion.md` |
| SNPE Conversion | `../references/snpe_conversion.md` |
| Context Binary | `../references/context_binary.md` |
| Troubleshooting | `../references/troubleshooting.md` |
| Windows Setup | `../references/win_qairt_setup.md` |
