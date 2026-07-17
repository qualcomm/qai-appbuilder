# Inference Reference

`onnxwrapper.py` is a drop-in replacement for `onnxruntime` that routes inference through Qualcomm QAI AppBuilder (`QNNContext`). The `aipc` launcher injects it so existing `onnxruntime`-based scripts run unchanged.

## ⚠️ CRITICAL: Wrapper Flavor Deployment

**Never** transfer `onnxwrapper_x86.py` (or a local `onnxwrapper.py` modified for x86) to a target ARM device.

| File Source | Intended Host | Capabilities | Deployment Path |
|-------------|---------------|--------------|-----------------|
| `onnxwrapper.py` | ARM Windows / ARM Linux | HTP, DSP, CPU (via `qai_appbuilder`) | **DEPLOY TO TARGET** |
| `onnxwrapper_x86.py` | x86_64 Linux | CPU Simulation Only | **DO NOT DEPLOY** |

**Validation**: Before running inference on the target, verify `onnxwrapper.py` contains `import qai_appbuilder`. If it contains `[WARNING] !This is x86 emulation!`, it is the wrong file and HTP initialization will fail.

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

### Linux ARM context-binary wrapper resolution (important)

When using `python aipc ...` with `onnxwrapper.py`, the wrapper auto-selects a QNN artifact by filename priority near the `.onnx` file.
Stale or mismatched files can cause wrong artifact selection and misleading transport errors.

Recommended practice for context-binary mode:

1. Clean stale matched files first:
```bash
cd <workdir>
mkdir -p _ctx_backup
mv -f <model>.so.bin _ctx_backup/ 2>/dev/null || true
mv -f <model>.onnx.so.bin _ctx_backup/ 2>/dev/null || true
mv -f <model>.htp.bin _ctx_backup/ 2>/dev/null || true
```

2. Deploy context binary with ONNX-matching name:
```bash
cp <generated_context>.bin <model>.onnx.so.bin
```

3. Pin runtime libraries explicitly (avoid unintended auto-selected toolchain dir):
```bash
export QAI_QNN_LIBS_DIR="$QAIRT_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"
export LD_LIBRARY_PATH="$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH"
export ADSP_LIBRARY_PATH="$QAIRT_SDK_ROOT/lib/hexagon-v73/unsigned"
```

4. Ensure skel SONAME-compatible alias exists if daemon expects `.so.2`:
```bash
cd "$QAIRT_SDK_ROOT/lib/hexagon-v73/unsigned"
sudo ln -sf libQnnHtpV73Skel.so libQnnHtpV73Skel.so.2
```

5. Run HTP inference through wrapper:
```bash
export QAI_QNN_RUNTIME=HTP
python aipc path/to/inference_script.py
```

If logs show:
- `Failed to load skel, error: 4000`
- `Transport layer setup failed: 14001`
- `Failed to parse platform config: 14001`

first verify wrapper-selected model filename and `QAI_QNN_LIBS_DIR` path before changing model or quantization flow.

### Acceptance environment snapshot (recommended)

Before final remote acceptance run, record the effective runtime environment.
For Linux targets, use:
```bash
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

Do not declare final acceptance pass if required fields are missing.
On non-Linux targets, capture equivalent runtime/environment fields with platform-appropriate commands.

### Runtime libs consistency check (platform-aware)

Before final acceptance, verify runtime core libraries are resolved from the same intended runtime stack.

- Linux targets:
  - `libQnnHtp.so`
  - `libQnnSystem.so`
- Windows targets:
  - corresponding QNN runtime core libraries from the same runtime family
    (for example, avoid mixing different runtime family/toolchain directories).

Linux example:
```bash
export LD_DEBUG=libs
timeout 15 python -u aipc onnx_inference.py > lddebug.log 2>&1 || true
grep -E 'libQnnHtp\\.so|libQnnSystem\\.so' lddebug.log
```

If resolved parent directories/toolchain families differ, treat it as mixed runtime stack.
Do not declare acceptance pass until runtime path alignment is fixed.

### Preflight checklist (final acceptance)

- Confirm wrapper-selected artifact is intended QNN artifact (not `.onnx`).
- Confirm runtime core libraries resolve from a single intended runtime stack.
- Confirm acceptance environment snapshot is complete and saved.

If any item fails, fix preflight first, then rerun acceptance.

> **⚠️ IMPORTANT**: Pass the `.onnx` file path to `InferenceSession`. The wrapper searches for a matching QAIRT model file **in the same directory**. The QAIRT model **must** exist with the correct naming — if not found, loading will fail. See [Model File Resolution](#model-file-resolution) below.

**Debugging**: the same inference script can be run with `python aipc script.py` (QAIRT via `onnxwrapper`) or with `python script.py` (standard ONNX via `onnxruntime`) to compare outputs between QAIRT and ONNX baseline.


## CRITICAL: Always Use `aipc` Launcher - Never Call `QNNContext` Directly

**Guardrail**: All inference, including final acceptance, **must** go through the `aipc` launcher with `onnxwrapper.py`. Direct use of `qai_appbuilder.QNNContext` is forbidden for production inference.

```bash
# ✅ Correct
python aipc infer_model.py

# ❌ Forbidden — bypasses output reordering, YAML name mapping, and preflight checks
from qai_appbuilder import QNNContext
ctx = QNNContext(model_path=..., backend_lib_path=..., system_lib_path=...)
ctx.Inference([...])
```

### Why: QAIRT may reorder output tensors

`QNNContext.Inference` returns tensors in the **HTP compiled graph's internal layout order**, which is determined at context-binary generation time and is **not guaranteed to match the ONNX `output_names` order**.

Example — whisper-tiny decoder:

| Source | Output order |
|--------|-------------|
| ONNX definition | `[logits, sa_present×8, ca_present×8]` |
| `QNNContext.Inference` raw return | `[ca_present×8, sa_present×8, logits]` |

If you index raw `QNNContext` output by position (e.g. `outs[0]` for logits), you will silently read the wrong tensor.

### How `onnxwrapper` fixes this

The wrapper reads the ONNX-inspected `.yaml` file (generated by `aipc_inspect_onnxio.py`) to recover the original ONNX output order, then remaps:

```python
out_map = {name: tensor for name, tensor in zip(qnn_output_names, raw_outs)}
ordered  = [out_map[name] for name in onnx_output_names]   # ONNX order restored
```

This means your inference script always receives outputs in the same order as `onnxruntime` would return them — regardless of how QAIRT internally reordered them.

### Prerequisite: `.yaml` file must be present

The reorder depends on the `.yaml` file produced by `aipc_inspect_onnxio.py`. Ensure it is deployed alongside the `.onnx` file on the target:

```bash
# On host — already generated during Phase 2
ls *.yaml   # whisper-tiny-encoder.yaml, whisper-tiny-decoder.yaml, ...

# Deploy to target alongside .onnx
scp whisper-tiny-decoder.onnx whisper-tiny-decoder.yaml ubuntu@target:/workdir/
```

If the `.yaml` is missing, the wrapper falls back to QNN internal order — outputs may be silently mismatched.

### Exception: `QNNConfig.Config` API compatibility

`onnxwrapper.py` detects the `qai_appbuilder` API shape at import time and calls `QNNConfig.Config` with explicit keyword arguments for both old and new API variants. If the wrapper fails to initialize due to a `QNNConfig.Config` signature mismatch, **fix the wrapper** — do not work around it by switching to direct `QNNContext`.

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

## Input/Output Name Formats

The QNN runtime uses underscore-separated names internally (e.g. past_key_values_0_key),
while the original ONNX model uses dot-separated names (e.g. past_key_values.0.key).
The wrapper handles both formats transparently.

**Name resolution in get_input_names() / get_inputs():**

The wrapper returns ONNX dot-format names when a matching .yaml file exists
next to the ONNX model (generated by ipc_inspect_onnxio.py). Otherwise it
falls back to QNN underscore format.

`python
session = ort.InferenceSession("model.onnx")
names = session.get_input_names()
# With YAML:    ['input_ids', 'past_key_values.0.key', ...]
# Without YAML: ['input_ids', 'past_key_values_0_key', ...]
`

**Input feed accepts both formats:**

session.run() normalizes dot-format names to underscore internally:

`python
# Both work:
inputs = {"past_key_values.0.key": data}    # ONNX dot format
inputs = {"past_key_values_0_key": data}    # QNN underscore format
`

**Recommendation:** Always run ipc_inspect_onnxio.py after ONNX export to
generate the .yaml file. This ensures consistent dot-format names throughout.

## Output Tensor Ordering

QNN may return output tensors in a different order than the ONNX model.
The wrapper reorders outputs to match the ONNX spec using the YAML config.

**How reordering works:**

1. QNN returns tensors in its internal order (e.g., all KV tensors first, logits last)
2. The wrapper builds a name-to-tensor map: out_map = {qnn_name: tensor}
3. Outputs are reordered to match the ONNX YAML order: [out_map[name] for name in yaml_output_names]

**Source of output order:**

| Priority | Source | Example order |
|----------|--------|---------------|
| 1 | ONNX-inspected .yaml (from ipc_inspect_onnxio.py) | logits first |
| 2 | Autogen YAML (from QNNContext IO specs) | logits last |
| 3 | QNN internal order (getOutputName()) | logits last |

**Do not rely on positional indices.** Always verify output order:

`python
session = ort.InferenceSession("model.onnx")
out_names = session.get_output_names()
print(out_names)  # Check order before indexing

outputs = session.run(None, inputs)
logits_idx = out_names.index("logits")
logits = outputs[logits_idx]
`

**If outputs are in the wrong order:**

- Ensure <model>.yaml exists next to the ONNX file (run ipc_inspect_onnxio.py)
- Check the YAML output list matches the expected ONNX order
- The wrapper matches ONNX dot names to QNN underscore names by converting dots to underscores


## Multiple Models in One Process

`qai_appbuilder.QNNContext` uses a shared global backend state internally.
When multiple `QNNContext` instances are created simultaneously in the same process,
each new instance overwrites the previous one's backend handle —
`getInputName()` / `getOutputName()` will only reflect the **last loaded model**.

This is a known `qai_appbuilder` design constraint, not a bug in `onnxwrapper`.
Changing `qai_appbuilder` internals is not recommended — it is an official Qualcomm package
that gets overwritten on every QAIRT version upgrade, and the backend handle management
involves C extensions where incorrect changes risk crashes or memory leaks.

The correct solution is to manage model lifetime explicitly at the `onnxwrapper` / `InferenceSession` layer.

### Rule: Sequential Load → Run → Release

For multi-model pipelines (e.g. encoder-decoder), never hold multiple `InferenceSession`
instances open at the same time. Load one model, run it, release it, then load the next.

```python
# ✅ Correct — sequential, one context at a time
enc_sess = ort.InferenceSession("encoder.onnx")
enc_out  = enc_sess.run(None, {"input_features": mel})
del enc_sess                                        # release before loading next

ckv_sess = ort.InferenceSession("cross-kv.onnx")
ca_kv    = ckv_sess.run(None, {"encoder_hidden_states": enc_out[0]})
del ckv_sess

dec_sess = ort.InferenceSession("decoder.onnx")    # keep open for decode loop
for token in decode_loop:
    out = dec_sess.run(None, {...})
del dec_sess

# ❌ Wrong — all three open simultaneously
enc_sess = ort.InferenceSession("encoder.onnx")
ckv_sess = ort.InferenceSession("cross-kv.onnx")   # overwrites enc_sess backend state
dec_sess = ort.InferenceSession("decoder.onnx")    # overwrites ckv_sess backend state
# enc_sess.get_inputs() now returns decoder's inputs — silently wrong
```

### Exception: decoder loop

It is fine to keep a single `InferenceSession` open across multiple `.run()` calls
(e.g. the decoder session across all decode steps). The constraint is only about
**simultaneous** instances, not repeated calls on the same instance.

### InferenceSession cleanup

`InferenceSession.__del__` calls `del self._model` which triggers `QNNContext.release()`.
Explicit `del sess` or letting the session go out of scope is sufficient.
No manual `.release()` call is needed when using `InferenceSession`.

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

> ⚠️ For Linux ARM targets where deployment uses `aarch64-oe-linux-gcc11.2` runtime libs, set `QAI_QNN_LIBS_DIR` explicitly to that directory to avoid unintended auto-resolution.

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

## PyTorch + QNN Runtime Conflict

**Do not import PyTorch and onnxruntime (via aipc) in the same Python process.**
The QNN runtime and PyTorch's C-level memory allocator conflict, causing a heap
corruption crash with exit code 
**Symptoms:**

- Script exits silently with no Python traceback
- Exit code -1073740940 (- Crash occurs during onnxwrapper import or first session.run() call

**Why this happens:** Both libraries initialize custom C memory allocators that
assume exclusive ownership of the process heap. When loaded together, one
library's allocation can corrupt the other's internal state.

**Proper approach: use ONNX + onnxwrapper together.**

The aipc wrapper is designed to work with ONNX models through onnxruntime.
For validation and debugging that requires comparing PyTorch outputs against QNN
outputs, use ONNX Runtime (CPU) as the bridge — not PyTorch directly:

`python
# Validation script — ONNX Runtime CPU vs QNN HTP (no PyTorch)
import numpy as np
import onnxruntime as ort

# Run on CPU via standard onnxruntime (not through aipc)
session_cpu = ort.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])
outputs_cpu = session_cpu.run(None, inputs)

# Run on HTP via aipc wrapper
# python aipc infer.py  (onnxruntime hot-patched to QNN)
outputs_htp = session_htp.run(None, inputs)

# Compare
cos_sim = np.dot(outputs_cpu[0].flatten(), outputs_htp[0].flatten()) / (
    np.linalg.norm(outputs_cpu[0]) * np.linalg.norm(outputs_htp[0])
)
`

**Split-script workaround (debug only):**

If you must compare against PyTorch directly (e.g., during initial bring-up),
run prefill and decode in separate processes with disk-based KV cache handoff.
This is a debugging convenience, not the recommended production workflow.

`bash
# Debug flow only — not for production
python prefill.py          # PyTorch only, saves KV cache to disk
python aipc decode_only.py # QNN only, loads KV cache from disk
`

## Transformer Model Target Compatibility (Pre- and Post-processing)

When running end-to-end inference on the remote target for **Transformer-based structures** (such as Whisper speech-to-text or LLMs), the CPU is responsible for Pre-processing (converting audio/text into tensors) and Post-processing (decoding token IDs back to text). Since the target device's Python environment may run a different `transformers` version (e.g., v4.x) than the host (e.g., v5.x), loading local weights directly can crash the pipeline before the NPU execution starts.

### Known Version Pitfalls:
1. **Processor Config Clash**: Older `transformers` versions (like 4.x) look for `preprocessor_config.json` inside the local weights folder, whereas newer versions (5.x) may generate only `processor_config.json`. Symlinking `processor_config.json` to `preprocessor_config.json` is a common pitfall because the processor config contents clash with the feature extractor constructor, throwing:
   `TypeError: WhisperProcessor.__init__() got multiple values for argument 'feature_extractor'`
2. **Tokenizer BPE Loading**: In older versions, loading a BPE tokenizer from a directory without legacy `vocab.json` and `merges.txt` will raise a `TypeError` (e.g., expecting a string or PathLike but getting NoneType) because they try to initialize a legacy slow python tokenizer instead of the fast tokenizer.

### Required Mitigation Steps during Deployment:
- **Download Legacy Assets**: If the target environment uses an older library, download the official model-specific `vocab.json`, `merges.txt`, and `preprocessor_config.json` files from Hugging Face and place them inside the local weights folder before transferring to the target.
- **Robust Manual Component Loading**: Instead of using the high-level `Processor.from_pretrained(local_dir)` directly (which might fail due to parameter signature clashes in `processor_config.json`), load the component parts separately and instantiate:
  ```python
  from transformers import WhisperFeatureExtractor, WhisperTokenizer, WhisperProcessor
  fe = WhisperFeatureExtractor.from_pretrained(model_dir)
  tok = WhisperTokenizer.from_pretrained(model_dir)
  processor = WhisperProcessor(feature_extractor=fe, tokenizer=tok)
  ```
  This manual assembly pattern is highly robust and fully compatible across both `transformers` 4.x and 5.x.

## Validation Checklist

- [ ] Input tensor name/shape matches model
- [ ] Preprocessing matches training/export assumptions
- [ ] Output tensor mapping is correct (check autogen YAML if wrong)
- [ ] Cosine similarity vs ONNX CPU baseline >= 0.99 (FP) / >= 0.95 (INT8)
- [ ] Latency / FPS collected on target runtime
