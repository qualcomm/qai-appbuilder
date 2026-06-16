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
OPTIMIZE_LAYOUT = <!-- YES / NO (default NO); QNN only. If YES, run final optimization pass by removing --preserve_io -->

SRC_FRAMEWORK = <!-- PyTorch(default) /   ONNX -->
TARGET_DEVICE = <!-- ARM WIN  (QCOM) / x86 Linux/ ARM Linux (QCOM) -->

PRECISION     = <!-- FP32 / FP16 (default)/ BF16(experimental) / INT8 / A16W8 /INT4/ A8W4 -->
QUANT_TOOL    = <!-- QAIRT (default) / AIMET
                     QAIRT: uses aipc_convert_int.py / snpe-dlc-quant — simple, works on all platforms.
                     AIMET: uses AIMET QuantSim — runs the same PRECISION as configured, but adds
                            advanced PTQ techniques (CLE, AdaRound, SeqMSE) for higher accuracy.
                            Produces .onnx + .encodings → handed to QAIRT converter via
                            --quantization_overrides. Linux only (x86 or ARM Linux host required).
                     Only relevant when PRECISION is INT8 / A16W8 / INT4 / A8W4. -->
HOST_DEVICE     = <!-- ARM WIN  (default )/  X86 LINUX  /  ARM LINUX -->
CONTEXT_BINARY_GEN = <!-- YES (default) / NO
                         YES: generate a hardware-specific HTP context binary on the HOST (x86) for the target SoC, then deploy to target (required on ARM WIN; recommended for fixed-SoC deployment)
                         NO:  skip context binary generation (use raw .so / .dll directly; only valid when on-device JIT compilation is acceptable) -->

RETMOE_DEVICE_INFO = <!-- Optional to configure. Leave empty for local inference.
                         If set, remote target inference is MANDATORY for final acceptance (host-only validation is not sufficient).
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
TARGET_ARCH   = <!-- derived from CONTEXT_BINARY_GEN and TARGET_DEVICE:
                     if TARGET_DEVIC is ARM WIN, ignore CONTEXT_BINARY_GEN  :
                        ARM WIN    → windows-x86_64 (emulation — qairt ARM WIN toolchain uses x86_64 emulation)
                        ### windows-aarch64 is for old method, not used now .
                     If CONTEXT_BINARY_GEN = YES (default):
                        X86 LINUX → x86_64-linux-clang
                        ARM LINUX → x86_64-linux-clang (the context binary generation process utilizes the host toolchain as an emulation environment)
                     If CONTEXT_BINARY_GEN = NO:
                       TARGET_ARCH is derived from TARGET_DEVICE target OS/arch:
                       ARM Linux  → aarch64-ubuntu-gcc9.4
                       x86 Linux  → x86_64-linux-clang 
                       -->
                       


# Toolchain artifact rules:
# - Model library (.dll / .so) must be generated by qnn-model-lib-generator; never reuse any existing .bin/.cpp/.so as the model library.
# - qnn-context-binary-generator --model expects a model library generated by qnn-model-lib-generator.
# - HOST_ARCH = QAIRT SDK bin/lib directory name (e.g. x86_64-windows-msvc)
# - TARGET_ARCH = qnn-model-lib-generator -t value (e.g. windows-x86_64). These are different strings.
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
| **Quantization Tool** | {QUANT_TOOL} |
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
| Quantization | FP16, FP32, BF16 (experimental export/input path only), INT8, A16W8 | FP16, FP32, BF16 (experimental source ONNX only), INT8, A16W8  |
| Primary target | AI PC, ARM Linux | Android, Embedded Linux |
| Converter tool | `qnn-onnx-converter` | `qairt-converter` |
| Script | `aipc_convert_fp.py` / `aipc_convert_int.py` | `aipc_convert_snpe.py` |

### Quantization Tool Selection

```
Is PRECISION INT8 / A16W8 / INT4 / A8W4?
        │
        ├─ NO  → QUANT_TOOL not applicable (FP16/FP32/BF16 path)
        │
        └─ YES ─┬─ HOST_DEVICE = ARM WIN  ──────────────────► QUANT_TOOL = QAIRT  (AIMET not supported on Windows)
                │
                └─ HOST_DEVICE = X86 LINUX / ARM LINUX ──────┬─ QUANT_TOOL = QAIRT   (simpler, faster)
                                                              └─ QUANT_TOOL = AIMET   (same precision + advanced PTQ
                                                                                       for higher accuracy; Linux only)
```

| Criteria | QAIRT (default) | AIMET |
|---|---|---|
| Platform | All (Windows, Linux) | Linux only (x86 / ARM) |
| Precision applied | As configured (`{PRECISION}`) | Same as configured (`{PRECISION}`) |
| Calibration | `.raw` list → `aipc_convert_int.py` | Python calibration callback → `QuantSim` |
| Advanced PTQ | ✗ | ✅ CLE, AdaRound, SeqMSE — same precision, higher accuracy |
| Output artifacts | `.bin` / `.cpp` / `.dlc` directly | `.onnx` + `.encodings` → QAIRT converter |
| QAIRT handoff | Direct | `--quantization_overrides <model>.encodings` |
| Reference | `references/model_quantization.md` | `references/model_quantization_aimet.md` |

---

## Objectives

- [ ] Export `{MODEL_NAME}` to ONNX format with QNN-compatible operators
- [ ] Inspect ONNX model I/O and operator compatibility
- [ ] Convert `{ONNX_FILE}` using **{FLOW}** flow
- [ ] (Optional) Quantize model to `{PRECISION}` using **{QUANT_TOOL}** with calibration data
- [ ] (Required on ARM WIN / Optional on Linux — QNN only) Generate context binary for `{TARGET_DEVICE}`
- [ ] Implement end-to-end inference pipeline using aipc launcher
- [ ] Validate accuracy and performance against baseline
- [ ] Profile model on target runtime and produce bottleneck + suggestion report
- [ ] If `RETMOE_DEVICE_INFO` is set, complete remote deployment + target inference + runtime log collection (**required for final acceptance**)

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
- [ ] Linux cross-build preflight (when `TARGET_ARCH = aarch64-ubuntu-gcc9.4`):
  ```bash
  which aarch64-linux-gnu-g++
  export QNN_AARCH64_UBUNTU_GCC_94=/
  printf '<%s>\n' "$QNN_AARCH64_UBUNTU_GCC_94"
  ```
  > If compiler is missing: `sudo apt install g++-aarch64-linux-gnu`
- [ ] **Windows preflight — cmake and MSVC toolchain** (QNN only, required by `qnn-model-lib-generator`):
  ```powershell
  # Check cmake
  where.exe cmake 2>$null
  if ($?) { cmake --version | Select-Object -First 1 }

  # Discover Visual Studio Launch-VsDevShell (any version/edition)
  $vsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (Test-Path $vsWhere) {
    $vsPath = & $vsWhere -latest -property installationPath 2>$null
    if ($vsPath) {
      & "$vsPath\Common7\Tools\Launch-VsDevShell.ps1" -Arch amd64 2>&1 | Out-Null
      Write-Output "VS dev shell: $vsPath"
    }
  }
  if (-not $vsPath) {
    Write-Warning "Visual Studio not found. qnn-model-lib-generator will fail."
    Write-Warning "Install Visual Studio with 'Desktop development with C++' workload"
  }

  # Verify MSVC and Windows SDK are available after dev shell
  cl.exe 2>$null
  if ($?) { Write-Output "MSVC compiler: OK" } else { Write-Warning "MSVC compiler not found" }
  ```
  > Without cmake and Visual Studio C++ tools, `qnn-model-lib-generator` fails with:
  > `'cmake' is not recognized` or `The CXX compiler identification is unknown`.
  > Install any Visual Studio version (2019, 2022, or later) with the "Desktop development with C++" workload,
  > and ensure cmake is available (bundled with VS or installed separately via `winget install cmake`).
- [ ] Source model weights available
- [ ] Test input data available for validation

### Wrapper Artifact & Runtime-Libs Preflight (QNN inference, platform-aware)

- [ ] Run wrapper artifact preflight before Phase 5 inference:
  ```bash
  # In model working directory
  ls -1 {MODEL_NAME}*.bin 2>/dev/null || true
  ```
- [ ] Remove/quarantine stale auto-matched context artifacts before deploying a new one:
  ```bash
  mkdir -p _ctx_backup
  mv -f {MODEL_NAME}.so.bin _ctx_backup/ 2>/dev/null || true
  mv -f {MODEL_NAME}.onnx.so.bin _ctx_backup/ 2>/dev/null || true
  mv -f {MODEL_NAME}.htp.bin _ctx_backup/ 2>/dev/null || true
  ```
- [ ] In context-binary mode, deploy with ONNX-matching filename:
  ```bash
  cp <generated_context>.bin {MODEL_NAME}.onnx.so.bin
  ```
- [ ] Linux ARM: pin QNN runtime libs directory explicitly (do not rely on auto-resolution):
  ```bash
  export QAI_QNN_LIBS_DIR={QAIRT_ROOT}/lib/aarch64-oe-linux-gcc11.2
  export LD_LIBRARY_PATH=$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH
  export ADSP_LIBRARY_PATH={QAIRT_ROOT}/lib/hexagon-v{DSP_ARCH}/unsigned
  ```
- [ ] Remote acceptance env snapshot (mandatory when `RETMOE_DEVICE_INFO` is set):
  ```bash
  # Linux example (x86 Linux / ARM Linux)
  SNAPSHOT=acceptance_env_snapshot.txt
  {
    echo "QAI_QNN_RUNTIME=$QAI_QNN_RUNTIME"
    echo "QAI_QNN_LIBS_DIR=$QAI_QNN_LIBS_DIR"
    echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
    echo "ADSP_LIBRARY_PATH=$ADSP_LIBRARY_PATH"
    echo "PRODUCT_SOC=$PRODUCT_SOC"
    echo "DSP_ARCH=$DSP_ARCH"
  } | tee "$SNAPSHOT"
  ```
- [ ] Gate: if any snapshot field is empty, mark as ❌ Blocked and stop final acceptance claim.
- [ ] Runtime libs consistency check (platform-aware; Linux command below):
  ```bash
  export LD_DEBUG=libs
  timeout 15 python -u aipc onnx_inference.py > lddebug.log 2>&1 || true
  grep -E 'libQnnHtp\\.so|libQnnSystem\\.so' lddebug.log
  ```
- [ ] If runtime core libs are loaded from different parent directories/toolchain families, mark ❌ Blocked.
- [ ] Record both resolved paths in Issue Log and REPORT.

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
> ⚠️ **ARM Windows**: Context binary generation runs on the **host** x86 using `x86_64-windows-msvc` toolchain (`QnnHtp.dll`). See `host_context_binary_gen.md`.  
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
  > If `{PRECISION}=BF16`, cast the model and dummy input to `torch.bfloat16` before export. Treat BF16 ONNX export as experimental and validate end-to-end.

- [ ] **1.4** Validate: `onnx.checker.check_model("{ONNX_FILE}")`

- [ ] **1.5** Simplify with `onnxsim`
  ```bash
  python -m onnxsim {ONNX_FILE} {ONNX_FILE}
  ```

- [ ] **1.6** ONNX inference sanity check — compare output with {SRC_FRAMEWORK} baseline
  > If `RETMOE_DEVICE_INFO` is set, skip local quick-smoke sanity inference at this step and perform target-device inference first in Phase 5.

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

## [QNN] Phase 3A: FP16 / FP32 / BF16 Conversion

**Agent**: Conversion Agent  
**Script**: `skills/aipc-toolkit/scripts/aipc_convert_fp.py`

> Skip if going directly to low-bit quantization → proceed to Phase 3B.
>
> If `{PRECISION}=BF16`, do **not** assume a dedicated BF16 converter mode exists. Use this phase only after confirming the exported ONNX is accepted by the converter/runtime, or fall back to FP32/FP16.
>
> 🚫 **Phase 3 guardrail**: `--preserve-io-mode none` is forbidden in conversion/quantization phases (3A/3B/3C).
> Use `datatype` by default, or `layout` only when required for compatibility.
> Baseline-first policy: complete Phase 6 validation on preserve-io artifacts before entering QNN Phase 8.

### Tasks

- [ ] **QNN-3A.1** Run FP conversion
  ```bash
  python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
    --onnx {ONNX_FILE} \
    --output-root {OUTPUT_DIR} \
    --precision <!-- 16 or 32 --> \
    --preserve-io-mode datatype \
    --target-arch {TARGET_ARCH}
  ```
  > If target runtime shows FP16/dtype compatibility issues, rerun with `--preserve-io-mode layout`.
  > Do **not** use `--preserve-io-mode none` in Phase 3A.

- [ ] **QNN-3A.2** Verify conversion outputs in `{OUTPUT_DIR}`:
  - `{MODEL_NAME}.bin` ✓
  - `{MODEL_NAME}.cpp` ✓
  - `{MODEL_NAME}_net.json` ✓

- [ ] **QNN-3A.3** Compile shared library via libgen
  - Output: `lib{MODEL_NAME}.so` (Linux) / `lib{MODEL_NAME}.dll` (Windows)

- [ ] **QNN-3A.4** Verify library file is non-zero size

**Exit Criteria**: `lib{MODEL_NAME}.so` compiled successfully.

---

## [QNN] Phase 3B: Model Quantization (INT4/INT8/A16W8) — QAIRT path (default)

**Agent**: Quantization Agent  
**Script**: `skills/aipc-toolkit/scripts/aipc_convert_int.py`  
**Reference**: `skills/aipc-toolkit/references/model_quantization.md`

> Use when `{QUANT_TOOL} = QAIRT`. Quantizes to `{PRECISION}` using the QAIRT toolchain directly.  
> Quantizes to the precision set in `{PRECISION}` using the QAIRT toolchain directly.  
> If `{QUANT_TOOL} = AIMET`, skip this phase and follow **Phase 3C** instead.
> 🚫 `--preserve-io-mode none` is forbidden in this phase.

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

- [ ] **QNN-3B.2** Generate `{CALIB_LIST}`
  ```
  # One absolute file path per line
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
    --preserve-io-mode datatype \
    --target-arch {TARGET_ARCH}
  ```
  > If target runtime shows dtype compatibility issues, rerun with `--preserve-io-mode layout`.
  > Do **not** use `--preserve-io-mode none` in Phase 3B.

- [ ] **QNN-3B.4** Verify quantized outputs in `{OUTPUT_DIR}`:
  - `{MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.bin` ✓
  - `{MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.cpp` ✓
  - `lib{MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.so` ✓

- [ ] **QNN-3B.5** Quick accuracy check vs. FP baseline (cosine similarity ≥ 0.95)

**Exit Criteria**: Quantized library compiled. Accuracy within acceptable threshold.

---

## [QNN] Phase 3C: Model Quantization (INT4/INT8/A16W8) — AIMET path (Linux only)

**Agent**: Quantization Agent  
**Reference**: `skills/aipc-toolkit/references/model_quantization_aimet.md`

> Use when `{QUANT_TOOL} = AIMET`.  
> Quantizes to the **same precision** as `{PRECISION}`, with advanced PTQ techniques (CLE, AdaRound, SeqMSE)  
> applied on top for higher accuracy than the QAIRT path.  
> ⚠️ Linux host required (x86 Linux or ARM Linux). Not available on ARM Windows.  
> Produces `.onnx` + `.encodings` → passed to `qnn-onnx-converter` via `--quantization_overrides`.
> 🚫 `--preserve-io-mode none` is forbidden in this phase.

### Additional Variables

```
AIMET_RUN_ID      = <!-- short identifier for this run, e.g. yolov8n_w8a8 -->
AIMET_WORK_DIR    = <!-- /local2/mnt/workspace/aimet/runs/{AIMET_RUN_ID} -->
AIMET_ARTIFACTS   = <!-- {AIMET_WORK_DIR}/artifacts -->
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

- [ ] **QNN-3C.1** Set up AIMET environment
  ```bash
  source ~/qairt_2404.sh
  mkdir -p {AIMET_WORK_DIR}/{artifacts,logs,data}
  python -c 'import torch, aimet_torch; print(torch.__version__, getattr(aimet_torch, "__version__", "unknown"))'
  ```
  ```bash
  # AIMET ONNX import preflight (recommended before long PTQ runs)
  python - <<'PY'
from aimet_onnx.common.defs import QuantScheme
from aimet_onnx.quantsim import QuantizationSimModel
print("AIMET ONNX import preflight: OK")
PY
  ```
  - If both `aimet_onnx` and `aimet_torch` are installed, avoid `aimet_common` imports in ONNX flow.

- [ ] **QNN-3C.2** Select AIMET PTQ technique (see `references/model_quantization_aimet.md` — Escalation Flow):
  - **Baseline**: CLE (data-free) + standard PTQ at `{PRECISION}`
  - **For higher accuracy**: add AdaRound or SeqMSE at the same `{PRECISION}`
  - All techniques operate at the precision configured in `{PRECISION}` — do not change bitwidth here
  - Record chosen technique in `AIMET_RUN_ID`

- [ ] **QNN-3C.3** Run AIMET PTQ (example: CLE + W8A8 baseline)
  ```python
  # See references/model_quantization_aimet.md for full templates
  # PyTorch path:
  import aimet_torch.cross_layer_equalization as cle
  from aimet_torch.quantsim import QuantizationSimModel
  from aimet_common.defs import QuantScheme

  cle.equalize_model(model, (<batch>, <channels>, <height>, <width>))
  sim = QuantizationSimModel(model, dummy_input,
      quant_scheme=QuantScheme.post_training_tf_enhanced,
      default_param_bw={WEIGHT_BITWIDTH}, default_output_bw={ACT_BITWIDTH})
  sim.compute_encodings(calib_cb, None)
  sim.export(path="{AIMET_ARTIFACTS}", filename_prefix="{MODEL_NAME}_ptq", dummy_input=dummy_input)
  ```
  ```bash
  python {AIMET_WORK_DIR}/ptq_baseline.py | tee {AIMET_WORK_DIR}/logs/ptq_baseline.log
  ```
  - For A16W8, set QuantSim types explicitly:
    - `param_type='int8'`
    - `activation_type='int16'`
  - First-pass calibration sample target: **50–200** representative samples.

- [ ] **QNN-3C.4** Verify AIMET artifacts:
  - `{AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.onnx` ✓
  - `{AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.encodings` ✓

- [ ] **QNN-3C.5** Evaluate accuracy (cosine similarity vs. FP32 baseline ≥ 0.99 target)
  ```python
  import numpy as np
  f = np.fromfile("{AIMET_ARTIFACTS}/ref_float.raw", dtype=np.float32)
  q = np.fromfile("{AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.raw", dtype=np.float32)
  cos = np.dot(f, q) / (np.linalg.norm(f) * np.linalg.norm(q) + 1e-12)
  print("Cosine Similarity:", float(cos))
  ```
  - If cosine < 0.99: apply AdaRound or SeqMSE at the same `{PRECISION}` per `references/model_quantization_aimet.md`

- [ ] **QNN-3C.6** Convert AIMET artifacts to QNN IR (pass encodings via `--quantization_overrides`)
  ```bash
  qnn-onnx-converter \
    --input_network {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.onnx \
    --quantization_overrides {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.encodings \
    -d {INPUT_NAME} $(echo {INPUT_SHAPE} | tr -d '[]' | tr ' ' ',') \
    --out_node {OUTPUT_NAMES} \
    -o {OUTPUT_DIR}/{MODEL_NAME}_aimet_qnn.cpp
  ```
  > ⚠️ Do **not** pass `--input_list` to `qnn-onnx-converter` — it is not a valid argument here.
  > Use preserve-io `datatype` (or `layout` only if required). Do **not** use `none` in Phase 3C.

- [ ] **QNN-3C.7** Compile shared library
  ```bash
  python {QAIRT_ROOT}/bin/x86_64-linux-clang/qnn-model-lib-generator \
    -c {OUTPUT_DIR}/{MODEL_NAME}_aimet_qnn.cpp \
    -b {OUTPUT_DIR}/{MODEL_NAME}_aimet_qnn.bin \
    -o {OUTPUT_DIR} -t {TARGET_ARCH}
  ```

- [ ] **QNN-3C.8** Verify outputs:
  - `{OUTPUT_DIR}/{MODEL_NAME}_aimet_qnn.bin` ✓
  - `{OUTPUT_DIR}/{MODEL_NAME}_aimet_qnn.cpp` ✓
  - `lib{MODEL_NAME}_aimet_qnn.so` ✓

**Exit Criteria**: AIMET `.encodings` generated, QNN library compiled, cosine similarity meets budget.

---

## [QNN] Phase 4: Context Binary Generation (Host-Side)

**Agent**: Context Binary Agent  
**Reference**: `skills/aipc-toolkit/references/host_context_binary_gen.md`

> Context binary generation runs on the **host** (x86 Linux or x86 Windows), not on the target device.
> The host uses `qnn-context-binary-generator` with `soc_id`/`dsp_arch` config to compile a binary for the target SoC.
> The resulting `.bin` is then deployed to the target for inference.
>
> **ARM Windows (ARM WIN)**: ⚙️ Optional — `.dll.bin` recommended for fixed-SoC deployment; `.dll` direct path is allowed.  
> **Linux (X86 LINUX / ARM LINUX)**: ⚙️ Optional — use when deploying to a specific SoC without on-device JIT compilation.
> **Linux fallback policy**: Do **not** switch to non-context `.so` unless all applicable methods in `host_context_binary_gen.md` are attempted and logged (soc/dsp validation, `vtcm_mb` sweep, mapping alternatives, `htp_arch`/no-`soc_id` path).
>
> ⚠️ **`SOC_ID` and `DSP_ARCH` are mandatory.** Confirm them from the target device before generation.
> See `host_context_binary_gen.md` → Step 1 for how to read these values.

### Tasks

- [ ] **QNN-4.1** Confirm `{SOC_ID}` and `{DSP_ARCH}` from target device (see `host_context_binary_gen.md` Step 1; on Windows on Snapdragon, run the `aipc_qairt_devinfo.ps1` script from the skill scripts directory to automatically detect them)

- [ ] **QNN-4.2** Create SoC config file (`.conf`) on the host:

  ```bash
  # Linux host
  cat > /tmp/soc{SOC_ID}_{DSP_ARCH}.conf << 'CONF'
  {"graphs":[{"graph_names":["{MODEL_NAME}"],"vtcm_mb":0,"O":3}],"devices":[{"soc_id":{SOC_ID},"dsp_arch":"{DSP_ARCH}","cores":[{"perf_profile":"burst","rpc_control_latency":50}]}]}
  CONF
  ```

  ```powershell
  # Windows host
  '{"graphs":[{"graph_names":["{MODEL_NAME}"],"vtcm_mb":0,"O":3}],"devices":[{"soc_id":{SOC_ID},"dsp_arch":"{DSP_ARCH}","cores":[{"perf_profile":"burst","rpc_control_latency":50}]}]}' `
    | Set-Content C:\tmp\soc{SOC_ID}_{DSP_ARCH}.conf
  ```

- [ ] **QNN-4.3** Create backend extension wrapper JSON on the host:

  ```bash
  # Linux host
  cat > /tmp/soc{SOC_ID}_{DSP_ARCH}.json << JSONEOF
  {"backend_extensions":{"shared_library_path":"$QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtpNetRunExtensions.so","config_file_path":"/tmp/soc{SOC_ID}_{DSP_ARCH}.conf"}}
  JSONEOF
  ```

  ```powershell
  # Windows host
  ('{"backend_extensions":{"shared_library_path":"' + $env:QAIRT_SDK_ROOT + '\\lib\\x86_64-windows-msvc\\QnnHtpNetRunExtensions.dll","config_file_path":"C:\\tmp\\soc{SOC_ID}_{DSP_ARCH}.conf"}}') `
    | Set-Content C:\tmp\soc{SOC_ID}_{DSP_ARCH}.json
  ```

- [ ] **QNN-4.4** Run `qnn-context-binary-generator` on the host:

  ```bash
  # Linux x86 host → generates binary for target SoC {SOC_ID}/{DSP_ARCH}
  mkdir -p {OUTPUT_DIR}
  $QAIRT_SDK_ROOT/bin/x86_64-linux-clang/qnn-context-binary-generator \
    --backend     $QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtp.so \
    --model       {OUTPUT_DIR}/lib{MODEL_NAME}.so \
    --binary_file lib{MODEL_NAME}.so \
    --output_dir  {OUTPUT_DIR} \
    --config_file /tmp/soc{SOC_ID}_{DSP_ARCH}.json
  # output: {OUTPUT_DIR}/lib{MODEL_NAME}.so.bin
  ```

  ```powershell
  # Windows  host → generates binary for target SoC {SOC_ID}/{DSP_ARCH}
  & "$env:QAIRT_SDK_ROOT\bin\x86_64-windows-msvc\qnn-context-binary-generator.exe" `
    --backend     "$env:QAIRT_SDK_ROOT\lib\x86_64-windows-msvc\QnnHtp.dll" `
    --model       "{OUTPUT_DIR}\{MODEL_NAME}.dll" `
    --binary_file {MODEL_NAME}.dll `
    --output_dir  "{OUTPUT_DIR}" `
    --config_file "C:\tmp\soc{SOC_ID}_{DSP_ARCH}.json"
  # output: {OUTPUT_DIR}\{MODEL_NAME}.dll.bin
  ```

  > ⚠️ `--binary_file` takes a **stem without `.bin`** — the tool appends `.bin` automatically.  
  > Do **not** pass an absolute path to `--binary_file` — it double-appends `.bin`.

- [ ] **QNN-4.5** Preflight — verify host model library architecture before VTCM sweep:

  The context binary generator must load the model `.so` or `.dll` on the **host**.
  If the `.so` was compiled for aarch64 it cannot be loaded on x86 — generation
  will silently fail or produce a corrupt binary. Verify arch before proceeding.

  **Linux Host (.so):**
  ```bash
  file {OUTPUT_DIR}/<host_toolchain>/lib{MODEL_NAME}.so
  # Expected: ELF 64-bit LSB shared object, x86-64
  # Wrong:    ELF 64-bit LSB shared object, ARM aarch64  ← rebuild host library required
  ```

  **Windows Host (.dll):**
  ```powershell
  # Check DLL headers using dumpbin or visual verification
  dumpbin /headers "{OUTPUT_DIR}\<host_toolchain>\{MODEL_NAME}.dll" | findstr "machine"
  # Expected: x64
  # Wrong:    ARM64  ← rebuild host library required
  ```

  If the host library is missing or compiled for the wrong architecture, rebuild it from the existing `.cpp`/`.bin` sources (no re-quantization needed):
  ```bash
  # Linux Host
  python {QAIRT_ROOT}/bin/x86_64-linux-clang/qnn-model-lib-generator \
    -c {MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.cpp \
    -b {MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.bin \
    -o {OUTPUT_DIR} -t x86_64-linux-clang

  # Windows Host
  python "$env:QAIRT_SDK_ROOT\bin\x86_64-windows-msvc\qnn-model-lib-generator" `
    -c {MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.cpp `
    -b {MODEL_NAME}_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.bin `
    -o {OUTPUT_DIR} -t windows-x86_64
  ```

- [ ] **QNN-4.6** VTCM sweep — generate, deploy, and validate all values on target device:

  > Host generation always succeeds for all `vtcm_mb` values.
  > Failures only surface at runtime on the target. Always sweep the full range
  > and select the **maximum passing** value (higher = better HTP performance).

  Record results in VTCM sweep log below.

### VTCM Sweep Log

| vtcm_mb | Host gen | Device load | Latency | Error |
|---------|----------|-------------|---------|-------|
| <!-- --> | <!-- OK/FAIL --> | <!-- PASS/FAIL --> | <!-- ms --> | <!-- error msg --> |

```
VTCM_PREFERRED  = <!-- value that failed, with error -->
VTCM_SELECTED   = <!-- maximum passing value -->
```

- [ ] **QNN-4.7** Deploy final context binary (maximum passing `vtcm_mb`) to target:
  ```bash
  scp {OUTPUT_DIR}/lib{MODEL_NAME}.so.bin <user>@<target-host>:<workdir>/
  ```

- [ ] **QNN-4.8 (Linux fallback gate)** If context still fails on Linux, complete and log all applicable troubleshooting attempts before `.so` fallback:
  - confirm `SOC_ID`/`DSP_ARCH` from target identity
  - sweep `vtcm_mb=0,1,2,3,4,8` (see above)
  - test `soc_id`/`dsp_arch` alternatives from QAIRT mapping
  - test `htp_arch` / no-`soc_id` path when applicable
  - attach command + error log evidence in Issue Log

**Exit Criteria**: Context binary generated on host, VTCM sweep completed on target, maximum passing `vtcm_mb` selected and deployed.

---

## [QNN] Phase 5: Inference Implementation

**Agent**: Inference Agent  
**Reference**: `skills/aipc-toolkit/references/inference.md`
> **ARM Windows (ARM WIN)**: ⚙️ Optional — `.dll.bin` recommended for fixed-SoC deployment; `.dll` direct path is allowed.  
### Tasks

- [ ] **QNN-5.0** Wrapper preflight (MUST before final acceptance run)
  - confirm current candidate artifacts near `{ONNX_FILE}`
  - clean stale matched context files from prior runs
  - confirm deployed context filename follows ONNX match rule (`{MODEL_NAME}.onnx.so.bin`)
  - record selected artifact path in Issue Log

- [ ] **QNN-5.0b** Linux ARM runtime-libs pin (MUST for target acceptance)
  ```bash
  export QAI_QNN_LIBS_DIR={QAIRT_ROOT}/lib/aarch64-oe-linux-gcc11.2
  export LD_LIBRARY_PATH=$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH
  export ADSP_LIBRARY_PATH={QAIRT_ROOT}/lib/hexagon-v{DSP_ARCH}/unsigned
  ```
  - Record `QAI_QNN_LIBS_DIR` and effective `LD_LIBRARY_PATH` in Issue Log
  - If omitted, errors may include: `Failed to load skel`, `Transport layer setup failed: 14001`

- [ ] **QNN-5.0d** Persist acceptance environment snapshot and attach to artifacts
  - use platform-appropriate commands (Linux command shown in preflight section)
  - output file: `{OUTPUT_DIR}/acceptance_env_snapshot.txt`
  - reference this file in `REPORT.md` and `Issue Log`

- [ ] **QNN-5.0e** Runtime libs consistency gate
  - confirm runtime core libs resolve to the same intended toolchain/runtime family
  - if mismatch: stop acceptance and fix runtime path selection first (e.g., `QAI_QNN_LIBS_DIR` / loader path alignment)

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
  > Ensure wrapper-selected QNN file is the intended deployed artifact (not stale from previous runs).
  > The `aipc` wrapper passes the `.onnx` path but searches for a matching QNN binary in the same directory.  
  > See `references/inference.md` → Model File Resolution for full search order.  
  > If I/O names fail, regenerate the model YAML.
  > Linux `.so` (non-context) is allowed only if QNN-4.7 fallback gate is satisfied and logged.

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

## [SNPE] Phase 3: DLC Conversion (FP16 / FP32 / BF16)

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
  >
  > If `{PRECISION}=BF16`, treat BF16 only as a possible **source ONNX dtype**, not as a guaranteed SNPE converter precision mode. Validate converter acceptance and correctness explicitly.

- [ ] **SNPE-3.2** Verify `{MODEL_NAME}.dlc` exists and is non-zero

- [ ] **SNPE-3.3** Inspect DLC (optional)
  ```bash
  {QAIRT_ROOT}/bin/x86_64-linux-clang/snpe-dlc-info -i {OUTPUT_DIR}/{MODEL_NAME}.dlc
  ```

**Exit Criteria**: `{MODEL_NAME}.dlc` generated successfully.

---

## [SNPE] Phase 4: DLC Quantization (INT4/INT8/A16W8, Optional)

**Agent**: Quantization Agent  
**Reference**: `skills/aipc-toolkit/references/model_quantization.md`, `skills/aipc-toolkit/references/model_quantization_aimet.md`

> Use when targeting DSP runtime for maximum performance on Android.  
> Choose sub-path based on `{QUANT_TOOL}`. Both paths quantize to the precision set in `{PRECISION}`.

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

---

### SNPE Phase 4A — QAIRT path (default, all platforms)

> Use when `{QUANT_TOOL} = QAIRT`. Quantizes to `{PRECISION}` using the QAIRT toolchain directly.

- [ ] **SNPE-4A.1** Prepare calibration dataset (50–200 representative samples)
  - Format: raw float32 binary `.raw` files, shape matching `{INPUT_SHAPE}`

- [ ] **SNPE-4A.2** Generate `{CALIB_LIST}`
  ```
  # One absolute file path per line
  calibration_raw/sample_001.raw
  calibration_raw/sample_002.raw
  ...
  ```

- [ ] **SNPE-4A.3** Run DLC quantization
  ```bash
  {QAIRT_ROOT}/bin/x86_64-linux-clang/snpe-dlc-quant \
    --input_dlc {OUTPUT_DIR}/{MODEL_NAME}.dlc \
    --input_list {CALIB_LIST} \
    --output_dlc {OUTPUT_DIR}/{MODEL_NAME}_quantized.dlc \
    --enable_htp
  ```

- [ ] **SNPE-4A.4** Verify `{MODEL_NAME}_quantized.dlc` ✓

- [ ] **SNPE-4A.5** Quick accuracy check vs. FP baseline (cosine similarity ≥ 0.95)

**Exit Criteria**: Quantized DLC generated. Accuracy within acceptable threshold.

---

### SNPE Phase 4B — AIMET path (better precision, Linux only)

> Use when `{QUANT_TOOL} = AIMET`.  
> Quantizes to the **same precision** as `{PRECISION}`, with advanced PTQ techniques (CLE, AdaRound, SeqMSE)  
> applied on top for higher accuracy than the QAIRT path.  
> ⚠️ Linux host required. Not available on ARM Windows.  
> Produces `.onnx` + `.encodings` → converted to DLC via `snpe-onnx-to-dlc --quantization_overrides`.

- [ ] **SNPE-4B.1** Run AIMET PTQ at `{PRECISION}` (see `references/model_quantization_aimet.md` for full templates)
  - Apply CLE + standard PTQ as baseline; optionally add AdaRound or SeqMSE for higher accuracy
  - All techniques stay at the precision configured in `{PRECISION}`
  - Produces: `{AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.onnx` + `{MODEL_NAME}_ptq.encodings`
  - See QNN Phase 3C tasks 3C.1–3C.5 for AIMET environment setup and execution steps

- [ ] **SNPE-4B.2** Convert AIMET ONNX → DLC (pass encodings via `--quantization_overrides`)
  ```bash
  snpe-onnx-to-dlc \
    --input_network {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.onnx \
    --quantization_overrides {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.encodings \
    -d {INPUT_NAME} $(echo {INPUT_SHAPE} | tr -d '[]' | tr ' ' ',') \
    --out_node {OUTPUT_NAMES} \
    -o {OUTPUT_DIR}/{MODEL_NAME}_aimet.dlc
  ```
  > Skip `snpe-dlc-quantize` — AIMET encodings are already embedded via `--quantization_overrides`.

- [ ] **SNPE-4B.3** Verify `{MODEL_NAME}_aimet.dlc` ✓

- [ ] **SNPE-4B.4** Quick accuracy check vs. FP baseline (cosine similarity ≥ 0.95)

**Exit Criteria**: AIMET-quantized DLC generated. Accuracy within acceptable threshold.

---

## [SNPE] Phase 5: Inference Implementation

**Agent**: Inference Agent  
**Reference**: `skills/aipc-toolkit/references/inference.md`

### Additional Variables

```
DLC_FILE      = <!-- {MODEL_NAME}.dlc  /  {MODEL_NAME}_quantized.dlc (QAIRT)  /  {MODEL_NAME}_aimet_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.dlc (AIMET) -->
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

### Accuracy Validation Criteria

- [ ] Accuracy validation
  - [ ] Cosine similarity > 0.995
  - [ ] SNR > 30 dB
  - [ ] Task metric loss < 1% *(check only if task-specific evaluation data is available)*

## Tasks

- [ ] **6.1** Accuracy comparison: ONNX vs. {FLOW} output
  - Method: cosine similarity on `{OUTPUT_NAMES}` tensors
  - FP16/FP32/BF16 threshold: ≥ 0.99
  - BF16 threshold: user-confirmed tolerance after end-to-end validation
  - Low-Bit Quantization (INT4/INT8/A16W8) threshold: ≥ 0.95
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

# ═══════════════════════════════════════════════
# PHASE 7 — PROFILING (Common)
# ═══════════════════════════════════════════════

**Agent**: Profiling Agent  
**Reference**: `skills/aipc-toolkit/references/profiling.md`

> Run after Phase 6 validation passes. Profiling requires a working inference pipeline.  
> Goal: collect per-layer execution data, identify bottlenecks, and produce an actionable report.
> 
> ⚠️ **Directory Isolation Rule**: For all profiling work, you must copy/save all related artifacts—including the compiled context binary, the converted QNN/SNPE model bin, network structure JSONs, dynamic libraries, and the generated profile output trace files—into a separate directory dedicated to the specific layout preservation mode under evaluation. The directory name must use the prefix `qairt_profile_{layout}` (for example: `qairt_profile_datatype`, `qairt_profile_layout`, or `qairt_profile_none`).

## Tasks

- [ ] **7.1** Confirm model format and select profiling path

  | Model format | Pre-step |
  |---|---|
  | QNN `.so` / `.dll` | None — enable profiling directly |
  | QNN `.bin` (context binary) | Regenerate `.bin` with `--profiling` flag first |
  | SNPE `.dlc` | None — enable profiling directly |

- [ ] **7.2** (QNN `.bin` only) Regenerate context binary with optrace instrumentation

  Follow the full procedure in `skills/aipc-toolkit/references/host_context_binary_gen.md` to regenerate the context binary.
  When running `qnn-context-binary-generator`, add the profiling flags:

  ```
  --profiling_level detailed --profiling_option optrace
  ```

  These flags embed optrace instrumentation in the output `.bin` and also produce a `*_schematic.bin` file required for QHAS visualization.
  See the [Additional CLI Options](../references/host_context_binary_gen.md#additional-cli-options) section of that reference for details.

- [ ] **7.3** Run inference with profiling enabled

  **QNN (`.so` / `.bin`)**
  ```python
  import onnxruntime as ort
  sess_options = ort.SessionOptions()
  sess_options.enable_profiling = True
  sess = ort.InferenceSession("{MODEL_NAME}.onnx", sess_options)
  # run inference as normal
  # output → qairt_profile_output/qnn-profiling-data_0.log
  ```

  **SNPE (`.dlc`) — detailed level (per-layer timings)**
  ```bash
  # Via aipc wrapper (recommended)
  QAI_SNPE_PROFILING_LEVEL=detailed python aipc path/to/infer_{MODEL_NAME}.py
  # output → snpe_output/SNPEDiag_0.log
  ```

- [ ] **7.4** Convert profiling log to human-readable / visualizable format

  **QNN — generate optrace/chrometrace and QHAS report:**
  > ⚠️ **Important**: 
  > 1. The schematic binary (the `.bin` file output directly by `qnn-onnx-converter`) differs from the compiled context binary. Ensure both the context binary (`.bin`) and the schematic binary are in the same folder as the profiling log to ensure the profile viewer resolves symbols correctly.
  > 2. You must also create and save the optrace configuration file (`optrace_config.json`) in the same folder before running the profile viewer.
  > 3. Refer to [`references/profiling.md`](../references/profiling.md) (QNN QHAS / Optrace Profiling section) for the exact config contents, viewer reader options, and commands, as they may change depending on your environment.
  
  Convert the profiling log to Chrome trace formats and generate the interactive QHAS HTML diagnostic dashboard (`chromeTrace_optrace_qnn_htp_analysis_summary.html`) as described in the reference guide. If the optrace reader fails, follow the fallbacks detailed in [`references/profiling.md`](../references/profiling.md).

  **Expected QNN Profiling Artifacts Checklist:**
  - [ ] OptTrace/ChromeTrace outputs (e.g., `chromeTrace_optrace.json`, `chromeTrace_optrace_htp.json`)
  - [ ] QHAS Interactive HTML Dashboard report (e.g., `chromeTrace_optrace_qnn_htp_analysis_summary.html`)
  - [ ] TODO: Perform linting checks on the generated profile traces (not yet completed/validated).

  **SNPE**
  ```bash
  ${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/snpe-diagview \
      --input_log snpe_output/SNPEDiag_0.log \
      --output {OUTPUT_DIR}/profiling/snpe_profile.csv | tee {OUTPUT_DIR}/profiling/snpe-diagview_stdout.txt
  ```
  > Always preserve the full `snpe-diagview` stdout — it contains the human-readable per-layer block.

- [ ] **7.5** Produce **Profiling Report** (fill in after log analysis and append directly inside the existing `REPORT.md` under a `## Profiling & Bottleneck Report` section)

  ### Profiling Report (To be appended to `REPORT.md`)

  **Runtime**: <!-- HTP / DSP / CPU / GPU -->  
  **Backend**: <!-- QNN / SNPE -->  
  **Precision**: {PRECISION}  
  **Total inference latency**: <!-- ms -->

  #### Bottleneck Analysis

  | Rank | Layer / Op name | Type | Execution time (ms / cycles) | % of total | Backend |
  |---|---|---|---|---|---|
  | 1 | <!-- --> | <!-- --> | <!-- --> | <!-- --> | <!-- HTP / CPU --> |
  | 2 | <!-- --> | <!-- --> | <!-- --> | <!-- --> | <!-- --> |
  | 3 | <!-- --> | <!-- --> | <!-- --> | <!-- --> | <!-- --> |
  | 4 | <!-- --> | <!-- --> | <!-- --> | <!-- --> | <!-- --> |
  | 5 | <!-- --> | <!-- --> | <!-- --> | <!-- --> | <!-- --> |

  > Fill from `htp_stats.txt` (QNN) or `snpe-diagview_stdout.txt` (SNPE) per-layer section.  
  > Flag any layer running on CPU instead of HTP/DSP — these are the primary latency culprits.

  #### Suggestions

  | # | Observation | Suggested Action |
  |---|---|---|
  | 1 | <!-- e.g. Layer X runs on CPU fallback --> | <!-- e.g. Patch operator / relax precision for that layer --> |
  | 2 | <!-- e.g. Top-3 ops account for >60% latency --> | <!-- e.g. Consider mixed precision to reduce their cost --> |
  | 3 | <!-- e.g. High VTCM pressure causing spills --> | <!-- e.g. Increase vtcm_mb in context binary config --> |
  | 4 | <!-- e.g. BN / Reshape layers not fused --> | <!-- e.g. Apply CLE / BN fold before re-conversion --> |
  | 5 | <!-- add as needed --> | <!-- --> |

  > Populate suggestions based on actual bottleneck findings above.  
  > Common patterns: CPU fallback ops → operator patching; precision mismatch → re-quantize; VTCM spills → vtcm_mb sweep.

- [ ] **7.6** Save all profiling and model artifacts to the layout-specific directory `qairt_profile_{layout}/` (e.g. `qairt_profile_datatype/`) rather than the original output folder.

**Exit Criteria**: Per-layer profiling log collected, bottleneck table filled, at least one actionable suggestion recorded, and all artifacts (model files, context binaries, and profiling traces) isolated in the layout-specific profile directory.


---

## [QNN] Phase 8: Layout Optimization (Optional, End-of-Plan)

**Agent**: Optimization Agent  
**Reference**: `skills/aipc-toolkit/references/optimization.md`

> Use only when `{FLOW} = QNN` and `{OPTIMIZE_LAYOUT} = YES`.  
> SNPE flow does not support this optimization phase.  
> Run this phase after Phase 6 validation (and ideally Phase 7 profiling).  
> The only optimization choice here is removing `--preserve_io` so conversion/runtime can use hardware-preferred layout/type.
> Strict sequencing: never run this phase before QNN-3A/3B/3C baseline conversion and Phase 6 validation are both complete.
> 
> ⚠️ **Directory Isolation Rule**: All optimized artifacts generated during this phase (libraries, model binaries, net JSONs, and context binaries) must be saved in a separate, dedicated folder prefixed with `qairt_profile_` indicating the layout mode (typically `qairt_profile_none/`). Do not overwrite the baseline artifacts.

### Tasks

- [ ] **8.1** Confirm optimization scope and acceptance target
  - Keep baseline metrics from Phase 6/7 as comparison anchor.
  - Record target backend (`HTP` / `GPU` / `CPU`) and scenario (local or remote target).

- [ ] **8.2** Build optimization candidate by removing `--preserve_io`
  - Do not use preserve-io flags (`--preserve_io` or `--preserve_io layout`) in this run.
  - If script supports it, use:
  ```bash
  python scripts/aipc_convert_fp.py \
    --onnx {ONNX_FILE} \
    --preserve-io-mode none \
    --output-dir qairt_profile_none
  ```
  - Apply the same no-preserve-io setting for INT path (`aipc_convert_int.py`) or AIMET path (`aipc_convert_aimet.py`) when applicable, targeting `qairt_profile_none/` (or the equivalent layout-prefixed directory).

- [ ] **8.3** Re-implement I/O handling as needed
  - Re-check generated model I/O metadata (`_net.json` / yaml / inspector output).
  - Update preprocessing and postprocessing for layout or datatype changes.

- [ ] **8.4** Re-run validation and profiling with optimized artifact
  - Accuracy check against baseline.
  - Latency/FPS comparison on the same backend and input set (⚠️ **Note**: latency/FPS must be measured **without** enabling profiling settings, as profiling degrades performance. This means:
    1. The `onnxwrapper` session option must have profiling disabled: `sess_options.enable_profiling = False`.
    2. The deployed context binary must be compiled **without** the `--profiling` configuration flag).

- [ ] **8.5** Decision gate
  - Keep optimized artifact only if end-to-end performance gain is real and accuracy remains acceptable.
  - Otherwise, keep baseline preserve-io path and document reason.

### Optimization Summary (append to `REPORT.md`)

| Item | Baseline | Optimized | Delta |
|---|---|---|---|
| Accuracy metric | <!-- --> | <!-- --> | <!-- --> |
| Avg latency (ms) | <!-- --> | <!-- --> | <!-- --> |
| Throughput (FPS) | <!-- --> | <!-- --> | <!-- --> |
| Notes | <!-- preserve-io mode and integration changes --> | <!-- --> | <!-- --> |

**Exit Criteria**: QNN no-preserve-io decision recorded with metrics, and final optimized artifacts saved in the dedicated `qairt_profile_none/` folder.

---

## Deliverables

### Flow A — QNN

| Artifact | Path | Status |
|---|---|---|
| ONNX model | `{ONNX_FILE}` | ⬜ |
| QNN binary | `{OUTPUT_DIR}/{MODEL_NAME}.bin` | ⬜ |
| QNN metadata json | `{OUTPUT_DIR}/{MODEL_NAME}_net.json` (generated by `qnn-onnx-converter`) | ⬜ |
| QNN library (FP) | `lib{MODEL_NAME}.so` | ⬜ |
| QNN library (INT) | `lib{MODEL_NAME}_a16_w8.so` | ⬜ |
| Context binary (Linux) | `lib{MODEL_NAME}.so.bin` | ⬜ |
| Context binary (Windows) | `{MODEL_NAME}.dll.bin` | ⬜ |
| Export script | `export_onnx.py` | ⬜ |
| Inference script | `infer_{MODEL_NAME}.py` | ⬜ |
| Calibration data | `{CALIB_LIST}` + `calibration_raw/` | ⬜ |
| AIMET encodings (if AIMET) | `{OUTPUT_DIR}/model_aimet_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.encodings` | ⬜ |
| AIMET ONNX (if AIMET) | `{OUTPUT_DIR}/model_aimet_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.onnx` | ⬜ |
| Profiling log (QNN) | `{OUTPUT_DIR}/profiling/qnn-profiling-data_0.log` | ⬜ |
| Profiling report | Appended to `REPORT.md` | ⬜ |
| Project report | `REPORT.md` | ⬜ |

### Flow B — SNPE

| Artifact | Path | Status |
|---|---|---|
| ONNX model | `{ONNX_FILE}` | ⬜ |
| DLC (FP) | `{OUTPUT_DIR}/{MODEL_NAME}.dlc` | ⬜ |
| DLC (quantized — QAIRT) | `{OUTPUT_DIR}/{MODEL_NAME}_quantized.dlc` | ⬜ |
| DLC (quantized — AIMET) | `{OUTPUT_DIR}/{MODEL_NAME}_aimet_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.dlc` | ⬜ |
| Export script | `export_onnx.py` | ⬜ |
| Inference script | `infer_{MODEL_NAME}.py` | ⬜ |
| Calibration data | `{CALIB_LIST}` + `calibration_raw/` | ⬜ |
| Profiling log (SNPE) | `snpe_output/SNPEDiag_0.log` | ⬜ |
| Profiling report | Appended to `REPORT.md` | ⬜ |
| Project report | `REPORT.md` | ⬜ |

---

## Issue Log

### Remote Acceptance Environment Snapshot (platform-aware)
| Key | Value |
|-----|-------|
| QAI_QNN_RUNTIME | |
| QAI_QNN_LIBS_DIR | |
| LD_LIBRARY_PATH | |
| ADSP_LIBRARY_PATH | |
| PRODUCT_SOC | |
| DSP_ARCH | |
| Snapshot file path | |

### Runtime Libraries Resolution (platform-aware)
| Library | Resolved path |
|---------|---------------|
| libQnnHtp.so | |
| libQnnSystem.so | |

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
| QNN-3A | FP16/FP32/BF16 Conversion | QNN | ⬜ Not Started |
| QNN-3B | Model Quantization (INT4/INT8/A16W8) — QAIRT | QNN | ⬜ Not Started |
| QNN-3C | Model Quantization (INT4/INT8/A16W8) — AIMET, same precision + advanced PTQ (Linux only) | QNN | ⬜ Not Started |
| QNN-4 | Context Binary Generation | QNN | ⬜ Not Started |
| QNN-5 | Inference (aipc wrapper) | QNN | ⬜ Not Started |
| SNPE-3 | DLC Conversion (FP16/FP32/BF16) | SNPE | ⬜ Not Started |
| SNPE-4A | DLC Quantization (INT4/INT8/A16W8) — QAIRT | SNPE | ⬜ Not Started |
| SNPE-4B | DLC Quantization (INT4/INT8/A16W8) — AIMET, same precision + advanced PTQ (Linux only) | SNPE | ⬜ Not Started |
| SNPE-5 | Inference (aipc wrapper) | SNPE | ⬜ Not Started |
| 6 | Validation & Testing | Common | ⬜ Not Started |
| 7 | Profiling & Bottleneck Report | Common | ⬜ Not Started |
| QNN-8 | Layout Optimization (remove `--preserve_io`, optional end-of-plan) | QNN | ⬜ Not Started |

> Status legend: ⬜ Not Started · 🔄 In Progress · ✅ Done · ❌ Blocked

---

## References

| Resource | Path |
|---|---|
| AIPC Skill (main) | `../SKILL.md` |
| Agent Definitions | `../assets/aipc_AGENTS.md` |
| Model Export Guide | `../references/model_export_validation.md` |
| **Operator Patching** | **`../references/operator_patching.md`** |
| Quantization Guide (QAIRT) | `../references/model_quantization.md` |
| Quantization Guide (AIMET) | `../references/model_quantization_aimet.md` |
| Inference Reference | `../references/inference.md` |
| QNN Conversion | `../references/qnn_conversion.md` |
| SNPE Conversion | `../references/snpe_conversion.md` |
| Context Binary | `../references/context_binary.md` |
| Host Context Binary Gen | `../references/host_context_binary_gen.md` |
| Profiling | `../references/profiling.md` |
| Optimization | `../references/optimization.md` |
| Troubleshooting | `../references/troubleshooting.md` |
| Windows Setup | `../references/win_qairt_setup.md` |
