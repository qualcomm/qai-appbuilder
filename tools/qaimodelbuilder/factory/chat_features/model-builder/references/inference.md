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

## ⚠️ CRITICAL: Model File Format and Context Binary

**qai_appbuilder supports three model formats (in priority order):**

| Format | Platform | Notes |
|--------|----------|-------|
| `.bin` (context binary) | ARM Windows / ARM Linux | **Best performance on the same target HW** — HTP-optimized, pre-compiled offline. **NOT cross-platform** (locked to one HTP version + host arch). |
| `.dlc` | ARM Windows / ARM Linux | SNPE/DLC format; **supported directly** — QNNContext compiles the graph on first load (slower cold start vs `.bin`). **Portable** across HTP versions / target devices. |
| `.dll` / `.so` | ARM Windows / ARM Linux | Compiled model lib; works but no HTP optimization cache |

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
4. **Linux / x64** → not yet finalized; ask user.

> 💡 `.dlc` direct load = same numerical result as `.bin`, just slower first run (on-the-fly compilation).

**Platform notes:** ARM Windows: both work. ARM Linux: `.so` directly, `.so.bin` optional. x86 Linux: CPU-only wrapper.

**If `.bin` generation failed:** Windows → B8 blocker; use `.dlc` as fallback. Linux → proceed with `.so`.

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

## WoS ARM64 Direct Inference (QAIRT 2.45)

> ⚠️ **Python**: ARM64 Python 3.13 (`python_arm64_venv` from `${APP_ROOT}\data\config\qairt_env.json`). If fails → run `Setup.bat`.

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

## qai_runner.py Wrapper Usage

```bash
# Copy wrapper scripts into the working folder, then run:
python qai_runner.py path/to/inference_script.py
```

### Target Device Inference over SSH

Source the user-provided QAIRT setup script on target before inference (sets `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `QNN_SDK_ROOT`, activates venv):

Example:

```bash
ssh ubuntu@<target-ip>
. /home/ubuntu/aienv.sh
python qai_runner.py path/to/inference_script.py
```

### Linux ARM HTP Environment (manual export only)

If HTP initialization fails on Linux ARM, you need to set `QAIRT_SDK_ROOT` /
`QNN_SDK_ROOT` / `PRODUCT_SOC` / `DSP_ARCH` / `ADSP_LIBRARY_PATH` /
`LD_LIBRARY_PATH` in the shell before running. **Full setup, error symptoms
(`Stub lib id mismatch`, `Failed to create transport ... error: 1008`),
diagnostic checklist, and SSH-one-liner pattern → [`troubleshooting/inference-troubleshooting/SKILL.md` § Linux ARM: HTP transport / version mismatch](../troubleshooting/inference-troubleshooting/SKILL.md).**

### x86 Host Inference (ONNX Wrapper Variant)

Copy x86 wrapper as `onnxwrapper.py` into your project:

```bash
# From skill scripts folder to project folder:
cp ${APP_ROOT}/factory/chat_features/model-builder/scripts/onnxwrapper_x86.py ./onnxwrapper.py
cp ${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_runner.py ./
python qai_runner.py path/to/inference_script.py
```

x86 wrapper is CPU-only; `QAI_QNN_RUNTIME=HTP` is ignored. Standard usage:
```bash
python qai_runner.py path/to/inference_script.py
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

---

## Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `QAI_QNN_RUNTIME` | `HTP` | `HTP` or `CPU` |
| `QAI_IO_CONFIG` | — | Explicit path to IO YAML |
| `QAI_IO_AUTOGEN_SAVE` | `1` | Save auto-generated YAML (`0` to disable) |

---

## Validation Checklist

- [ ] Input tensor name/shape matches model
- [ ] Preprocessing matches training/export assumptions
- [ ] **Input format verified**: check `model.getInputShapes()` — NCHW if `--preserve_io` used (default with `qai_convert_fp.py`), NHWC otherwise. Wrong format → completely wrong results.
- [ ] Output tensor mapping is correct (check autogen YAML if wrong)
- [ ] Cosine vs ONNX CPU baseline ≥ 0.99 (FP) / ≥ 0.95 (INT8). If below: do NOT auto-fix. Run zero-cost diagnosis first (single-image calibration?), then STOP and ask user which fix to try: (1) calibration diversity, (2) CLE+per_channel, (3) W8A16, (4) FP16/BF16, (5) accept. See `references/quantization-sensitivity.md`.
- [ ] Latency / FPS collected on target runtime

---

## See Also

- `scripts/inference/infer_generic.py` — Generic inference script for WoS ARM64 (qai_appbuilder)(auto-detects NCHW/NHWC I/O)
- `references/context_binary.md` — Context binary generation guide
- `references/win_qairt_setup.md` — WoS ARM64 environment setup
- `references/pack_export.md` — deployment packaging: `inference_manifest.json` / `output.type`
