# QNN Conversion Reference

## Scope
Use this reference when `{FLOW}=QNN`.

> **Wrapper scripts vs. direct toolchain calls**  
> `aipc_convert_fp.py` and `aipc_convert_int.py` are the **recommended entry points** for QNN conversion.  
> They internally invoke `qnn-onnx-converter` and `qnn-model-lib-generator`, and include **tested auto-detection logic** that resolves known QAIRT toolchain path issues across all supported host architectures (ARM WIN / X86 LINUX / ARM LINUX).  
> Preserve-IO policy in wrapper scripts:  
> - Default: `--preserve_io` (keep layout + datatype)  
> - Fallback: `--preserve_io layout` when target runtime shows FP16/dtype compatibility issues  
> Prefer these scripts over calling the toolchain binaries directly.

## Dependencies

**CMake is required** for `qnn-model-lib-generator` to compile model libraries (.dll/.so).

### Windows
- Install **Visual Studio** (2019, 2022, or later) with the **"C++ CMake tools for Windows"** component
  - Or install [CMake standalone](https://cmake.org/download/) and add it to PATH
- CMake is typically located at:
  `C:\Program Files\Microsoft Visual Studio\<version>\<edition>\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\`
  where `<version>` = 2019, 2022, etc. and `<edition>` = Community, Professional, Enterprise
- If `cmake` is not in PATH, add it before running conversion:
  ```powershell
  $env:PATH = "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;" + $env:PATH
  ```

### Linux
```bash
sudo apt install cmake
```

> **Note**: The `aipc_convert_fp.py` script checks for CMake before conversion and will fail fast with guidance if it is missing.

## Target architecture selection

Omit `--host-arch`,`--target-arch` to let the script auto-detect (picks the most common case for the host OS).  

Override explicitly when you need a specific target:

| `--target-arch` | Purpose |
|----------------|---------|
| `x86_64-linux-clang` | Host testing on x86 Linux (auto-detected default on Linux) |
| `aarch64-ubuntu-gcc9.4` | ARM target device support (cross-compile output) |
| `x86_64-windows-msvc` | Host testing on Windows (auto-detected default on Windows) |

> **Note:** `aarch64-ubuntu-gcc9.4` produces a library for ARM deployment; the resulting `.so` cannot be run on an x86 host.

## Linux cross-compile toolchain note (`aarch64-ubuntu-gcc9.4`)

When building ARM Linux model libraries on a Linux host, `qnn-model-lib-generator` validates the Ubuntu aarch64 toolchain using:

- `QNN_AARCH64_UBUNTU_GCC_94` (sysroot root)
- `${QNN_AARCH64_UBUNTU_GCC_94}/usr/bin/aarch64-linux-gnu-g++` (compiler path)

### Required setup

Install the cross compiler package:

```bash
sudo apt install g++-aarch64-linux-gnu
```

Set the environment variable before conversion/libgen:

```bash
# Typical Ubuntu host setup when compiler is at /usr/bin/aarch64-linux-gnu-g++
export QNN_AARCH64_UBUNTU_GCC_94=/
```

### Quick verification

```bash
which aarch64-linux-gnu-g++
printf '<%s>\n' "$QNN_AARCH64_UBUNTU_GCC_94"
```

Expected:
- `which` prints `/usr/bin/aarch64-linux-gnu-g++`
- variable is set to `/` (recommended)

### Common failure signatures

- `Could not find compiler: ${QNN_AARCH64_UBUNTU_GCC_94}/usr/bin/aarch64-linux-gnu-g++`
  - Meaning: toolchain variable/path was not resolved as expected by libgen.
- `fatal error: ... No space left on device`
  - Meaning: compilation ran but failed due to host disk/tmp capacity (check `/tmp` and free space).

## Float conversion (FP16/FP32)
```bash
python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
  --onnx model.onnx \
  --precision 16 \
  --preserve-io-mode datatype
```

If target runtime shows FP16/dtype compatibility issues, retry with layout-only preservation:
```bash
python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
  --onnx model.onnx \
  --precision 16 \
  --preserve-io-mode layout
```

### Explicit input dimensions for dynamic inputs

If the ONNX model has dynamic input shapes, you must specify explicit dimensions using `--input-dim`:

```bash
python aipc_convert_fp.py \
  --onnx model.onnx \
  --precision 16 \
  --output-root qnn_output \
  --input-dim input,1,3,64,64
```

Format: `--input-dim input_name,dim1,dim2,dim3,dim4`

You can specify multiple inputs by repeating the flag:
```bash
python aipc_convert_fp.py \
  --onnx model.onnx \
  --input-dim input1,1,3,224,224 \
  --input-dim input2,1,10
```

Expected outputs:
- `model.bin`
- `model.cpp`
- `model_net.json`
- compiled model library (`.so` or `.dll`)

## Quantized conversion (INT8/INT16)
```bash
python skills/aipc-toolkit/scripts/aipc_convert_int.py \
  --input_network model.onnx \
  --input_list calibration_list.txt \
  --output-root qnn_output \
  --act_bw 8 \
  --weight_bw 8 \
  --preserve-io-mode datatype
```

If target runtime shows FP16/dtype compatibility issues, retry with:
```bash
python skills/aipc-toolkit/scripts/aipc_convert_int.py \
  --input_network model.onnx \
  --input_list calibration_list.txt \
  --output-root qnn_output \
  --act_bw 8 \
  --weight_bw 8 \
  --preserve-io-mode layout
```

> **Troubleshooting**: If conversion fails with "Unsupported operator" errors, see [In-Memory Operator Patching](operator_patching.md) for patching guidance.

## Converter dry-run

> Verify the toolchain binary exists for your `HOST_ARCH` before running. hardcoded setting current

```bash
# Step 1 — confirm toolchain binary (bash: X86 LINUX / ARM LINUX)
ls ${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/qnn-onnx-converter        # X86 LINUX
ls ${QAIRT_SDK_ROOT}/bin/aarch64-linux-gcc/qnn-onnx-converter          # ARM LINUX

# Step 1 — confirm toolchain binary (PowerShell: ARM WIN)
Test-Path "$env:QAIRT_SDK_ROOT\bin\x86_64-windows-msvc\qnn-onnx-converter"

# Step 2 — run dry-run with the matching toolchain binary
# X86 LINUX
${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/qnn-onnx-converter \
  --input_network model.onnx --dry_run

# ARM LINUX(not tested!)
${QAIRT_SDK_ROOT}/bin/aarch64-linux-gcc/qnn-onnx-converter \
  --input_network model.onnx --dry_run

# ARM WIN (PowerShell, x86_64-windows-msvc emulation to prevent qairt issue)
python "$env:QAIRT_SDK_ROOT\bin\x86_64-windows-msvc\qnn-onnx-converter" `
  --input_network model.onnx --dry_run
```

## Context binary generation
Use on target device when host and model-lib arch differ.

```bash
python skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py \
  --model /absolute/path/to/libmodel.so \
  --output model_context.bin
```

### Options

| Option | Required | Description |
|--------|----------|-------------|
| `--model` | ✅ Yes | Absolute path to compiled model library (`.so` on Linux, `.dll` on Windows) |
| `--output` | No | Output path for the context binary. If omitted, output is placed in `output/<name>` in the current directory |
| `--profiling` | No | Enable HTP optrace profiling (`--profiling_level detailed --profiling_option optrace`) |

### Backend resolved automatically by script

| Host OS | Arch dir used | Backend |
|---------|--------------|---------|
| Linux | `x86_64-linux-clang` | `libQnnHtp.so` |
| Windows | `aarch64-windows-msvc` | `QnnHtp.dll` |

### Preflight

**Linux:**
```bash
# Linux
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
| Linux | `x86_64` | `aarch64` (`.so`) | Blocked on host — run on ARM target device |
| Linux | `aarch64` | `aarch64` (`.so`) | Allowed |
| Linux | `x86_64` | `x86_64` (`.so`) | Allowed |
| Windows | `ARM64` | `ARM64` (`.dll`) | Allowed |
| Windows | `AMD64` | `ARM64` (`.dll`) | Blocked on host — run on ARM target device |

If architectures do not match, **do not run** `qnn-context-binary-generator` locally. Stop and instruct the user to run on the target device.
