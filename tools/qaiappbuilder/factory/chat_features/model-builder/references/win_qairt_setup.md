# Task: Setup QAIRT Environment on Windows (ARM64 WoS / x64)

## Overview

Setup for **WoS ARM64** (Snapdragon X Elite) or **Windows x64** using **QAIRT SDK `<QAIRT SDK version>`** (recorded in `${APP_ROOT}\data\config\qairt_env.json`). Host arch chosen by `Setup.bat` auto-detect or `Setup.bat --arch arm64|x64` and locked in `data\config\host_arch`.

> ℹ️ **Pre-configured by `Setup.bat`.** All paths in `qairt_env.json`; scripts read automatically. This file is troubleshooting/reference only.

---

## What `Setup.bat` Configures

Runs once. Creates:
- `venv\.venv_x64_310` — x86_64 Python 3.10 (conversion, key: `python_x64_venv`). Same on both hosts.
- Runtime venv (key `python_runtime_venv`; legacy alias `python_arm64_venv` still populated):
  - WoS ARM64 (`Setup.bat` default) → `venv\.venv_arm64_313` (aarch64 Python 3.13).
  - x64 Windows (`Setup.bat --arch x64` on WoS, or auto-detected on native x64) → `venv\.venv_x64_313` (x86_64 Python 3.13).
- `qairt_env.json` — all paths (SDK root, venvs, VS paths, `qairt_runtime_subdir` = matching arch subdir).
- `data\config\host_arch` (`arm64` | `x64`) — single source of truth for scripts.
- QAIRT SDK at `C:\Qualcomm\AIStack\QAIRT\<version>\`.
- VS 2022 Build Tools (`vs_vcvarsall`, `vc_targets_path`) — **only used by Flow C** (legacy DLL). Flow A + `windows-x64` do not need it.

`${APP_ROOT}\data\config\qairt_env.json` structure:
```json
{
  "qairt_sdk_root":       "<path to QAIRT SDK>",
  "python_x64_venv":      "<path to x86_64 Python 3.10 venv>",
  "python_runtime_venv":  "<path to runtime venv — .venv_arm64_313 on WoS, .venv_x64_313 on x64>",
  "python_arm64_venv":    "<legacy alias for python_runtime_venv — kept for older scripts>",
  "qairt_runtime_subdir": "aarch64-windows-msvc | x86_64-windows-msvc",
  "vs_vcvarsall":         "<path to vcvarsall.bat, Flow C only>",
  "vc_targets_path":      "<path to VS MSBuild VC v170, Flow C only>"
}
```

> ⚠️ Both envs managed by `uv`. See SKILL.md § "Python Environments".

> ℹ️ Versions managed by `Setup.bat` → `scripts/setup/setup_qairt_env.py`. Do NOT manually install different versions.

> ⚠️ **`onnx`/`tensorflow` have conflicting `protobuf` requirements** — scripts pin compatible versions. Manual installs may break.
> **Symptom**: `ImportError: cannot import name 'builder' from 'google.protobuf.internal'`
> **Fix**: Re-run `Setup.bat`.

### PyTorch / torchvision — `--index-url` Rules

> ⚠️ **CRITICAL**: Rule differs between envs. Wrong usage breaks other installs.

| Environment | torch/torchvision | Other packages |
|-------------|-------------------|----------------|
| `python_x64_venv` (x86_64 3.10) | Standard PyPI — `win_amd64` wheels | Standard PyPI |
| `python_runtime_venv` — aarch64 3.13 on WoS | **MUST** `--index-url https://download.pytorch.org/whl` (ARM64 wheels off-PyPI) | Standard PyPI |
| `python_runtime_venv` — x86_64 3.13 on x64 | Standard PyPI — `win_amd64` wheels | Standard PyPI |

**x86_64 (conversion env):**
```bat
REM No --index-url needed — PyPI has win_amd64 wheels for torch/torchvision
<python_x64_venv>\Scripts\python.exe -m pip install torch torchvision
```

**Runtime venv (`python_runtime_venv`) — torch/torchvision:**
```bat
REM ARM64 Windows wheels NOT on PyPI — must use PyTorch index
data\bin\uv\uv.exe pip install torch torchvision --index-url https://download.pytorch.org/whl

REM All other packages: standard PyPI, no --index-url
data\bin\uv\uv.exe pip install Pillow numpy scipy matplotlib
```

> 💡 `SSLCertVerificationError` on weight download → use PowerShell:
> ```powershell
> Invoke-WebRequest -Uri "https://download.pytorch.org/models/model.pth" -OutFile "model.pth" -UseBasicParsing
> ```
> Re-run export script; torchvision finds cached file.

> 💡 PyTorch 2.x exports ONNX opset 18 (fine for QAIRT 2.45). Large models use `.onnx` + `.onnx.data`; pass `.onnx` path.

---

## QAIRT 2.45+ Tool Path Rules (Windows, both hosts)

> ⚠️ **CRITICAL**: Paths differ from older QAIRT and Linux.

| Step | Tool | Arch dir on `windows-arm64` | Arch dir on `windows-x64` |
|------|------|------------------------------|---------------------------|
| ONNX → DLC (Flow A) | `qairt-converter` | `bin/arm64x-windows-msvc/` (or `x86_64-windows-msvc/` via Prism) | `bin/x86_64-windows-msvc/` |
| DLC → quant DLC | `qairt-quantizer` | same as above | `bin/x86_64-windows-msvc/` |
| DLC → `.bin` | `qnn-context-binary-generator.exe` | `bin/aarch64-windows-msvc/` (real HTP) | `bin/x86_64-windows-msvc/` (HTP functional simulator) |
| ONNX → C++/bin (Flow C, WoS only) | `qnn-onnx-converter` | `bin/x86_64-windows-msvc/` (Python, Prism) | — |
| C++/bin → DLL (Flow C, WoS only) | `qnn-model-lib-generator` | `bin/aarch64-windows-msvc/` (native ARM64) | — |
| Inference | `qai_appbuilder` | `python_runtime_venv` (`.venv_arm64_313`) | `python_runtime_venv` (`.venv_x64_313`) |

Runtime libs (`QnnHtp.dll` / `QnnCpu.dll` / `QnnModelDlc.dll`) resolve from `lib/aarch64-windows-msvc/` on WoS and `lib/x86_64-windows-msvc/` on x64. `run_pipeline.py` prepends the correct dir to `PATH` automatically.

---

## VS ARM64 Environment Requirement

`qnn-model-lib-generator` and `qnn-context-binary-generator.exe` require VS ARM64 build env.

**Rules:**
- `vcvarsall.bat arm64` in **same `.bat` process** — does NOT propagate across `cmd /c` subprocesses
- `VCTargetsPath` → **VS 2022 Community** (NOT BuildTools)
  - ✅ `C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Microsoft\VC\v170\`
  - ❌ `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\...` ← FAILS
- `run_pipeline.bat` handles automatically from `qairt_env.json`

**Template for custom `.bat`:**
```bat
@echo off
REM Read from ${APP_ROOT}\data\config\qairt_env.json -- do NOT hardcode paths
for /f "delims=" %%V in ('powershell -NoProfile -Command "(Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json).vs_vcvarsall"') do set _VCVARSALL=%%V
for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json).vc_targets_path"') do set VCTargetsPath=%%T
for /f "delims=" %%S in ('powershell -NoProfile -Command "(Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json).qairt_sdk_root"') do set QAIRT_SDK_ROOT=%%S
for /f "delims=" %%P in ('powershell -NoProfile -Command "(Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json).python_x64_venv"') do set PYTHON_X64=%%P\Scripts\python.exe

call "%_VCVARSALL%" arm64
set PYTHONPATH=%QAIRT_SDK_ROOT%\lib\python;%PYTHONPATH%
set PATH=%QAIRT_SDK_ROOT%\lib\aarch64-windows-msvc;%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc;%QAIRT_SDK_ROOT%\bin\x86_64-windows-msvc;%PATH%
```

---

## HTP Runtime Files (Context Binary Generation)

`run_pipeline.bat` copies automatically. For manual runs:
```bat
copy %QAIRT_SDK_ROOT%\lib\aarch64-windows-msvc\QnnHtp.dll          <working_dir>\
copy %QAIRT_SDK_ROOT%\lib\hexagon-v73\unsigned\libqnnhtpv73.cat     <working_dir>\
copy %QAIRT_SDK_ROOT%\lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so  <working_dir>\
```

Missing → `loadRemoteSymbols failed` / `DspTransport.openSession qnn_open failed`.

---

## Verification Checklist

```powershell
$cfg = Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json
Test-Path "$($cfg.qairt_sdk_root)\bin\x86_64-windows-msvc\qnn-onnx-converter"     # converter
Test-Path "$($cfg.qairt_sdk_root)\bin\aarch64-windows-msvc\qnn-model-lib-generator" # lib gen
Test-Path "$($cfg.qairt_sdk_root)\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" # ctx gen
echo $cfg.vc_targets_path  # Must contain "Community", NOT "BuildTools"
& "$($cfg.python_x64_venv)\Scripts\python.exe" -c "import onnx; print('conversion env OK')"
& "$($cfg.python_runtime_venv)\Scripts\python.exe" -c "import qai_appbuilder; print('inference env OK')"
```

---

## Common Issues

### `qnn-model-lib-generator` CMake error

**Symptom**: `CMake Error: CMAKE_C_COMPILER not found`
**Cause**: `vcvarsall.bat arm64` not called, or `VCTargetsPath` → BuildTools.
**Fix**: Use `run_pipeline.bat`, or read `vs_vcvarsall`/`vc_targets_path` from `qairt_env.json` (see template).

### `qnn-model-lib-generator` — `BaseOutputPath not set`

**Symptom**:
```
MSBUILD : error MSB1009: Project file does not exist.
C:\...\Microsoft.Common.CurrentVersion.targets: error : The BaseOutputPath/OutputPath property is not set
Configuration='Debug'  Platform='ARM64'
```

**Cause**: `VCTargetsPath` → BuildTools (cannot compile ARM64 DLLs for QNN).
**Fix**: Ensure `vc_targets_path` read correctly:
```bat
for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json).vc_targets_path"') do set VCTargetsPath=%%T
```
**Verify**:
```bat
where MSBuild.exe
REM Should show: C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\...
REM NOT: C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\...
```

### `Unknown Key` warnings (context binary generation)

`[WARNING] Unknown Key: ...` — **non-fatal**. If `.bin` created, proceed.

---

## run_pipeline.bat

Known issues (QAIRT_SDK_ROOT read, DLL search, exit code) all fixed. Use directly.

### `qai_appbuilder` import error

**Symptom**: `ModuleNotFoundError: No module named 'qai_appbuilder'`
**Fix**: Use runtime Python (`python_runtime_venv`; legacy alias `python_arm64_venv`):
```bat
for /f "delims=" %%P in ('powershell -NoProfile -Command "(Get-Content ${APP_ROOT}\data\config\qairt_env.json | ConvertFrom-Json).python_runtime_venv"') do set PYTHON_RUNTIME=%%P\Scripts\python.exe
%PYTHON_RUNTIME% -c "import qai_appbuilder; print('OK')"
```
If fails → re-run `Setup.bat`.

### Host architecture detection (Prism emulation caveat)

`platform.machine()` returns **process arch**, not host arch — a x64 Python under WoS Prism reports `AMD64` on an ARM64 host. **Canonical source: `data/config/host_arch`** (written by `Setup.bat --arch`). WMI fallback: `(Get-WmiObject Win32_Processor).Architecture` (12 = ARM64, 9 = x64). All model-builder scripts read this via `scripts/_host_arch.py`; never re-implement.
---

## Platform SoC Identification (HTP Version Detection)

```powershell
Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
  Where-Object { $_.PSChildName -like "qcadsp*" } |
  Get-ItemProperty | Select-Object PSChildName, ImagePath
```

Read 4-digit code from INF filename in `ImagePath`:

| INF code | Device | `--htp_version` | `--soc_model` |
|:--------:|--------|:---------------:|:-------------:|
| `8380` | Snapdragon X Elite (X1E-80-100) | `v73` | `60` |
| `8480` | Snapdragon X2 Elite (XG102006) | `v73` | `88` |

> ⚠️ **Never `Get-PnpDeviceProperty`** — wakes DSP, blocks 300-400s.
> ⚠️ **Never `Get-WmiObject Win32_SystemDriver`** — WMI throttling → indefinite hang.

---

### Context Binary (Windows)

- `Wrong number of Parameters 5` / `Conv2d failed 3110` = missing VS ARM64 env (NOT operator patching). Fix: `.bat` with `vcvarsall.bat arm64`.
- Copy HTP runtime files to working dir: `QnnHtp.dll`, `libqnnhtpv73.cat`, `libQnnHtpV73Skel.so` (missing → `loadRemoteSymbols failed`).
- VS env same-process only — use `.bat`, not `cmd /c`. Add `--no_simplification` on WoS.

### Inference API (qai_appbuilder)

Full → `references/inference.md`. Key: `QNNConfig.Config(Runtime.HTP, ...)` before `QNNContext`; 2.47 dropped lib-dir arg. Input layout: NCHW if `--preserve_io`. BURST lifecycle: set once/release once. `del model` after use.

## Reference Times (NOT for timeouts)

> All commands MUST use `timeout=0`. Estimates only.

| Operation | Time | Notes |
|-----------|------|-------|
| ONNX export (≤256×256, torch 2.x) | ~10s | No forward pass + `do_constant_folding=False` |
| ONNX export (512×512, torch 2.x) | ~41s | Optimized |
| ONNX export (512×512, torch 1.13) | ~163s | ~275s unoptimized |
| FP16/FP32 conversion | ~30-120s | Per model |
| Context binary generation | ~200s | Per model |
| W8A8 quant (512×512, 2 samples) | ~392s | Scales with size × samples |
| W8A16 quant (256×256, 20 samples) | ~482s | Reference |
| W8A8 quant (256×256, 20 samples) | ~351s | Reference |

> ⚠️ Quantization scales with H×W × sample count (512×512 ≈ 4× of 256×256). Check shape via `qai_inspect_onnxio.py`.

## Runtime Inference venv — Packages & Offline Install

| Package | Status | Notes |
|---------|--------|-------|
| `numpy` | Pre-installed (`vendor\whl\`) | Do not reinstall |
| `opencv-python-headless` | Pre-installed (`vendor\whl\`) | Headless; no `cv2.imshow`. **Never install `opencv-python` GUI — conflicts.** |
| `Pillow` | Via `pip install -e .` | Official `win_arm64`/`win_amd64` cp313 wheels |
| `torch`/`torchvision` | Not pre-installed | See `--index-url` rule above; install only if needed |

> `import cv2`/`from PIL import Image` fails → `Setup.bat` incomplete. Re-run it (don't `pip install opencv-python`).

**Verify**: `<python_runtime_venv>\Scripts\python.exe -c "import qai_appbuilder; print('OK')"`

**Offline install**: `data\bin\uv\uv.exe pip install vendor\whl\qai_appbuilder-*.whl`
