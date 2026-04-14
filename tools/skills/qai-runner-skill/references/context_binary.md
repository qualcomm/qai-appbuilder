# Context Binary Generation Reference

Generate hardware-specific HTP context binaries for on-device deployment.
Use absolute paths for all model file arguments.

## Troubleshooting Flow

If context binary generation fails, follow this structured flow:

```
Step 1: Is this Windows ARM or Linux ARM?
  ├─ Windows ARM → Context binary is MANDATORY → Continue to Step 2
  └─ Linux ARM   → Optional (.so works directly) → Can skip

Step 2: What does the error say?
  Look for: operator name, error code (e.g., 0xc26), "unsupported", "validation failed"
  └─→ Match the error in the Error → Action table in operator_patching.md

Step 3: For each failing operator
  3a. Identify the operator name and input types from the error log
  3b. Check input types: TopK output = INT64, Conv output = FLOAT, Constant dtype in Netron
  3c. Follow the Error → Action table in operator_patching.md to patch
  3d. Validate: onnx.checker.check_model(patched.onnx)

Step 4: Re-convert the patched model
  4a. python aipc_convert_fp.py --onnx patched.onnx ...
  4b. Re-generate context binary

Step 5: If all patterns exhausted
  → Escalate as Blocking Condition B7
  → Consider: SNPE/DLC flow or CPU/GPU backend alternative
```

---

## ⚠️ CRITICAL: Context Binary Requirements

**Context binary requirements vary by platform:**

| Target Platform | Context Binary | Can use library directly? |
|-----------------|----------------|--------------------------|
| **ARM Windows** | **REQUIRED** — `{model}.dll.bin` | ❌ NO — `.dll` alone will NOT load |
| **ARM Linux**   | **OPTIONAL** — `{model}.so.bin` | ✅ YES — `.so` works directly |
| x86 Linux       | N/A (CPU-only) | ✅ YES — use x86 wrapper |

**If context binary generation fails:**
- **Windows**: → **Blocking Condition B8** — Cannot proceed to inference
- **Linux**: → Can proceed with `.so` library directly
- **Alternative**: Consider SNPE flow (`.dlc`) if QNN HTP is incompatible

---

## QNN Context Binary Generation

Use `scripts/aipc_dev_gen_contextbin.py` to generate a QNN context binary from a compiled model library (`.so` on Linux / `.dll` on Windows).

### Mandatory preflight — architecture check (no exceptions)

Before running, verify that the native host architecture matches the model library architecture.

> **Note**: Context binary compilation failures (e.g., `Failed to compile layer 'Einsum_123'`) often trace back to unsupported operators in the ONNX graph. See [In-Memory Operator Patching](operator_patching.md) §Stage 4 for resolution.

**Linux:**
```bash
uname -m
file /absolute/path/to/libmodel.so
```

**Windows** (do not use Python `platform.machine()` or `$env:PROCESSOR_ARCHITECTURE` — both can be affected by emulation):
```powershell
# Reliable: WMI Win32_Processor.Architecture
(Get-WmiObject Win32_Processor).Architecture
# 0 = x86, 5 = ARM, 9 = x64 (AMD64), 12 = ARM64

# Or use dumpbin to check DLL architecture:
dumpbin /headers C:\path\to\model.dll | find "machine"
```

| OS | Host arch | Model lib | Action |
|----|-----------|-----------|--------|
| Linux | `x86_64` | `aarch64` (`.so`) | ❌ Blocked on host — run on ARM target device |
| Linux | `aarch64` | `aarch64` (`.so`) | ✅ Allowed |
| Windows | `ARM64` | `ARM64` (`.dll`) | ✅ Allowed |
| Windows | `AMD64` | `ARM64` (`.dll`) | ❌ Blocked on host — run on ARM target device |

If architectures do not match, **do not run** `qnn-context-binary-generator` locally. Stop and instruct the user to run on the target device.

### Usage

**Linux:**
```bash
python skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py \
  --model /absolute/path/to/libmodel.so \
  --output model_context.bin
```

**Windows:**
```powershell
python skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py \
  --model C:\absolute\path\to\model.dll \
  --output model_context.dll.bin
```

### x86 Host Support (QNN)

When generating a QNN context binary on an x86 host, use `aipc_dev_gen_contextbin_x86.py`.
This script resolves the host SDK `bin/lib` arch directory automatically and invokes `qnn-context-binary-generator`.

**Linux x86_64:**
```bash
python aipc_dev_gen_contextbin_x86.py \
  --model /absolute/path/to/libmodel.so \
  --output /absolute/path/to/model_context.bin \
  --backend htp
```

**Windows x86_64:**
```powershell
python aipc_dev_gen_contextbin_x86.py `
  --model C:\absolute\path\to\model.dll `
  --output C:\absolute\path\to\model_context.bin `
  --backend htp
```

**DLC input (Linux/Windows):**
```bash
python aipc_dev_gen_contextbin_x86.py \
  --dlc /absolute/path/to/model.dlc \
  --output /absolute/path/to/model.dlc.bin \
  --backend htp
```

Notes:
- `QAIRT_SDK_ROOT` must be set before running.
- `--backend` supports `htp` and `cpu` (default: `htp`).
- Input is mutually exclusive: use exactly one of `--model` or `--dlc`.
- If `--output` is omitted, output is created under `./output/<binary_name>.bin`.
- Use `--profiling` to enable `optrace` profiling flags.

### Options

| Option | Description |
|--------|-------------|
| `--model` | Absolute path to compiled model library (`.so` on Linux, `.dll` on Windows) |
| `--output` | Output path for the context binary (`.bin`) |
| `--profiling` | Enable HTP optrace profiling (`--profiling_level detailed --profiling_option optrace`). Required for performance analysis. |

`aipc_dev_gen_contextbin_x86.py` additional options:

| Option | Description |
|--------|-------------|
| `--dlc` | Path to input `.dlc`; script uses `QnnModelDlc` loader automatically |
| `--backend` | Backend library for generator: `htp` or `cpu` (default: `htp`) |

---

## SNPE/DLC Context Binary Generation

Generate an HTP context binary from a `.dlc` file using `qnn-context-binary-generator` directly.  
Use **absolute paths** for `--dlc_path` and `--binary_file`.

### Windows

```bash
qnn-context-binary-generator \
  --backend QnnHtp.dll \
  --model QnnModelDlc.dll \
  --dlc_path /absolute/path/to/model.dlc \
  --binary_file model
```

### Linux (aarch64)

```bash
${QAIRT_SDK_ROOT}/bin/aarch64-oe-linux-gcc11.2/qnn-context-binary-generator \
  --backend libQnnHtp.so \
  --model libQnnModelDlc.so \
  --dlc_path /absolute/path/to/model.dlc \
  --binary_file model
```

Output is written to an `output/` folder; the binary will be `output/model.dlc.bin`.

---

## Verification checklist

- [ ] Host arch matches model lib arch (or running on target device)
- [ ] Absolute paths used for all model file arguments
- [ ] Context binary (`.bin`) exists and is non-zero
- [ ] Binary loads correctly on `{TARGET_DEVICE}`


## See Also

- `scripts/aipc_dev_gen_contextbin.py` — Wrapper script that handles both `.so` (Linux) and `.dll` (Windows) model libraries
- `aipc_dev_gen_contextbin_x86.py` — Host-arch-aware wrapper for generating QNN context binaries on x86/ARM hosts
