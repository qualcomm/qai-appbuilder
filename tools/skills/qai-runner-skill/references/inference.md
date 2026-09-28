# Inference Reference

`onnxwrapper.py` is a drop-in replacement for `onnxruntime` that routes inference through Qualcomm QAI AppBuilder (`QNNContext`). The `qai_runner.py` launcher injects it so existing `onnxruntime`-based scripts run unchanged.

For direct `qai_appbuilder` inference (WoS ARM64), use the scripts in `scripts/inference/`.

> ## 🚨 `onnxruntime` Rules (MANDATORY)
>
> | Scenario | Allowed EP | Note |
> |------|-----------|------|
> | Accuracy/perf comparison (ONNX vs QNN) | `CPUExecutionProvider` ✅ | Only allowed use of `onnxruntime` |
> | NPU inference | `qai_appbuilder` / `QNNContext` ✅ | Standard inference tool |
> | Running model on NPU via onnxruntime | ❌ **Forbidden** | Always use `qai_appbuilder` / `QNNContext` |
>
> **Principle**: `onnxruntime` = CPU baseline comparison ONLY. NPU inference = `qai_appbuilder` / `QNNContext`.
>
> 🚨 **ONNX CPU baseline MUST run in a separate process from QNN inference.** `qai_runner.py` hot-swaps `sys.modules["onnxruntime"]` — any later `import onnxruntime` in the same process gets the QNN wrapper. Standard: **Process A** runs ONNX CPU → saves `.npy`; **Process B** (ARM64 + qai_runner) runs QNN → saves `.npy`; **Process C** computes cosine.
>
> 🚨 **Deeper root cause (not just the module hot-swap):** importing PyTorch and loading a `QNNContext` (directly, or via `qai_runner.py`) in the **same process** risks a crash even without the `sys.modules` swap above — PyTorch's C-level tensor allocator and the QNN HTP runtime's allocator both assume exclusive ownership of the process heap. Symptom: the process exits silently with **no Python traceback**, typically exit code `-1073740940` (`0xC0000005`), during `import torch` (if QNN libs are already loaded) or at the first `Inference()` / `session.run()` call. Treat this as a hard separate-process rule — if you need a PyTorch reference output alongside QNN, run PyTorch in **Process A** (save `.npy`) and QNN in **Process B**, same as the ONNX-baseline pattern above.

> 🚨 **PyTorch source I/O contract and picture-output rule (MANDATORY):** Generated inference code MUST match the existing PyTorch source implementation's input and output contract: preprocessing, tensor layout, shape, dtype, value range, output names/order, shape, dtype, and post-processing semantics. Do not invent a new input/output interface or change the source behavior to fit QNN. For normal picture inference, write the source-level result (for example an image, labels, or detections) in the expected format; do **not** generate `.npy` files. `.npy` files are debug/accuracy-comparison artifacts only and may be written only when explicitly needed for debugging or validation, clearly marked as such, and never treated as the user-facing picture output.

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

When using `python qai ...` with `onnxwrapper.py`, the wrapper auto-selects a QNN artifact by filename priority near the `.onnx` file.
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
python qai path/to/inference_script.py
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
timeout 15 python -u qai onnx_inference.py > lddebug.log 2>&1 || true
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

**Debugging**: the same inference script can be run with `python qai script.py` (QAIRT via `onnxwrapper`) or with `python script.py` (standard ONNX via `onnxruntime`) to compare outputs between QAIRT and ONNX baseline.


## CRITICAL: Always Use `qai` Launcher - Never Call `QNNContext` Directly

**Guardrail**: All inference, including final acceptance, **must** go through the `qai` launcher with `onnxwrapper.py`. Direct use of `qai_appbuilder.QNNContext` is forbidden for production inference.

```bash
# Correct
python qai infer_model.py

# Forbidden - bypasses output reordering, YAML name mapping, and preflight checks
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

The wrapper reads the ONNX-inspected `.yaml` file (generated by `qai_inspect_onnxio.py`) to recover the original ONNX output order, then remaps:

```python
out_map = {name: tensor for name, tensor in zip(qnn_output_names, raw_outs)}
ordered  = [out_map[name] for name in onnx_output_names]   # ONNX order restored
```

This means your inference script always receives outputs in the same order as `onnxruntime` would return them — regardless of how QAIRT internally reordered them.

### Prerequisite: `.yaml` file must be present

The reorder depends on the `.yaml` file produced by `qai_inspect_onnxio.py`. Ensure it is deployed alongside the `.onnx` file on the target:

```bash
# On host — already generated during Phase 2
ls *.yaml   # whisper-tiny-encoder.yaml, whisper-tiny-decoder.yaml, ...

# Deploy to target alongside .onnx
scp whisper-tiny-decoder.onnx whisper-tiny-decoder.yaml ubuntu@target:/workdir/
```

If the `.yaml` is missing, the wrapper falls back to QNN internal order — outputs may be silently mismatched.

### Exception: `QNNConfig.Config` API compatibility

`onnxwrapper.py` targets **one** API shape: the qai_appbuilder 2.47 signature
`QNNConfig.Config(runtime, log_level, profiling_level)`, called with three **positional**
arguments inside `InferenceSession`. The older 2.46 form took a leading libs-dir argument;
that argument is gone, so the wrapper instead only prepends `qnn_libs_dir` to `PATH`
(`_ensure_path_contains`) so the QNN DLLs resolve. There is **no import-time API-shape
detection and no keyword-argument fallback** — a wrapper built against a different
qai_appbuilder version will simply raise on this call.

If the wrapper fails to initialize due to a `QNNConfig.Config` signature mismatch,
**fix the wrapper** — do not work around it by switching to direct `QNNContext`.


## ⚠️ CRITICAL: Model File Format and Context Binary

**qai_appbuilder supports three model formats (in priority order):**

| Format | Platform | Notes |
|--------|----------|-------|
| `.bin` (context binary) | All hosts (ARM64 hosts load on real HTP; x64 hosts — see `x64-host-notes.md`) | **Best perf on real-HTP hosts** — HTP-optimized, pre-compiled offline. Portable across HTP backend builds (routing doc §4). |
| `.dlc` | All hosts | SNPE/DLC; **supported directly** — QNNContext compiles on first load. **Portable** across HTP versions/devices. On x64 hosts see `x64-host-notes.md` for local loading rules. |
| `.dll` / `.so` | `windows-arm64` (`.dll`) / `linux-aarch64` (`.so`) only | Compiled model lib; no HTP optimization cache; blocked on x64 hosts. |

> ℹ️ **DLC direct load behavior (verified on QAIRT 2.45 WoS):** `QNNContext` loading
> a `.dlc` file does on-the-fly graph compilation (equivalent to running
> `qnn-context-binary-generator` internally). Inference results are **numerically
> identical** to the corresponding `.bin` (cosine ≈ 1.000000); `.bin` is ~21-27%
> faster at p50 (Inception-V3 W8A8 measured) because it skips compilation.
>
> Cold-start may print these non-fatal warnings (safe to ignore):
> ```
> input_data_type: float, output_data_type: float
> warmup_parallel_stl
> ```

### Format selection (`.bin` vs `.dlc`) — when which

**Decision priority** (top-down):
1. **User specifies format** → honor it.
2. **Same machine (ARM64 host == target)** → `.bin` (best p50).
3. **Cross-target** (different HTP versions/devices) → `.dlc` (portable; target compiles on first load).
4. **x64 hosts** → `.dlc` or `.bin` (loading rules and backend selection see `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md`). Real-device HTP performance → run on ARM64 hosts or push via ADB.

> 💡 `.dlc` direct load = same numerical result as `.bin`, just slower first run (on-the-fly compilation).

**Platform notes:** ARM64 hosts: HTP `.bin`+`.dlc` both work locally on real NPU. x64 hosts: default is ADB; local execution is opt-in — see `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md`.

**If `.bin` generation failed:** `windows-arm64` → B8 blocker; use `.dlc` as fallback. `linux-aarch64` → proceed with `.so`. x64 hosts → see `x64-host-notes.md`; if offline prepare still fails, prefer `--skip_contextbin` and use `.dlc` directly via `QNNContext`.

> **⚠️ IMPORTANT** (qai_runner.py wrapper): Pass the `.onnx` file path to `InferenceSession`. The wrapper searches for a matching QAIRT model file **in the same directory**. See [Model File Resolution](#model-file-resolution) below.

**Debugging**: run with `python qai_runner.py script.py` (QAIRT) or `python script.py` (ONNX baseline) to compare outputs.

---

## ⚠️ CRITICAL: Input Format — NCHW vs NHWC (--preserve_io Effect)

**This is the most common cause of wrong inference results with QNN HTP.**

The input format required by the QNN model depends on whether `--preserve_io` was used during conversion:

| Conversion flag | QNN model input format | Required inference input |
|----------------|----------------------|--------------------------|
| `--preserve_io` (used by `qai_convert_fp.py`) | **NCHW** `[1, C, H, W]` — same as ONNX/PyTorch | Pass NCHW directly (no transpose needed) |
| No `--preserve_io` | **NHWC** `[1, H, W, C]` — QNN default | Transpose: `np.transpose(x, (0,2,3,1))` |

> ⚠️ **`qai_convert_fp.py` uses `--preserve_io` by default** — the QNN model keeps the ONNX input format (NCHW for PyTorch models).
> Passing NHWC to a NCHW model causes channel dimension mismatch → completely wrong results (e.g., predicting "window screen" instead of "Samoyed").

**How to determine the correct input format:**
```python
# Step 1: Always check model I/O first
model = MyModel("model", "model.bin")
print(f"Input shapes: {model.getInputShapes()}")
# [1, 3, 299, 299] → NCHW (C=3 is second dim) → pass NCHW directly
# [1, 299, 299, 3] → NHWC (C=3 is last dim) → pass NHWC

# Step 2: Prepare input accordingly
if input_shape[1] == channels:  # NCHW: [N, C, H, W]
    inp = image_nchw.astype(np.float32)          # no transpose needed
else:                            # NHWC: [N, H, W, C]
    inp = np.transpose(image_nchw, (0,2,3,1)).astype(np.float32)
```

**Verification**: Always compare QNN output with PyTorch CPU baseline — if Top-1 differs significantly, check input format first.

---

## Direct Inference via `qai_appbuilder` (QAIRT 2.45+)

> ⚠️ **Python**: `python_runtime_venv` (3.13; legacy alias `python_arm64_venv`) from `${APP_ROOT}\data\config\qairt_env.json`. If fails → run `Setup.bat`.

`scripts/inference/` contains reference templates. **These are starting points — customize for your model's I/O shapes/dtypes/pre-post processing.**

> **Template workflow:** 1) `infer_generic.py --model model.bin` → inspect I/O 2) Select closest template 3) Adapt post-processing 4) Test with random then real data.

### Inference Script Templates

| Script | Best for | Key customization points |
|--------|---------|--------------------------|
| `inference/infer_generic.py` | Any model, quick verification | Output format, reshape logic |
| `inference/infer_classify.py` | Classification (softmax output) | Input normalization, label mapping |
| `inference/infer_detect.py` | YOLO/SSD detection | Output tensor format, NMS params |
| `inference/infer_segment.py` | Semantic segmentation | Output channels, color palette |
| `inference/infer_sr.py` | Super-resolution | Input/output size, scale factor |

### Customizing Templates

```bash
# Step 1: Always start with infer_generic.py to inspect model I/O
python inference/infer_generic.py --model model.bin
# → Prints: input shapes, dtypes, output shapes, dtypes
```

```python
# Step 2: Identify mismatches
# Common issues:
#   - Output shape doesn't match template expectation
#   - Model uses NCHW but template expects NHWC
#   - Quantized model needs io_data_type=native
#   - Custom pre/post-processing required

# Step 3: Adapt the template
# Example: model outputs [1, 1000] logits (not softmax)
# → Add softmax in post-processing
# (inside your QNNContext subclass Inference method or after calling it)
logits = np.array(output).flatten()
probs = np.exp(logits - logits.max()) / np.exp(logits - logits.max()).sum()  # stable softmax
```

Write your own inference script following this pattern:

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel, PerfProfile
)
import numpy as np

# Step 1: Global QNN config — MUST be called BEFORE QNNContext
# qai_appbuilder 2.47 signature: (runtime, log_level, profiling_level, log_path)
# ⚠️ The old leading `qnn_lib_path` arg was REMOVED in 2.47 — do NOT pass "".
#    The package's bundled QNN libs are used automatically (no SDK path).
QNNConfig.Config(
    Runtime.HTP,           # runtime: Runtime.HTP or Runtime.CPU (enum, NOT string)
    LogLevel.WARN,         # log_level: LogLevel enum (ERROR/WARN/INFO/VERBOSE/DEBUG)
    ProfilingLevel.BASIC   # profiling_level: ProfilingLevel enum (OFF/BASIC/DETAILED)
)

# Step 2: Define model class (recommended: inherit QNNContext)
class MyModel(QNNContext):
    def Inference(self, input_data):
        output_data = super().Inference([input_data])[0]
        return output_data

# Step 3: Load model
# ⚠️ Signature: QNNContext(model_name: str, model_path: str)
# - model_name: arbitrary string identifier (e.g., "inception_v3")
# - model_path: supported formats: .bin (best) | .dlc | .dll
#   → .bin (context binary) = best performance, target format for deployment
#   → .dlc = SNPE format, also supported
#   → .dll = compiled model lib, works but slower (no HTP optimization cache)
# ❌ WRONG: QNNContext(model_path, config)
# ✅ CORRECT: QNNContext("name", "model.bin")  ← always prefer .bin
model = MyModel("my_model", r"C:\path\to\model.bin")

# Step 4: Inspect I/O (optional but recommended)
print(f"Input  shapes: {model.getInputShapes()}")
print(f"Output shapes: {model.getOutputShapes()}")
print(f"Input  dtypes: {model.getInputDataType()}")
print(f"Output dtypes: {model.getOutputDataType()}")

# Step 5: Prepare input — NCHW vs NHWC depends on the --preserve_io flag.
#   Authoritative rule + decision code → this file § "Input Format — NCHW vs NHWC"
#   (above). In short: check model.getInputShapes(), pass NCHW directly if
#   channel is dim 1, else np.transpose(x, (0,2,3,1)).
inp = image_nchw.astype(np.float32)  # adjust per the NCHW/NHWC section above

# Step 6: Run inference with BURST performance mode
# CRITICAL: SetPerfProfileGlobal MUST be called AFTER at least one QNNContext
# model is loaded in the current process. If called before any model is loaded,
# the call silently does nothing and inference runs at default (non-BURST) speed.
# Correct lifecycle:
#   1. Load model(s): QNNContext(...)
#   2. Set BURST:     PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
#   3. Run inference: model.Inference(...)
#   4. Release BURST: PerfProfile.RelPerfProfileGlobal()
#   5. Release model: del model
# If multiple models cooperate in one task, set BURST once before ALL inference
# and release once after ALL inference (not per-model).
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
output = model.Inference([inp_nhwc])   # inputs must be a LIST (see Key Notes)
PerfProfile.RelPerfProfileGlobal()

# Step 7: Process outputs
print(f"Output shape: {output.shape}")

# Step 8: Release resources
del model
```

### Key QAIRT 2.45 WoS Notes

| Item | Correct | Wrong |
|------|---------|-------|
| `QNNConfig.Config` call | Before `QNNContext(...)` | After or omitted |
| lib-dir arg | **Not passed** — removed in qai_appbuilder 2.47 (built-in libs used automatically) | Passing a leading `""` or SDK path (2.47 treats it as `runtime`) |
| `QNNConfig.Config` args | `(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)` | `("", Runtime.HTP, ...)` (old 2.46 lib-dir form) |
| `runtime` param type | `Runtime.HTP` (enum) | `"Htp"` (string) |
| `log_level` param type | `LogLevel.WARN` (enum) | `1` (int) |
| `QNNContext` signature | `QNNContext("name", "model.bin")` | `QNNContext(model_path, config)` |
| Model file priority | `.bin` > `.dlc` > `.dll` (all supported; `.bin` = best perf) | Assuming only `.bin` works |
| Inference API | `model.Inference([inp])` | `model.Execute([inp])` |
| Input format | Depends on `--preserve_io`: NCHW if used (default), NHWC if not. **Always check `model.getInputShapes()` first.** | Assuming always NHWC |
| NCHW→NHWC conversion | Only needed when model input is NHWC: `np.transpose(x, (0,2,3,1))` | Always transposing |
| Performance mode | `PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)` — **must be called AFTER at least one model is loaded** (otherwise silently ineffective) | `perf_profile=PerfProfile.BURST` in `Inference()` |
| Performance lifecycle | Set BURST once before all inference → run all models → release once after all done | Setting/releasing per model call (stack imbalance) |
| Resource cleanup | `del model` | Leaving model in memory |

### 🚀 HTP BURST performance lifecycle (session / streaming workloads)

`PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)` raises HTP to highest clock.

> 🔗 Multi-model rules: see § Multi-model same-process below. Symptoms: `troubleshooting/inference-troubleshooting/SKILL.md`.

1. **BURST requires ≥1 loaded QNNContext** — calling before any model loaded silently does nothing (`setPowerConfig error 0x32c9`).
2. **Set ONCE per session, hold for all inferences, release ONCE at end.** Never per-call (causes HTP clock ramp jitter). Streaming ASR: set at recording start → hold across all chunks → release after voice session ends. TTS pipeline: set once before first NPU stage → release after last.
3. **Release BEFORE `del model`** — else `You should set perf profile before you release it!`.

```python
# Session-scoped BURST (correct for streaming/ASR/TTS)
enc = QNNContext("encoder", "encoder.bin")      # 1. load model(s) first
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)   # 2. session start: set ONCE
try:
    for chunk in session:                       # 3. many inferences, BURST held
        run_inference(enc, chunk)
finally:
    PerfProfile.RelPerfProfileGlobal()          # 4. session end: release ONCE
del enc                                          # 5. then destroy contexts
```

### QNNConfig.Config — Full Signature (qai_appbuilder 2.47)

```python
QNNConfig.Config(
    runtime: str = Runtime.HTP,          # Runtime.HTP or Runtime.CPU (use enum)
    log_level: int = LogLevel.ERROR,     # LogLevel.ERROR/WARN/INFO/VERBOSE/DEBUG (use enum)
    profiling_level: int = ProfilingLevel.OFF,  # ProfilingLevel.OFF/BASIC/DETAILED (use enum)
    log_path: str = "None"               # log file path; "None" = console output
) -> None
# NOTE: 2.47 removed the old leading `qnn_lib_path` arg. The package's bundled
# libs/ is used automatically — do NOT pass a lib dir (passing "" makes 2.47
# treat it as the runtime and fail with "backend library does not exist: Qnn.dll").
```

### QNNContext — Full Constructor Signature

```python
QNNContext(
    model_name: str = "None",            # unique model identifier string
    model_path: str = "None",            # model file path — supported: .bin | .dlc | .dll
                                         # .bin (context binary) = best performance, preferred
                                         # .dlc = SNPE format
                                         # .dll = compiled model lib (no HTP cache)
    backend_lib_path: str = "None",      # QnnHtp.dll path; "None" = built-in (v2.0.0+)
    system_lib_path: str = "None",       # QnnSystem.dll path; "None" = built-in (v2.0.0+)
    is_async: bool = False,              # async inference mode
    input_data_type: str = DataType.FLOAT,   # DataType.FLOAT or DataType.NATIVE
    output_data_type: str = DataType.FLOAT   # DataType.FLOAT or DataType.NATIVE
)
```

> 🔗 The `input_data_type` / `output_data_type` above are the **runtime** dtypes
> (`DataType.FLOAT` vs `DataType.NATIVE`). The **packaged** output type declared
> for deployment (`output.type` in `inference_manifest.json`) is defined in
> `references/pack_export.md`.

### Multi-model same-process (sticky worker) rules

When multiple QNN models run **in one process** (e.g. a sticky worker with whisper-base + zipformer-zh + melotts-zh), these rules MUST hold to avoid `Incorrect amount of Input Buffers` / graph-binding errors. (Diagnostic symptom view → `troubleshooting/inference-troubleshooting/SKILL.md`; this is the authoritative reference with full code.)

**Rule 1 — `model_name` must be globally unique.** `QNNContext(model_name, model_path, …)` uses `model_name` as an internal key; two contexts sharing a name make the QNN runtime **reuse the first-loaded graph** for the second. Symptom: `Incorrect amount of Input Buffers for graphIdx: 0. Expected: N, received: M` where N belongs to a *different* model. Use `{model_id}_{filename_stem}`:
```python
# BAD — name collision across models (second reuses the first's graph!)
QNNContext("encoder", "models/whisper-base/encoder.bin", ...)
QNNContext("encoder", "models/zipformer-zh/encoder.bin", ...)
# GOOD — globally unique names
QNNContext("whisper-base_encoder", "models/whisper-base/encoder.bin", ...)
QNNContext("zipformer-zh_encoder", "models/zipformer-zh/encoder.bin", ...)
```

**Rule 2 — `QNNConfig.Config()` exactly once per process.** It sets global runtime state (backend lib path, log level, profiling); repeated calls may corrupt loaded graph state. Guard with a module-level flag. Canonical: `QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)`.

**Rule 3 — `input_data_type` / `output_data_type` is per-context.** Each context can pick its dtype independently:
```python
# NATIVE — pass tensors in the model's native dtype (int32, float16, …).
# Better performance, no type-conversion overhead. You MUST feed the exact native dtype;
# the model will NOT auto-convert in NATIVE mode.
QNNContext("whisper-base_encoder", path, input_data_type=DataType.NATIVE, output_data_type=DataType.NATIVE)
# FLOAT (default) — all tensors converted to float32 internally.
QNNContext("melotts_bert", path, input_data_type=DataType.FLOAT, output_data_type=DataType.FLOAT)
```
`DataType.NATIVE` = `"native"`, `DataType.FLOAT` = `"float"` (strings). In NATIVE mode ensure inputs match the model's expected dtype (e.g. `np.float16` mel spectrograms, `np.int32` token indices).

**Rule 4 — canonical multi-model setup** (from `whisper_base_en.py`):
```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel, DataType

# 1. Config once
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

# 2. Load contexts with unique names + NATIVE for best performance
encoder = QNNContext("whisper_encoder", encoder_path,
                     input_data_type=DataType.NATIVE, output_data_type=DataType.NATIVE)
decoder = QNNContext("whisper_decoder", decoder_path,
                     input_data_type=DataType.NATIVE, output_data_type=DataType.NATIVE)

# 3. Inference — pass a list of numpy arrays matching the native dtypes
output = encoder.Inference([mel_input])   # mel_input: np.float16
```

### HF `transformers` Version Compatibility (Whisper / LLM Pre- and Post-processing)

Multi-component ASR/LLM pipelines (e.g. Whisper) run **pre-processing (audio/text → tensors) and post-processing (token IDs → text) on the CPU in Python** via the HuggingFace `transformers` library, while only the encoder/decoder graphs run through `QNNContext` (see Rule 4 above). The target device's Python environment may pin a different `transformers` version than the host used for prototyping — loading local weights via the high-level `Processor.from_pretrained()` API can crash the pipeline **before NPU execution starts**.

**Known version pitfalls:**

| Pitfall | Cause | Symptom |
|---|---|---|
| Processor config clash | Older `transformers` (4.x) looks for `preprocessor_config.json`; newer (5.x) only writes `processor_config.json`. Symlinking one name to the other clashes with the feature-extractor constructor signature. | `TypeError: WhisperProcessor.__init__() got multiple values for argument 'feature_extractor'` |
| Legacy BPE tokenizer files missing | Loading a BPE tokenizer from a directory without legacy `vocab.json`/`merges.txt` falls back to the slow Python tokenizer, which requires those files explicitly. | `TypeError: expected str, bytes or os.PathLike object, not NoneType` |

**Mitigation:**
- Download the model-specific `vocab.json`, `merges.txt`, and `preprocessor_config.json` from Hugging Face and place them in the local weights folder before deploying to target.
- Load components separately instead of the high-level `Processor.from_pretrained(local_dir)`:
```python
from transformers import WhisperFeatureExtractor, WhisperTokenizer, WhisperProcessor
fe = WhisperFeatureExtractor.from_pretrained(model_dir)
tok = WhisperTokenizer.from_pretrained(model_dir)
processor = WhisperProcessor(feature_extractor=fe, tokenizer=tok)
```
  This manual assembly is robust across `transformers` 4.x and 5.x — it sidesteps both the config-clash and slow-tokenizer-fallback failure modes above.

> 🔗 Applies to any multi-component pipeline with CPU-side pre/post-processing (see `references/model-architectures/multi_component_pipeline.md` for Whisper/CLIP component-splitting) — the `transformers` call runs in the same Python process as the `QNNContext` calls above, so a version-mismatch crash here blocks NPU inference before it starts.

### Using Inference Templates (`scripts/inference/`)

> 💡 **These are reference templates.** Always verify model I/O first with `infer_generic.py`,
> then select and customize the appropriate template for your model.

#### Common arguments (all 5 templates)

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--model` | Yes | — | Path to QNN context binary (`.bin`) |
| `--input` | No* | — | Path to a single input image (not in `infer_generic.py`) |
| `--input_dir` | No* | — | Directory of images for batch mode (not in `infer_generic.py`) |
| `--runtime` | No | `Htp` | `Htp` or `Cpu` |
| `--log_level` | No | `1` | `0`=ERROR `1`=WARN `2`=INFO `3`=VERBOSE |

*For `infer_classify/detect/segment/sr.py`: one of `--input` or `--input_dir` is required.

#### `infer_generic.py` — extra arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--raw_paths` | — | One or more `.raw` float32 input files (alternative to `--input`) |
| `--output_dir` | — | Directory to save raw output files |
| `--io_data_type` | `float` | `float` or `native` (use `native` for quantized models) |

#### `infer_classify.py` — extra arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--labels` | — | Labels file: `.json` list or `.txt` one-per-line |
| `--topk` | `5` | Number of top predictions to display |
| `--input_size` | auto from model | Resize shorter edge to this size, then center-crop |
| `--normalize` | False | Apply ImageNet normalization (mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225]) |

**Auto-detects NCHW/NHWC** from `model.getInputShapes()`: if `shape[1] in (1,3,4)` and `shape[1] < shape[2]` → NCHW, else NHWC. Transposes input automatically — no manual transpose needed.

**Preprocessing pipeline** (inside script, not configurable via CLI):
1. `Image.open().convert("RGB")`
2. Resize: scale shortest edge to `input_size`, then center-crop to `input_size × input_size`
3. Normalize to `[0, 1]` (divide by 255)
4. If `--normalize`: apply ImageNet mean/std
5. Add batch dim → `(1, H, W, C)` NHWC, then transpose to NCHW if model expects it

**Output**: softmax probabilities, Top-K `(label, score, class_idx)` printed to stdout.

#### `infer_detect.py` / `infer_segment.py` / `infer_sr.py` — extra arguments

| Script | Argument | Default | Description |
|--------|----------|---------|-------------|
| `infer_detect.py` | `--conf` | `0.45` | Confidence threshold for detections |
| `infer_detect.py` | `--iou` | `0.7` | IoU threshold for NMS |
| `infer_segment.py` | `--output_dir` | — | Directory to save output mask images |
| `infer_segment.py` | `--alpha` | `0.5` | Blend alpha for mask overlay on original image |
| `infer_sr.py` | `--output_dir` | — | Directory to save upscaled output images |
| `infer_sr.py` | `--scale` | `4` | Upscale factor (e.g., `2`, `4`) |

`infer_sr.py` and `infer_classify.py` both auto-detect NCHW/NHWC I/O.

> ⚠️ **Detection: use `infer_detect.py` directly — avoid hand-written NMS** (common `IndexError` when `cls_boxes` empties mid-loop; guard with `if len(cls_boxes)==0: break`).

---

```bat
REM Step 1: Inspect model I/O (always do this first)
python scripts\inference\infer_generic.py --model model.bin
REM -> Prints input/output shapes and dtypes

REM Step 2a: Super-resolution model
python scripts\inference\infer_sr.py --model model_fp16.bin --input image.png --scale 4

REM Step 2b: Image classification model (with ImageNet labels + normalization)
python scripts\inference\infer_classify.py --model model.bin --input image.jpg ^
  --labels imagenet_labels.json --topk 5 --normalize

REM Step 2b (minimal, no labels file)
python scripts\inference\infer_classify.py --model model.bin --input image.jpg --topk 5

REM Step 2c: Object detection model (YOLO-style)
python scripts\inference\infer_detect.py --model model.bin --input image.jpg ^
  --conf 0.45 --iou 0.7

REM Step 2d: Semantic segmentation model
python scripts\inference\infer_segment.py --model model.bin --input image.jpg ^
  --alpha 0.5

REM Step 2e: Any other model (generic, raw I/O)
python scripts\inference\infer_generic.py --model model.bin ^
  --raw_paths input.raw --output_dir outputs\

REM Batch processing (all templates support --input_dir)
python scripts\inference\infer_classify.py --model model.bin ^
  --input_dir images\ --labels labels.json
```

> ⚠️ **If the template output doesn't look correct:**
> 1. Check output tensor shape with `infer_generic.py`
> 2. Verify input format (NHWC vs NCHW) — check `model.getInputShapes()`: NCHW if `--preserve_io` was used (default), NHWC otherwise
> 3. Adapt post-processing in the template to match your model's output format
> 4. For quantized models, add `--io_data_type native` to `infer_generic.py`

---

## qai_runner.py/qai Wrapper Usage
qai has same function as qai_runner.py. make it friendly to type.

```bash
# Copy wrapper scripts into the working folder, then run:
python qai_runner.py path/to/inference_script.py
python qai path/to/inference_script.py
```

### Target Device Inference over SSH
Source the user-provided QAIRT setup script on target before inference (sets `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `QNN_SDK_ROOT`, activates venv):

Inference can also be run directly on the target device over SSH. Before launching inference, you **must** source the QAIRT setup script on the target device.

This setup script path is **user-provided** (it is environment-specific) and typically performs tasks such as:
- Exporting required environment variables (e.g., `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `QNN_SDK_ROOT`, etc.)
- Activating a Python virtual environment (if your workflow uses one)
- Initializing QAIRT/QNN runtime environment

Example:

```bash
ssh ubuntu@<target-ip>
. /home/ubuntu/aienv.sh
python qai path/to/inference_script.py
python qai_runner.py path/to/inference_script.py
```

### Linux ARM HTP Environment (manual export only)

If HTP initialization fails on Linux ARM, you need to set `QAIRT_SDK_ROOT` /
`QNN_SDK_ROOT` / `PRODUCT_SOC` / `DSP_ARCH` / `ADSP_LIBRARY_PATH` /
`LD_LIBRARY_PATH` in the shell before running. **Full setup, error symptoms
(`Stub lib id mismatch`, `Failed to create transport ... error: 1008`),
diagnostic checklist, and SSH-one-liner pattern → `troubleshooting/inference-troubleshooting/SKILL.md` § Linux ARM: HTP transport / version mismatch.**
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
python qai path/to/inference_script.py
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
ssh ubuntu@<target-ip> '. /home/ubuntu/aienv.sh && python qai path/to/inference_script.py'
```


### x86 / CPU Host Inference

**`windows-x64`:** the unified `onnxwrapper.py` shipped in `scripts/` reads `data/config/host_arch` and auto-selects the `x86_64-windows-msvc` toolchain automatically — no wrapper swap needed. For backend selection when running inference locally see `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md`. Launch:
```powershell
<python_runtime_venv>\Scripts\python.exe qai_runner.py path\to\infer_<model>.py
```

**`linux-x64`** (if the `qai_appbuilder` wheel is not available on this install): copy the CPU-only wrapper into the project:
```bash
cp scripts/onnxwrapper_x86.py ./onnxwrapper.py
cp scripts/qai_runner.py ./
python qai_runner.py path/to/inference_script.py
```

For x64 hosts: local backend selection (QnnCpu vs QnnHtp simulator), the wrapper's `--backend` legacy default, and applicable blocking / closing-statement rules → `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` (§3 wrapper bug, §5 B11, §4 closing statement). Use with `.dlc` or the `.bin` from Step 6. Cosine vs ONNX applies (≥0.99 FP16/FP32).
This keeps your inference script unchanged (`import onnxruntime as ort`) while routing execution through the x86-compatible QAIRT wrapper.

Note for x86 wrapper behavior:
- `onnxwrapper_x86.py` is CPU-only by design for stable host execution.
- Runtime selection like `QAI_QNN_RUNTIME=HTP` is ignored by this wrapper.
- **`QAIRT_SDK_ROOT` is mandatory here** (unlike `onnxwrapper.py`, which ignores it):
  the x86 wrapper raises `RuntimeError: QAIRT_SDK_ROOT is not set` at construction time.
  It then picks a host toolchain by probing **`$QAIRT_SDK_ROOT/bin/<toolchain>`** — *not*
  `lib/` — in a fixed order: `x86_64-linux-clang` → `aarch64-oe-linux-gcc11.2` →
  `x86_64-windows-msvc`; the first existing directory wins. If none exists it raises
  `Unable to detect QAIRT host toolchain path under QAIRT_SDK_ROOT/bin`.
  (Only `aarch64-oe-linux-gcc11.2` ships the aarch64 host tools; the other aarch64
  `bin/` dirs are device-runtime-only and are deliberately not candidates.)
- Recommended usage remains simply:
```bash
python qai path/to/inference_script.py
```

Inference script uses standard `onnxruntime` API — pass the `.onnx` path; the wrapper resolves the QNN model automatically:

```python
# ✅ CPUExecutionProvider: for ONNX CPU baseline comparison (the allowed use within this skill)
import onnxruntime as ort
sess = ort.InferenceSession("model.onnx")  # defaults to CPUExecutionProvider
outputs = sess.run(None, {"input_name": input_tensor})

# ❌ Forbidden: do NOT use onnxruntime to run the model on the NPU
# NPU inference always goes through qai_appbuilder / QNNContext (loading .bin / .dlc)
```

---
## ARM64X (CHPE) Model File Resolution on ARM64 Windows

On ARM64 Windows where Python runs under x86_64 emulation:

1. **QNN runtime DLLs** (`QnnHtp.dll`, `QnnHtpPrepare.dll`, etc.) are loaded from
   `qai_appbuilder/libs/` (bundled ARM64X hybrid format) by default, or from
   `$QAIRT_SDK_ROOT/lib/arm64x-windows-msvc/` **only when you set
   `QAI_QNN_LIBS_DIR` to it explicitly** — the wrapper performs no toolchain
   auto-detection (see § *QNN libs dir resolution order*).
   ARM64X DLLs contain both x64 and ARM64 code — Windows loads the correct path
   automatically based on the process architecture.

2. **Model library DLL** (`esrgan.dll`) must be **x86_64** (`windows-x86_64` target)
   because the Python process is x86_64 emulated. ARM64-native DLLs cannot be loaded
   by an x86_64 process.

3. **Context binary** (`.dll.bin`) is SoC-specific and platform-independent — the same
   `.bin` works whether generated from an x86_64 or ARM64 host.

4. **ADSP_LIBRARY_PATH** should point to both the native ARM64 stub library directory and the Hexagon skel library directory for your specific SoC:
   ```powershell
   # Set <dsp_arch> to your SoC's Hexagon arch dir, e.g. hexagon-v73 / hexagon-v81
   $env:ADSP_LIBRARY_PATH = "$env:QAIRT_SDK_ROOT\lib\<dsp_arch>\unsigned;$env:QAIRT_SDK_ROOT\lib\aarch64-windows-msvc"
   ```
   This ensures both the Windows-side stub DLLs (e.g., `QnnHtpV73Stub.dll`) and Hexagon-side skel libraries (e.g., `libQnnHtpV73Skel.so`) are correctly loaded.

   To resolve `<dsp_arch>` without hardcoding, use the SDK validator plus the in-repo table:
   ```powershell
   qnn-platform-validator --backend dsp --coreVersion   # -> "Core Version ... : V73" => hexagon-v73
   ```
   `scripts/_soc_targets.py` is the single source of truth for the SoC → `dsp_arch` mapping;
   see also `references/host_context_binary_gen.md` § *Step 1 Option C*.
**Summary of file roles on ARM64 Windows:**

| File | Arch | Source |
|------|------|--------|
| `esrgan.dll` (model lib) | x86_64 | `qnn-model-lib-generator -t windows-x86_64` |
| `esrgan.dll.bin` (context bin) | SoC-specific | `qnn-context-binary-generator` |
| `QnnHtp.dll` (runtime) | ARM64X/CHPE | `qai_appbuilder/libs/` (default) or SDK `arm64x-windows-msvc/` via explicit `QAI_QNN_LIBS_DIR` |
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

# Now qai can find the QNN model:
python qai inference.py

# Now qai_runner.py can find the QNN model:
python qai_runner.py inference.py
```

---

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

The QNN runtime uses underscore-separated names internally (e.g. `past_key_values_0_key`),
while the original ONNX model uses dot-separated names (e.g. `past_key_values.0.key`).
The wrapper handles both formats transparently.

**Name resolution in get_input_names() / get_inputs():**

The wrapper returns ONNX dot-format names when it can *verify* a mapping onto the
QNN names. Candidates are tried in this order; the first one that verifies wins:

| Priority | Source |
|----------|--------|
| 1 | ONNX-inspected `{model_wo_ext}.yaml` next to the model (`qai_inspect_onnxio.py`) — real ONNX dot names, real ONNX order |
| 2 | The loaded / auto-generated IO config (`input:` / `inputs:` name list) |
| 3 | QNN underscore names (no translation at all) |

Both YAML shapes are accepted: the flat `input: [a, b]` list and the
`inputs: [{name: a, ...}]` dict list.

```python
session = ort.InferenceSession("model.onnx")
names = session.get_input_names()
# With YAML:    ['input_ids', 'past_key_values.0.key', ...]
# Without YAML: ['input_ids', 'past_key_values_0_key', ...]
```

**How the mapping is verified — and why it is not positional:**

`QNNContext.getInputName()` / `getOutputName()` do **not** guarantee the ONNX
graph order, so two name lists having the same length proves nothing about their
order. The wrapper therefore never pairs them by position. For each ONNX name it
applies the deterministic sanitization rule (`.` → `_`) and looks the result up
in the QNN name list:

1. `past_key_values.0.key` → expected QNN name `past_key_values_0_key`
2. that exact name must be present in `getInputName()` — if it is, the pair is
   accepted regardless of where it sits in either list
3. an ONNX name that is already QNN-native (no dots) maps to itself

The mapping is accepted only when it is a full one-to-one match: every ONNX name
resolves, no two ONNX names collide on the same QNN name, and no QNN tensor is
left unclaimed. If any of those checks fails, the wrapper logs a `WARNING` naming
the offending tensors and falls back to the QNN-native names for that side
(inputs and outputs are resolved independently). It never guesses a positional
pairing: a wrong guess would silently feed tensors into the wrong slots instead
of failing loudly.

**Input feed accepts both formats:**

`session.run()` translates dot-format keys to the QNN names before inference.
Keys with no mapping are passed through unchanged, so a QNN-native feed keeps
working whether the dot-format mapping is active or was rejected:

```python
# Both work:
inputs = {"past_key_values.0.key": data}    # ONNX dot format
inputs = {"past_key_values_0_key": data}    # QNN underscore format
```

The `output_names` argument of `run()` accepts dot format too, so the names from
`get_output_names()` can be fed straight back in. `run()` does not mutate the
`input_feed` dict you pass it.

**Shape / dtype pairing:** `get_inputs()` / `get_outputs()` resolve shapes and
dtypes by name as well, not by index — `getInputShapes()` is indexed in QNN order
while the names returned may be in ONNX order.

**Recommendation:** Always run `qai_inspect_onnxio.py` after ONNX export to
generate the `.yaml` file. This ensures consistent dot-format names throughout.

## Output Tensor Ordering

QNN may return output tensors in a different order than the ONNX model.
The wrapper reorders outputs to match the ONNX spec using the YAML config.

**How reordering works:**

1. QNN returns tensors in its internal order (e.g., all KV tensors first, logits last)
2. The wrapper builds a name-to-tensor map: `out_map = {qnn_name: tensor}`
3. Outputs are reordered to match the ONNX YAML order: `[out_map[name] for name in yaml_output_names]`
   (the ONNX dot names are first translated to their QNN counterparts, by name)

**Source of output order:**

| Priority | Source | Example order |
|----------|--------|---------------|
| 1 | ONNX-inspected `.yaml` (from `qai_inspect_onnxio.py`) | logits first |
| 2 | Autogen YAML (from QNNContext IO specs) | logits last |
| 3 | QNN internal order (`getOutputName()`) | logits last |

**Do not rely on positional indices.** Always verify output order:

```python
session = ort.InferenceSession("model.onnx")
out_names = session.get_output_names()
print(out_names)  # Check order before indexing

outputs = session.run(None, inputs)
logits_idx = out_names.index("logits")
logits = outputs[logits_idx]
```

**If outputs are in the wrong order:**

- Ensure `<model>.yaml` exists next to the ONNX file (run `qai_inspect_onnxio.py`)
- Check the YAML output list matches the expected ONNX order
- The wrapper matches ONNX dot names to QNN underscore names by converting dots to
  underscores and then confirming the result really is in `getOutputName()`; if any
  name fails that check it logs a `WARNING` and keeps the QNN names untranslated


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
| `QAI_QNN_LIBS_DIR` | auto | Override QNN libs dir — **the only** knob `SessionOptions` reads (see below) |
| `QAIRT_SDK_ROOT` | — | QAIRT SDK root. Used by the **conversion / context-bin scripts** and by `onnxwrapper_x86.py`. **`onnxwrapper.py` does NOT read it** — it has no effect on `qnn_libs_dir`. |

### QNN libs dir resolution order (`SessionOptions`)

`SessionOptions.__init__` resolves `qnn_libs_dir` in exactly **two** steps:

1. `QAI_QNN_LIBS_DIR` env var (explicit override)
2. `qai_appbuilder/libs/` — the bundled libs shipped inside the installed
   `qai_appbuilder` package (`_default_qai_libs_dir()`)

The resolved directory is then prepended to `PATH` (`_ensure_path_contains`) and logged as
`[QNN] Using QNN libs dir: <abspath>` — check that log line first when diagnosing a libs
problem.

> ⚠️ **There is no auto-detection of the QAIRT SDK toolchain directory.** The wrapper does
> not inspect `QAIRT_SDK_ROOT`, does not probe for `arm64x-windows-msvc`, and does not pick
> a per-OS/per-arch toolchain subdirectory. Anything other than the bundled libs must be
> selected by hand via `QAI_QNN_LIBS_DIR`, e.g.:
>
> - Linux ARM deployment using the OE runtime libs →
>   `export QAI_QNN_LIBS_DIR="$QAIRT_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"`
> - ARM64 Windows needing SDK libs instead of the bundled ones →
>   `$env:QAI_QNN_LIBS_DIR = "$env:QAIRT_SDK_ROOT\lib\arm64x-windows-msvc"`

> ⚠️ **ARM64 Windows / x86_64 emulation — the wrapper will NOT rescue you here.**
> The pure x86_64 `QnnHtp.dll` from `x86_64-windows-msvc` **cannot access HTP hardware**
> on ARM64, because x86_64 emulation does not forward NPU driver ioctls. The ARM64X (CHPE)
> hybrid `QnnHtp.dll` contains both x64 and ARM64 code paths, so Windows loads the correct
> one automatically regardless of the process architecture (the ARM64X variant is
> significantly smaller because it uses CHPE thunks instead of bundling a full x64
> implementation).
>
> `SessionOptions` does **not** detect this situation and does **not** prefer
> `arm64x-windows-msvc` over `x86_64-windows-msvc`. So when HTP initialization fails on
> ARM64 Windows, treat it as a **manual** libs-dir problem: read the
> `[QNN] Using QNN libs dir:` log line, then point `QAI_QNN_LIBS_DIR` at an ARM64X-capable
> directory (`qai_appbuilder/libs/`, which ships ARM64X hybrids, or
> `$QAIRT_SDK_ROOT\lib\arm64x-windows-msvc`) and re-run. Never point it at
> `x86_64-windows-msvc` on an ARM64 machine.

> ⚠️ **Always source the QAIRT env script before running inference.**
> The `qai_appbuilder` package bundles its own `libQnnHtp.so` which may be a
> different version than the QAIRT SDK used to compile the model/context binary.
> Because the wrapper silently defaults to those bundled libs whenever
> `QAI_QNN_LIBS_DIR` is unset, an ABI mismatch can segfault in C extension getter calls
> (`getGraphName`, `getInputName`, etc.) when loading a context binary. If that happens,
> set `QAI_QNN_LIBS_DIR` to the matching `$QAIRT_SDK_ROOT/lib/<toolchain>` directory
> explicitly — sourcing the env script alone is not enough, since `onnxwrapper.py` never
> looks at `QAIRT_SDK_ROOT`.

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

The qai wrapper is designed to work with ONNX models through onnxruntime.
For validation and debugging that requires comparing PyTorch outputs against QNN
outputs, use ONNX Runtime (CPU) as the bridge — not PyTorch directly:

`python
# Validation script — ONNX Runtime CPU vs QNN HTP (no PyTorch)
import numpy as np
import onnxruntime as ort

# Run on CPU via standard onnxruntime (not through aipc)
session_cpu = ort.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])
outputs_cpu = session_cpu.run(None, inputs)

# Run on HTP via qai wrapper
# python qai infer.py  (onnxruntime hot-patched to QNN)
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
python qai decode_only.py # QNN only, loads KV cache from disk
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
- [ ] **Input format verified**: check `model.getInputShapes()` — NCHW if `--preserve_io` used (default with `qai_convert_fp.py`), NHWC otherwise. Wrong format → completely wrong results. onnxwrapper may handle this in currrent version.
- [ ] Output tensor mapping is correct (check autogen YAML if wrong)
- [ ] Cosine similarity vs ONNX CPU baseline ≥ 0.99 (FP) / ≥ 0.95 (INT8). If below: do NOT auto-fix. Run zero-cost diagnosis first (single-image calibration?), then STOP and ask user which fix to try: (1) calibration diversity, (2) CLE+per_channel, (3) W8A16, (4) FP16/BF16, (5) accept. See `references/quantization-sensitivity.md`.
- [ ] Latency / FPS collected on target runtime

---

## qnn-net-run Fallback (qai_appbuilder version mismatch)

If `python qai script.py` segfaults (exit 139) when loading a context binary, the installed
`qai_appbuilder` version is incompatible with the QAIRT SDK that generated the context binary.

**Preferred fix**: build `qai_appbuilder` from source (see §qai-appbuilder-build above).

**Fallback** (when build is not feasible): use `qnn-net-run` CLI directly.

### qnn-net-run inference pattern

```python
import subprocess, os, numpy as np

def run_qnn_netrun(ctx_bin, inputs: dict, output_shapes: dict,
                   work_dir: str, qairt_bin: str, qnn_libs: str) -> dict:
    """Run one inference step via qnn-net-run CLI."""
    os.makedirs(work_dir, exist_ok=True)
    htp_backend = os.path.join(qnn_libs, "libQnnHtp.so")

    for name, arr in inputs.items():
        arr.astype(np.float32).tofile(os.path.join(work_dir, f"{name}.raw"))

    input_line = " ".join(f"{n}:={work_dir}/{n}.raw" for n in inputs)
    with open(os.path.join(work_dir, "input_list.txt"), "w") as f:
        f.write(input_line + "\n")

    out_dir = os.path.join(work_dir, "out")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        os.path.join(qairt_bin, "qnn-net-run"),
        "--backend",          htp_backend,
        "--retrieve_context", ctx_bin,
        "--input_list",       os.path.join(work_dir, "input_list.txt"),
        "--output_dir",       out_dir,
        "--use_native_input_files",
        "--use_native_output_files",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if result.returncode != 0:
        raise RuntimeError(f"qnn-net-run failed:\n{result.stderr[-1000:]}")

    result_dir = os.path.join(out_dir, "Result_0")
    outputs = {}
    for name, shape in output_shapes.items():
        raw_path = os.path.join(result_dir, f"{name}.raw")
        nbytes = os.path.getsize(raw_path)
        nelems = int(np.prod(shape))
        if nbytes == nelems * 2:
            outputs[name] = np.fromfile(raw_path, dtype=np.float16).reshape(shape).astype(np.float32)
        else:
            outputs[name] = np.fromfile(raw_path, dtype=np.float32).reshape(shape)
    return outputs
```

**Key notes**:
- Outputs land in `<output_dir>/Result_0/<tensor_name>.raw` — **not** directly in `output_dir`
- Native output dtype is FP16 for FP16 models — detect by file size: `nbytes == nelems * 2`
- Input files must be FP32 raw binary (runtime converts internally)
- `--use_native_input_files` + `--use_native_output_files` enables raw binary I/O
- `--retrieve_context` loads a pre-compiled context binary (`.so.bin`)
- `--system_library` flag does **not** exist in all QAIRT versions — omit it

**Limitation**: each call spawns a new process (~0.5–2s overhead). For production, fix the
`qai_appbuilder` version mismatch instead (§qai-appbuilder-build above).

## See Also

- `scripts/inference/infer_generic.py` — Generic inference script for WoS ARM64 (qai_appbuilder)(auto-detects NCHW/NHWC I/O)
- `references/on_device_context_binary.md` — Context binary generation guide
- `references/win_qairt_setup.md` — WoS ARM64 environment setup
- `references/pack_export.md` — deployment packaging: `inference_manifest.json` / `output.type`
