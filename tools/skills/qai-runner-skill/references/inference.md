# Inference Reference

`onnxwrapper.py` is a drop-in replacement for `onnxruntime` that routes inference through Qualcomm QAI AppBuilder (`QNNContext`). The `aipc` launcher injects it so existing `onnxruntime`-based scripts run unchanged.

## ⚠️ CRITICAL: Context Binary & Model Library Architecture

**Context binary requirements vary by platform:**

| Target Platform | Context Binary | Model Library Target | Can use `.so`/`.dll` directly? |
|-----------------|----------------|---------------------|-------------------------------|
| **ARM Windows** (x86_64 emulated Python) | **PREFERRED** | `windows-x86_64` | ✅ YES — ARM64X/CHPE `.dll` loads in emulated Python |
| **ARM Windows** (native ARM64 Python) | **PREFERRED** | `windows-aarch64` | ✅ YES — native `.dll` loads directly |
| **ARM Linux** | **OPTIONAL** | `aarch64-ubuntu-gcc9.4` | ✅ YES — `.so` works directly |
| x86 Linux | N/A (CPU-only) | `x86_64-linux-clang` | ✅ YES — use x86 wrapper |

**Key principle — the model library (.dll/.so) must match the Python process architecture, not the CPU:**
- On ARM64 Windows, the QAIRT venv Python is typically x86_64 emulated (`platform.machine()` = `AMD64`)
- The model library must be compiled for `windows-x86_64` so the emulated Python can load it
- The QNN runtime DLLs (`QnnHtp.dll`, etc.) are ARM64X (CHPE) hybrid binaries that bridge x86_64→native HTP
- If using a native ARM64 Python, compile the model library for `windows-aarch64` instead

**If context binary generation failed:**
- **Windows**: → Continue with `.dll` direct path. ARM64X/CHPE runtime DLLs loaded via `qai_appbuilder`
  can execute HTP inference without a context binary.
- **Linux**: → Do NOT immediately fallback to `.so`.
  - First, you MUST exhaust host-context troubleshooting in `references/host_context_binary_gen.md`:
    - validate correct `soc_id`/`dsp_arch`
    - sweep `vtcm_mb=0,1,2,3,4,8`
    - try applicable `soc_id`/`dsp_arch` candidates and `htp_arch`/no-`soc_id` path when needed
  - Only after all applicable methods fail with recorded logs may you proceed with `.so` library directly.
- **Alternative**: Try SNPE flow (`.dlc`) if QNN HTP is incompatible

Linux cross-host/cross-arch clarification:
- If context-binary generation fails while targeting Linux from a different host architecture, you may skip context-binary and run inference with `.so` only after the required host-context troubleshooting above is completed.
- Record the skip reason in the project issue log.

> **⚠️ IMPORTANT**: Pass the `.onnx` file path to `InferenceSession`. The wrapper searches for a matching QAIRT model file **in the same directory**. The QAIRT model **must** exist with the correct naming — if not found, loading will fail. See [Model File Resolution](#model-file-resolution) below.

**Debugging**: the same inference script can be run with `python aipc script.py` (QAIRT via `onnxwrapper`) or with `python script.py` (standard ONNX via `onnxruntime`) to compare outputs between QAIRT and ONNX baseline.

## Usage

```bash
# Copy wrapper scripts into the working folder, then run:
python aipc path/to/inference_script.py
```

### Target Device Inference over SSH

Inference can also be run directly on the target device over SSH. Before launching inference, you **must** source the QAIRT setup script on the target device.

This setup script path is **user-provided** (it is environment-specific) and typically performs tasks such as:
- Exporting required environment variables (e.g., `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `QNN_SDK_ROOT`, etc.)
- Activating a Python virtual environment (if your workflow uses one)
- Initializing QAIRT/QNN runtime environment

Example:

```bash
ssh ubuntu@<target-ip>
. /home/ubuntu/aienv.sh
python aipc path/to/inference_script.py
```

### Linux ARM HTP Environment (manual export only)

If HTP initialization fails, set runtime environment variables in the current shell first.
Do not assume a fixed SoC ID or DSP arch; use values provided by the device owner.

```bash
# Required SDK root (typically set by your environment setup)
export QAIRT_SDK_ROOT=/path/to/qairt/<version>
export QNN_SDK_ROOT="${QNN_SDK_ROOT:-$QAIRT_SDK_ROOT}"

# Device-specific values (must match target hardware)
export PRODUCT_SOC=<soc_id>
export DSP_ARCH=<dsp_arch>

# DSP and runtime library paths
export ADSP_LIBRARY_PATH="$QNN_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned"
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"
```

Then run inference normally:

```bash
python aipc path/to/inference_script.py
```

If logs show:
- `Stub lib id mismatch: expected ..., detected ...`
- `Failed to create transport ... error: 1008`

then check:
1. `QAIRT_SDK_ROOT` points to intended version.
2. `QNN_SDK_ROOT` is aligned with `QAIRT_SDK_ROOT`.
3. `PRODUCT_SOC` and `DSP_ARCH` are correct for the target device.
4. `ADSP_LIBRARY_PATH` points to matching `hexagon-v${DSP_ARCH}`.
5. `LD_LIBRARY_PATH` includes target ARM64 runtime libs from the same SDK.
6. No older QNN/HTP libraries appear earlier in search paths.

Quick verification:

```bash
echo "QAIRT_SDK_ROOT=$QAIRT_SDK_ROOT"
echo "QNN_SDK_ROOT=$QNN_SDK_ROOT"
echo "PRODUCT_SOC=$PRODUCT_SOC"
echo "DSP_ARCH=$DSP_ARCH"
echo "ADSP_LIBRARY_PATH=$ADSP_LIBRARY_PATH"
echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
```

Notes:
- Do not hardcode `PRODUCT_SOC=9075` or `DSP_ARCH=73` in shared docs; these are platform-specific examples only.
- Keep placeholders (`<soc_id>`, `<dsp_arch>`) in reusable instructions.

If you invoke commands remotely through SSH in a single line, source the setup script first in the same shell session:

```bash
ssh ubuntu@<target-ip> '. /home/ubuntu/aienv.sh && python aipc path/to/inference_script.py'
```

### x86 Host Inference (ONNX Wrapper Variant)

For x86 inference, use the x86-specific wrapper source file and place it in your project as `onnxwrapper.py`:

```bash
# From skill scripts folder to project folder:
cp skills/aipc-toolkit/scripts/onnxwrapper_x86.py ./onnxwrapper.py
cp skills/aipc-toolkit/scripts/aipc ./
python aipc path/to/inference_script.py
```

This keeps your inference script unchanged (`import onnxruntime as ort`) while routing execution through the x86-compatible QAIRT wrapper.

Note for x86 wrapper behavior:
- `onnxwrapper_x86.py` is CPU-only by design for stable host execution.
- Runtime selection like `QAI_QNN_RUNTIME=HTP` is ignored by this wrapper.
- Recommended usage remains simply:
```bash
python aipc path/to/inference_script.py
```

Inference script uses standard `onnxruntime` API — pass the `.onnx` path; the wrapper resolves the QNN model automatically:

```python
import onnxruntime as ort
sess = ort.InferenceSession("model.onnx")
outputs = sess.run(None, {"input_name": input_tensor})
```

## ARM64X (CHPE) Model File Resolution on ARM64 Windows

On ARM64 Windows where Python runs under x86_64 emulation:

1. **QNN runtime DLLs** (`QnnHtp.dll`, `QnnHtpPrepare.dll`, etc.) are loaded from
   `qai_appbuilder/libs/` (bundled ARM64X hybrid format) or from
   `$QAIRT_SDK_ROOT/lib/arm64x-windows-msvc/` if explicitly configured.
   ARM64X DLLs contain both x64 and ARM64 code — Windows loads the correct path
   automatically based on the process architecture.

2. **Model library DLL** (`esrgan.dll`) must be **x86_64** (`windows-x86_64` target)
   because the Python process is x86_64 emulated. ARM64-native DLLs cannot be loaded
   by an x86_64 process.

3. **Context binary** (`.dll.bin`) is SoC-specific and platform-independent — the same
   `.bin` works whether generated from an x86_64 or ARM64 host.

4. **ADSP_LIBRARY_PATH** should point to both the native ARM64 stub library directory and the Hexagon skel library directory for your specific SoC. You can automatically construct this dynamically using the `aipc_qairt_devinfo.ps1` script:
   ```powershell
   # Dynamically detect local DSP architecture (SoC-agnostic)
   # Replace <path_to_skills> with the actual path to your active skills directory
   $devInfo = & "<path_to_skills>/aipc-toolkit/scripts/aipc_qairt_devinfo.ps1" -Json | ConvertFrom-Json
   $env:ADSP_LIBRARY_PATH = "$env:QAIRT_SDK_ROOT\lib\$($devInfo.DspArch)\unsigned;$env:QAIRT_SDK_ROOT\lib\aarch64-windows-msvc"
   ```
   This ensures both the Windows-side stub DLLs (e.g., `QnnHtpV73Stub.dll`) and Hexagon-side skel libraries (e.g., `libQnnHtpV73Skel.so`) are correctly loaded.

**Summary of file roles on ARM64 Windows:**

| File | Arch | Source |
|------|------|--------|
| `esrgan.dll` (model lib) | x86_64 | `qnn-model-lib-generator -t windows-x86_64` |
| `esrgan.dll.bin` (context bin) | SoC-specific | `qnn-context-binary-generator` |
| `QnnHtp.dll` (runtime) | ARM64X/CHPE | `qai_appbuilder/libs/` or SDK `arm64x-windows-msvc/` |
| `QnnHtpV73Stub.dll` (skel) | ARM64 | SDK `lib/aarch64-windows-msvc/` |

## Model File Resolution

Given an `.onnx` path, the wrapper searches for the QNN model in this order:

**Linux**: `model.htp.bin`→ `model.so.bin` → `model.so` → `libmodel.htp.bin`  → `libmodel.so.bin` → `libmodel.so` → `model.bin` → `libmodel.bin`

**Windows**: `model.htp.bin`→ `model.dll.bin` → `libmodel.htp.bin` → `libmodel.dll.bin` → `libmodel.dll` → `model.bin`  → `libmodel.bin`

Any file ending in `.bin` (including `.so.bin`, `.dll.bin`) is treated as a context binary (`--retrieve_context`).

### Practical Example

If your script loads `esrgan.onnx`, copy the context binary to match:

```powershell
# After conversion produces qairt_output\esrgan.dll.bin
# Copy to match ONNX naming:
Copy-Item qairt_output\esrgan.dll.bin .\esrgan.onnx.dll.bin
# OR
Copy-Item qairt_output\esrgan.dll.bin .\esrgan.dll.bin

# Now aipc can find the QNN model:
python aipc inference.py
```

## IO Config YAML

QNN may reorder I/O relative to the original ONNX. The wrapper uses a YAML to remap names, dtypes, and layouts so outputs are returned in the correct ONNX order.

Search order (first found wins): `QAI_IO_CONFIG` env → `{model_wo_ext}.yaml` → `{model_wo_ext}.autogen.yaml` → `{model_name}.{runtime}.autogen.yaml` → `{model_name}.yaml`

If no YAML is found, one is auto-generated from `QNNContext` IO specs and saved as `{model_name}.{runtime}.autogen.yaml`. Inspect it if outputs are wrong.

```yaml
inputs:
  - name: images
    dtype: float32
    layout: NCHW      # triggers NCHW→NHWC before inference
    add_batch: true
outputs:
  - name: output0
    dtype: float32
    layout: NCHW      # triggers NHWC→NCHW after inference
```

## Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `QAI_QNN_RUNTIME` | `HTP` | `HTP` or `CPU` |
| `QAI_IO_CONFIG` | — | Explicit path to IO YAML |
| `QAI_IO_AUTOGEN_SAVE` | `1` | Save auto-generated YAML (`0` to disable) |
| `QAI_QNN_LIBS_DIR` | auto | Override QNN libs dir (see note below) |
| `QAIRT_SDK_ROOT` | — | When set, `SessionOptions` auto-resolves the correct libs dir |

### QNN libs dir resolution order (`SessionOptions`)

`SessionOptions` resolves `qnn_libs_dir` in this priority order:

1. `QAI_QNN_LIBS_DIR` env var (explicit override)
2. `QAIRT_SDK_ROOT/lib/<toolchain>` — derived from `QAIRT_SDK_ROOT` env var
   - `aarch64` Linux → `aarch64-ubuntu-gcc9.4`
   - `x86_64` Linux → `x86_64-linux-clang`
   - Windows → `arm64x-windows-msvc` (preferred, works on both x86_64 and ARM64) or `x86_64-windows-msvc` (fallback)
3. `qai_appbuilder/libs/` — bundled libs (fallback)

> ⚠️ **ARM64 Windows toolchain detection (onnxwrapper.py:1482):**
> On Windows, the wrapper now checks for `arm64x-windows-msvc` first. If it exists
> (ARM64 Windows with CHPE support), it is used — these ARM64X hybrid DLLs work from
> both x86_64-emulated and ARM64-native Python processes. If it doesn't exist (pure
> x86_64 Windows), it falls back to `x86_64-windows-msvc`.
>
> The pure x86_64 `QnnHtp.dll` from `x86_64-windows-msvc` cannot access HTP hardware
> on ARM64 because x86_64 emulation doesn't forward NPU driver ioctls. The ARM64X hybrid
> `QnnHtp.dll` contains both x64 and ARM64 code paths — Windows loads the correct one
> automatically (the ARM64X variant is significantly smaller because it uses CHPE thunks
> instead of bundling a full x64 implementation).

> ⚠️ **Always source the QAIRT env script before running inference.**
> The `qai_appbuilder` package bundles its own `libQnnHtp.so` which may be a
> different version than the QAIRT SDK used to compile the model/context binary.
> If `QAIRT_SDK_ROOT` is not set, `SessionOptions` falls back to the bundled libs,
> causing an ABI mismatch that segfaults in C extension getter calls
> (`getGraphName`, `getInputName`, etc.) when loading a context binary.

## Validation Checklist

- [ ] Input tensor name/shape matches model
- [ ] Preprocessing matches training/export assumptions
- [ ] Output tensor mapping is correct (check autogen YAML if wrong)
- [ ] Cosine similarity vs ONNX CPU baseline ≥ 0.99 (FP) / ≥ 0.95 (INT8)
- [ ] Latency / FPS collected on target runtime
