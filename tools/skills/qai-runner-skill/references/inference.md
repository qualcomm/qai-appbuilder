# Inference Reference

`onnxwrapper.py` is a drop-in replacement for `onnxruntime` that routes inference through Qualcomm QAI AppBuilder (`QNNContext`). The `aipc` launcher injects it so existing `onnxruntime`-based scripts run unchanged.

## ⚠️ CRITICAL: Context Binary REQUIRED

**Context binary requirements vary by platform:**

| Target Platform | Context Binary | Can use `.so`/`.dll` directly? |
|-----------------|----------------|-------------------------------|
| **ARM Windows** | **REQUIRED** — `{model}.dll.bin` | ❌ NO — `.dll` alone will NOT load |
| **ARM Linux**   | **OPTIONAL** — `{model}.so.bin` | ✅ YES — `.so` works directly |
| x86 Linux       | N/A (CPU-only) | ✅ YES — use x86 wrapper |

**Never assume:**
- ❌ "The DLL can be used directly on Windows without context binary"
- ❌ "Context binary is required on Linux"

**If context binary generation failed:**
- **Windows**: → **Blocking Condition B8** — Do NOT proceed to inference
- **Linux**: → Can proceed with `.so` library directly
- **Alternative**: Try SNPE flow (`.dlc`) if QNN HTP is incompatible

Linux cross-host/cross-arch clarification:
- If context-binary generation fails while targeting Linux from a different host architecture, skip context-binary and run inference with `.so`.
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

## Validation Checklist

- [ ] Input tensor name/shape matches model
- [ ] Preprocessing matches training/export assumptions
- [ ] Output tensor mapping is correct (check autogen YAML if wrong)
- [ ] Cosine similarity vs ONNX CPU baseline ≥ 0.99 (FP) / ≥ 0.95 (INT8)
- [ ] Latency / FPS collected on target runtime
