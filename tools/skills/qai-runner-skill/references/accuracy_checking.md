# AIPC / QAIRT Accuracy Checking Guide

This guide is used for the **Validation / Accuracy Check** step after inference is complete in
`assets/aipc_plan.md`. By default, it reuses the artifacts that were already deployed and accepted
during the inference step (QNN `.so` / `.dll`, QNN context binary `.so.bin` / `.dll.bin`, SNPE
`.dlc`) and runs the AIPC wrapper directly against the same inputs used for ONNX/PyTorch CPU
baseline comparison. **Do not proactively reconvert the model just for an accuracy check.** Only
enter QAIRT Accuracy Debugger or redo conversion/prepare when artifacts are missing, outputs are
missing, or validation raw-tensor / task metrics fail.

> [!IMPORTANT]
> Final AIPC acceptance must prioritize the real inference output produced by
> `python aipc infer_{MODEL_NAME}.py`. A QNN context binary may change output tensor order, and the
> AIPC wrapper relies on the `.yaml` file to restore ONNX output order. Calling `QNNContext`
> directly, or relying only on lower-level tool output, can cause silent output mismatches.

### Default artifact priority

| Flow / Platform | Default validation artifact | When reconversion / re-prepare is allowed |
|---|---|---|
| QNN Linux | Deployed `.onnx.so.bin` context binary; if recorded in plan, `.so` may be used as fallback | Context binary / `.so` missing, mismatched with ONNX/YAML, or additional intermediate outputs are needed |
| QNN Windows | Deployed `.onnx.dll.bin` context binary; if recorded in plan, `.dll` may be used as fallback | `.dll.bin` / `.dll` missing, architecture mismatch, or additional intermediate outputs are needed |
| SNPE | Already converted / quantized `.dlc` | `.dlc` missing, I/O signature mismatch, or requantization / re-export is needed |
| Debugger diagnostics | Reuse existing target outputs and baseline outputs first with `verification` | Use `inference_engine` only when comparable outputs are missing or layerwise `snooping` is required |

---

## 1. AIPC acceptance artifacts

After each inference step, save the following files for validation / accuracy checking:

```text
accuracy_check/
  inputs/                         # Raw or preprocessed raw/npy inputs used for this validation
  baseline_outputs/               # ONNX/PyTorch CPU baseline, saved by output name
  target_outputs/                 # AIPC wrapper target-backend outputs, saved by output name
  metrics.json                    # cosine / SNR / MAE / max_abs_diff / task metric
  real_inference_output.txt       # Inference acceptance log required by the AIPC plan
```

Recommended output naming:

```text
baseline_outputs/<output_name>.npy
target_outputs/<output_name>.npy
```

If you also need to feed QAIRT Accuracy Debugger `verification`, you may additionally save `.raw`
files, but make sure dtype, shape, and output names are recorded.

### Pass thresholds

| Accuracy type | Raw tensor cosine | SNR | Task-level check |
|---|---:|---:|---|
| FP32 / FP16 / BF16 | >= 0.99 | Recommended >= 30 dB | decoded/task output matches baseline or stays within tolerance |
| INT8 / INT4 / A16W8 / A8W4 | >= 0.95 | Recommended >= 20 dB | task metric drop <= 1% when an evaluation set exists |
| Embedding | >= 0.999 | Recommended >= 30 dB | retrieval / similarity results remain stable |
| LLM / decoder | token logits per project tolerance | check NaN/Inf first | first 10 greedy tokens should match as much as possible and text semantics should remain sane |

---

## 2. Linux / Windows execution paths

### 2.1 Linux host / Linux target

Run the AIPC wrapper locally on Linux or over SSH on a Linux target. `--save-raw-outputs` means
your project inference script must provide a way to save raw/npy outputs. If it does not yet,
extend `infer_{MODEL_NAME}.py` so every `{OUTPUT_NAMES}` tensor can be saved as `.npy` or `.raw`:

```bash
source <QAIRT_ENV_SETUP>        # for example: source ~/qairt_2404.sh
python aipc infer_${MODEL_NAME}.py \
  --save-raw-outputs accuracy_check/target_outputs \
  > accuracy_check/real_inference_output.txt 2>&1
```

If the target is a remote Linux system, prefer running the AIPC wrapper directly on the target and
then syncing the output directory back:

```bash
rsync -av ./inputs/ user@target:/work/accuracy_check/inputs/
ssh user@target 'cd /work/project && source <QAIRT_ENV_SETUP> && \
  python aipc infer_${MODEL_NAME}.py \
    --save-raw-outputs /work/accuracy_check/target_outputs \
    > /work/accuracy_check/real_inference_output.txt 2>&1'
rsync -av user@target:/work/accuracy_check/ ./accuracy_check/
```

> Linux remote is not the same as ADB. Android usually uses ADB; Linux targets more commonly use
> SSH or direct execution on the target. If the target matches QAIRT's `linux-embedded` platform
> flow, you may also use `qairt-accuracy-debugger --platform linux-embedded` for diagnostics, but
> final acceptance must still be based on AIPC wrapper output.

### 2.2 Windows on Snapdragon / ARM Windows

Run in Windows PowerShell. As on Linux, `infer_{MODEL_NAME}.py` must save every output tensor so
validation can compute metrics:

```powershell
. <QAIRT_ENV_SETUP.ps1>
python aipc infer_${MODEL_NAME}.py `
  --save-raw-outputs accuracy_check\target_outputs `
  *> accuracy_check\real_inference_output.txt
```

Notes:

* Context binary filenames must match the ONNX name, for example `{MODEL_NAME}.onnx.dll.bin`.
* The `.yaml` file must be deployed in the same directory as the `.onnx`; the AIPC wrapper uses it
  to restore output order.
* Runtime DLL selection for Windows x86_64 emulation vs native ARM64 Python must follow the
  host/target architecture rules recorded in the AIPC plan.

### 2.3 Android target

Android targets usually use ADB. If you run QAIRT debugger for target-side diagnostics, use
`--platform aarch64-android --device_id <adb_serial>`. Final AIPC acceptance should still save the
real wrapper outputs and decoded results.

### 2.4 QAIRT Accuracy Debugger platform selection

Use QAIRT debugger only when validation metrics fail or deeper diagnosis is needed. In QAIRT 2.47,
`qairt-accuracy-debugger inference_engine` supports these target platforms:

| Target | Parameter | Transport |
|---|---|---|
| Linux host local machine | `--platform x86_64-linux-clang` | local execution |
| Linux embedded | `--platform linux-embedded` | QAIRT platform flow / target-side execution, depending on project setup |
| Windows on Snapdragon | `--platform wos` | local Windows execution |
| Android | `--platform aarch64-android --device_id <serial>` | ADB |
| QNX | `--platform qnx --ip_address <ip> --username <user> --password <pwd>` | IP connection |

For a normal remote Linux target, prefer SSH to run the AIPC wrapper or lower-level
`qnn-net-run` on the target, then sync outputs back to the host for `verification`. Do not assume
remote Linux can be controlled like Android via ADB.

### 2.5 Debugger dependency isolation

QAIRT Accuracy Debugger may require extra Python packages on the target. Unless the user explicitly
approves it, do not modify the existing QAIRT / `qai_appbuilder` inference environment on the
target. Create an isolated debugger venv in the project, for example `.qairt_accdbg_venv`, and use
it only for `framework_runner`, `inference_engine`, `verification`, `tensor_visualizer`, and
`snooping`.

If target-side debugger fails with `module 'onnx' has no attribute 'version'` or a similar ONNX
API incompatibility, first pin a validated ONNX version inside the isolated venv and retry the
debugger. Do not replace the currently accepted inference venv. Record the venv path, installed
packages, versions, and fail/pass logs in the `aipc_plan.md` issue log.

---

## 3. Baseline and metric calculation

### 3.1 Generate an ONNX CPU baseline

Use exactly the same preprocessed inputs as AIPC inference and save baseline outputs:

```bash
python tools/run_onnx_baseline.py \
  --onnx ${MODEL_NAME}.onnx \
  --input accuracy_check/inputs/sample.npy \
  --output_dir accuracy_check/baseline_outputs
```

If the project does not provide `tools/run_onnx_baseline.py`, add a CPU baseline branch to
`infer_*.py`, or use ONNX Runtime `InferenceSession(..., providers=['CPUExecutionProvider'])` to
produce same-named `.npy` outputs.

### 3.2 Calculate cosine / SNR / diff

```bash
python - <<'PY'
import json
from pathlib import Path
import numpy as np

baseline_dir = Path('accuracy_check/baseline_outputs')
target_dir = Path('accuracy_check/target_outputs')
metrics = {}

for baseline_path in sorted(baseline_dir.glob('*.npy')):
    name = baseline_path.stem
    target_path = target_dir / baseline_path.name
    if not target_path.exists():
        metrics[name] = {'error': f'missing target output: {target_path}'}
        continue
    ref = np.load(baseline_path).astype(np.float64).reshape(-1)
    out = np.load(target_path).astype(np.float64).reshape(-1)
    if ref.shape != out.shape:
        metrics[name] = {'error': f'shape mismatch: {ref.shape} vs {out.shape}'}
        continue
    diff = out - ref
    cosine = float(np.dot(ref, out) / (np.linalg.norm(ref) * np.linalg.norm(out) + 1e-12))
    snr = float(20 * np.log10((np.linalg.norm(ref) + 1e-12) / (np.linalg.norm(diff) + 1e-12)))
    metrics[name] = {
        'cosine': cosine,
        'snr_db': snr,
        'mae': float(np.mean(np.abs(diff))),
        'max_abs_diff': float(np.max(np.abs(diff))),
        'nan_or_inf': bool((~np.isfinite(out)).any()),
    }

Path('accuracy_check/metrics.json').write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
print(json.dumps(metrics, indent=2, ensure_ascii=False))
PY
```

In PowerShell, you can call an equivalent scripted Python tool:

```powershell
python .\tools\compare_outputs.py `
  --baseline accuracy_check\baseline_outputs `
  --target accuracy_check\target_outputs `
  --output accuracy_check\metrics.json
```

---

## 4. Accuracy failure symptom analysis

In QAIRT deployment flows, the most common accuracy failure looks like this: **the model behaves
correctly on CPU (FP32), but after conversion to FP16 or a quantized format and execution on HTP or
GPU, outputs become gibberish, repeated meaningless characters, or cannot be decoded into normal
strings by the tokenizer.**

This usually comes from one of the following causes:

* **Numeric overflow / underflow**: FP16 has a much narrower representable range
  (`6.10 x 10^-5` to `65504`). Sensitive operators in large models, such as exponentials inside
  Softmax or variance computation inside LayerNorm / RMSNorm, can easily exceed FP16 limits, making
  the result instantly become `Infinity` or `NaN`.
* **Flush-to-zero of subnormal values**: for maximum performance, Snapdragon HTP hardware
  defaults to flushing extremely small floating-point numbers to zero. This can sharply reduce
  precision for operators that depend on tiny weight values or activation scales.
* **Output layout confusion**: the converter may transpose output tensor dimensions while
  optimizing the network. If post-processing does not adjust accordingly, token indices or other
  outputs can become completely scrambled.

---

## 5. Diagnosis and triage workflow

When you hit an accuracy issue, do not guess. Follow this standard diagnosis flow:

### Step 1: inspect raw outputs

Before any post-processing such as Tokenizer / NMS / argmax / decoder, inspect the raw model output
tensors directly. For LLMs, this means raw logits. For CV / ASR / embedding models, inspect the
raw tensors for the corresponding `{OUTPUT_NAMES}`.

* If values are all `NaN`, `-Inf`, or a fixed constant: this is **definitely a numeric overflow /
  non-convergence problem**. Focus on the precision of upstream operators.
* If values are normal floating-point numbers but decoding still looks like gibberish: check output
  tensor **shape and layout**, and verify vocab indexing alignment.

### Step 2: establish a CPU FP32 golden reference

Run the model with the QNN CPU backend in FP32, or compare directly against PyTorch / ONNX Runtime
CPU inference. If CPU FP32 results are correct, the converted graph topology is likely correct, and
the issue is purely numeric precision or NPU-backend execution behavior in FP16 / quantized mode.

### Step 3: use QAIRT Accuracy Debugger for supplemental diagnosis

Do not treat `qairt-accuracy-evaluator` as a layer-dump tool. In QAIRT 2.47, its CLI is a
configuration-driven model evaluation pipeline that only accepts arguments such as `-config`,
`-work_dir`, and `-inference_schema_type`. It does not provide `--model`, `--target_backend`,
`--reference_backend`, or `--layer_outputs`.

Recommended QAIRT Accuracy Debugger ladder:

1. `framework_runner`: verify the framework / ONNX baseline is reproducible and save reference
   tensors.
2. `inference_engine`: run only when you need to redump tensors or validate the debugger path
   itself; do not use it as a replacement for AIPC wrapper acceptance by default.
3. `verification`: compare reference and target tensors and generate `verification.csv`.
4. `tensor_visualizer`: plot important tensors / diffs for manual inspection.
5. `snooping`: run `oneshot`, `layerwise`, or `cumulative_layerwise` to locate the first layer with
   an obvious accuracy drop.

For every step, save the command, working directory, CSV/HTML/plot paths, and pass/fail conclusion.
At minimum, the report should record the worst cosine/SNR tensor, its op type, whether any layer
falls below threshold, and whether this affects final AIPC wrapper acceptance.

#### AIMET encoding diagnostics: `validate_encoding` / `compare_encodings`

When `{QUANT_TOOL} = AIMET`, and the project passes AIMET-exported `.encodings` to QAIRT / SNPE
through `--quantization_overrides`, Accuracy Debugger should also cover these two encoding checks:

1. **`validate_encoding` (before conversion or after DLC generation)**: check bitwidth, symmetry,
   per-channel settings, and scale/offset ranges in AIMET `.encodings`, `.json`, or quantized
   DLCs. If you provide only JSON / `.encodings`, per-tensor rules run. If you also provide
   `--dlc_file_path`, context-aware rules such as reshape / transpose / concat checks can run.
2. **`compare_encodings` (after conversion)**: compare AIMET `.encodings` against the converted
   encoding JSON or quantized DLC, and verify that scale/offset values did not change unexpectedly
   after the `--quantization_overrides` handoff. If fused ops or renamed tensors exist, you may
   provide `--framework_model_path` and `--quantized_dlc*_path` to help mapping.

Example:

```bash
# 1) Validate exported AIMET encodings before conversion.
qairt-accuracy-debugger validate_encoding \
  --encoding_path {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.encodings \
  --working_directory accuracy_check/acc_dbg_validate_encoding

# 2) If a quantized DLC exists, enable context-aware rules.
qairt-accuracy-debugger validate_encoding \
  --encoding_path {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.encodings \
  --dlc_file_path {OUTPUT_DIR}/{MODEL_NAME}_aimet_a{ACT_BITWIDTH}_w{WEIGHT_BITWIDTH}.dlc \
  --working_directory accuracy_check/acc_dbg_validate_encoding_dlc

# 3) Compare AIMET encodings with converted encodings JSON or a quantized DLC.
qairt-accuracy-debugger compare_encodings \
  --encoding_path1 {AIMET_ARTIFACTS}/{MODEL_NAME}_ptq.encodings \
  --encoding_path2 <converted_encoding.json-or-quantized.dlc> \
  --scale_threshold 0.001 \
  --working_directory accuracy_check/acc_dbg_compare_encodings
```

`validate_encoding --rule_config <rules.json>` replaces built-in rules with the custom rule set; it
does not append to them. Therefore, a custom rule file must explicitly include every built-in or
custom rule you still want to run. Older `qnn-accuracy-debugger` / `snpe-accuracy-debugger`
versions may use a `--compare_encodings` flag, but QAIRT 2.47+ should prefer the
`qairt-accuracy-debugger compare_encodings` / `validate_encoding` subcommands.

By default, validation should first use the `baseline_outputs/` and `target_outputs/` already saved
during inference. If you only need `cosine` / `snr` / `mse` / `mae`, call
`qairt-accuracy-debugger verification` directly and do not reconvert or rerun `inference_engine`:

```bash
qairt-accuracy-debugger verification \
  --reference_tensor accuracy_check/baseline_outputs \
  --inference_tensor accuracy_check/target_outputs \
  --comparators cosine snr mse mae \
  --working_directory acc_dbg/verify_existing_outputs
```

Use the following backup tools only when the inference step did not save comparable raw tensors,
existing artifacts cannot emit the tensors you need, or you need to locate the first bad layer:

* `snooping`: preferred for layerwise localization; automatically runs `oneshot`, `layerwise`, or
  `cumulative_layerwise` debugging.
* `inference_engine`: backup path that starts from a source model / DLC / context `.bin` and reruns
  reference and target backends while dumping tensors. It may trigger conversion / prepare, so it
  should not be the default validation path.
* `qairt-accuracy-evaluator`: for end-to-end metric comparison across multiple candidate
  configurations, not for single-run layer dumping.

#### Backup: Linux host redump example

```bash
source ~/qairt_2404.sh

# 1) Run the reference backend, for example CPU FP32
qairt-accuracy-debugger inference_engine \
  --input_model model.onnx \
  --input_list input_list.txt \
  --desired_input_shape "input_ids" 1,128 int32 \
  --output_tensor logits \
  --backend CPU \
  --platform x86_64-linux-clang \
  --converter_float_bitwidth 32 \
  --working_directory acc_dbg/cpu_fp32

# 2) Run the target backend, for example HTP FP16
qairt-accuracy-debugger inference_engine \
  --input_model model.onnx \
  --input_list input_list.txt \
  --desired_input_shape "input_ids" 1,128 int32 \
  --output_tensor logits \
  --backend HTP \
  --platform x86_64-linux-clang \
  --converter_float_bitwidth 16 \
  --working_directory acc_dbg/htp_fp16

# 3) Compare the dumped tensors
qairt-accuracy-debugger verification \
  --reference_tensor acc_dbg/cpu_fp32 \
  --inference_tensor acc_dbg/htp_fp16 \
  --comparators cosine snr mse mae \
  --working_directory acc_dbg/verify
```

#### Backup: Windows on Snapdragon redump example

```powershell
. <QAIRT_ENV_SETUP.ps1>

qairt-accuracy-debugger inference_engine `
  --input_model model.onnx `
  --input_list input_list.txt `
  --desired_input_shape "input" 1,3,224,224 float32 `
  --output_tensor output `
  --backend HTP `
  --platform wos `
  --converter_float_bitwidth 16 `
  --working_directory acc_dbg\wos_htp_fp16

qairt-accuracy-debugger verification `
  --reference_tensor acc_dbg\cpu_fp32 `
  --inference_tensor acc_dbg\wos_htp_fp16 `
  --comparators cosine snr mse mae `
  --working_directory acc_dbg\verify_wos
```

#### If `.so` / `.dll` / `.dlc` / context binary already exists, do you need reconversion?

* End-to-end acceptance: **do not reconvert by default**. Use the QNN `.onnx.so.bin` /
  `.onnx.dll.bin` context binary selected by the AIPC wrapper from the deployed inference step, or
  the planned fallback `.so` / `.dll`, or SNPE `.dlc`, then save target outputs and compare them
  against the baseline.
* Only reconvert, requantize, or regenerate a context binary when artifacts are missing, artifacts
  do not match ONNX / YAML, output tensors were not saved, or extra intermediate outputs are
  required.
* `qairt-accuracy-debugger inference_engine --input_model`: QAIRT 2.47 help describes this as
  `Path to the source model/dlc/bin file`, so it can start from a source model, DLC, or context
  `.bin`. But a standalone QNN `.so` / `.dll` is not a complete input. Treat it as a backup
  diagnostic path, not the default validation acceptance path.
* `linux-embedded --offline_prepare` may reject an already generated context `.bin` in some QAIRT
  versions and require DLC / source input instead. In that case, debugger may reconvert from ONNX
  and rerun prepare, exposing prepare failures different from the accepted AIPC context binary. If
  AIPC wrapper acceptance already passed, do not treat such debugger re-prepare failure as final
  acceptance failure. Record it as a debugger-path limitation or follow-up diagnostic item instead.
* True layerwise `snooping` usually needs the source ONNX / DLC or a graph structure that can be
  prepared again. With only the final context binary, you can usually do only end-to-end or
  preconfigured-output comparisons, and localization power is limited.

#### Linux ARM direct `.so` diagnostic path

On Linux ARM, `.so` is a valid fallback / diagnostic path, but it must be an AArch64 model
library. A host x86-generated `.so` is only suitable for host-side context-binary generation; it
cannot be deployed and directly loaded by a Python process on an ARM target.

If the host lacks a cross-compilation environment, for example missing `${TARGET_PREFIX}g++` or
`${SDKTARGETSYSROOT}`, you can compile the ARM `.so` natively on the target with `g++`:

```bash
source <target_qairt_envsetup.sh>
export TARGET_PREFIX=
export SDKTARGETSYSROOT=/
qnn-model-lib-generator \
  -c <model>.cpp \
  -b <model>.bin \
  -t aarch64-oe-linux-gcc11.2 \
  -l <model> \
  -o qairt_output/arm_native_libs
```

If direct `.so` inference already saved correct outputs but the process exits with `139`, first
check the Linux ARM HTP AppBuilder teardown segfault entry in `references/troubleshooting.md`. You
may add `AIPC_SAFE_EXIT=1` protection to the inference script: after outputs and metrics are saved,
flush stdout/stderr and call `os._exit(0)` to bypass the destructor phase. This workaround is only
for diagnostic / acceptance scripts where output completeness is already confirmed; still record
the normal-exit failure log.

If your goal is to compare multiple candidate configurations end to end, such as ONNX Runtime, QNN
CPU, QNN HTP FP16, and QNN HTP INT8 TopK / task metrics, then use `qairt-accuracy-evaluator`:

```bash
qairt-accuracy-evaluator \
  -config path/to/evaluator_config.yaml \
  -work_dir qacc_temp \
  -inference_schema_type qnn \
  -silent
```

Key config sections are `model.dataset`, `model.preprocessing`,
`model.inference-engine.inference_schemas`, and `model.metrics`. SDK sample configs live at:
`$QAIRT_SDK_ROOT/lib/python/qti/aisw/accuracy_evaluator/configs/samples/model_configs/qnn_htp_resnet50_config.yaml`.

#### How to analyze the report

Open the Accuracy Debugger / Evaluator summary CSV or HTML report and inspect `cosine` / `snr` /
`mse` top to bottom in execution order:

1. Find the **accuracy cliff**. If one layer's cosine suddenly drops from `0.999` to `0.90` or
   below `0.70`, that node is the likely **root cause**.
2. Record that operator's `Node Name` and `Op Type` (commonly `MatMul`, `Add`, `Softmax`,
   `RMSNorm`).

---

## 6. Solutions and mitigation strategies

For the identified accuracy-sensitive node, choose among the following options based on the
performance / accuracy tradeoff:

### Strategy 1: enable hardware FP32 accumulation (preferred, no performance penalty)

Through the HTP backend config file or QNN operator config, enable hardware FP32 accumulation only
for key `MatMul` operators. That means: **inputs and outputs stay FP16 to save bandwidth, but the
hardware performs MAC accumulation internally in FP32**.

* **How**: specify `"accum_precision": "fp32"` for selected nodes in the conversion or runtime
  config JSON.
* **Pros**: almost no performance penalty, and it solves most large-model overflow issues.

### Strategy 2: mixed precision and CPU fallback

For standalone operators that are not dominated by large matrix multiplication but are extremely
precision-sensitive, such as `RMSNorm` or `Softmax`, HTP cannot run them in pure FP32. In that
case, preserve them as FP32 during conversion.

* **How**: such operators will be dispatched to CPU at runtime and executed in true FP32.
* **Pros**: preserves accuracy perfectly.
* **Cons**: moving data between NPU and CPU can **significantly slow down inference**, so use it
  sparingly and locally.

### Strategy 3: use AIMET to search for an optimal mixed-precision configuration

Use Qualcomm's official AI Model Efficiency Toolkit (AIMET) mixed-precision analysis:

1. Provide a calibration set and evaluation callback.
2. AIMET performs a greedy search to determine which operators can safely drop to FP16 and which
   must remain FP32.
3. Pass the resulting precision config file (encoding JSON) to `qnn-onnx-converter` through
   `--quantization_overrides` for one-shot optimized deployment.

### Strategy 4: ONNX graph surgery and numeric scaling (scale up / scale down)

If mixed precision is not practical during ONNX export or conversion, for example due to hardware
limits or high fallback latency, use proportional numeric scaling to avoid FP16 overflow at the
source.

#### 1. Math and mechanism

Suppose there is an overflow-prone matrix multiplication `Y = X x W` where `X` is the input
activation and `W` is the weight.

* **Scale down the input**: divide the input by a scale factor `S` before the operator:
  `X_scaled = X / S`. This keeps values safely inside the FP16 range, for example reducing `60000`
  to `7500`.
* **Scale up the weight or output**: to preserve mathematical equivalence, use one of these:
  * **Weight absorption (zero runtime cost)**: during offline export or conversion, directly
    multiply the corresponding weight matrix by `S`: `W_scaled = W x S`. Then the hardware computes
    `X_scaled x W_scaled` and directly produces the correct-scale `Y` with **no extra runtime
    multiply/divide overhead**.
  * **Post-operator restoration**: if weights cannot be changed, insert a `Mul` node after the
    operator and multiply the output by `S`: `Y = Y_scaled x S`.

#### 2. How to choose the scale factor

Choose scale factor `S` according to these rules:

* **Use dynamic-range analysis**: run the model in FP32 baseline mode and record the maximum
  absolute value of the sensitive operator input, `X_max = max(|X|)`. Set a safe HTP computation
  boundary, usually `10000 ~ 15000` to preserve accumulation margin, and use
  `S = X_max / 10000`.
* **Prefer powers of two**: round `S` to the nearest `2^n` such as `2, 4, 8, 16, 32, 64`. In IEEE
  754 binary floating-point, multiplying or dividing by a power of two only shifts the exponent and
  does not change the mantissa, so it introduces **no extra rounding loss** and runs fastest in
  hardware.
* **Borrow from SmoothQuant**: for LLM overflow caused by per-channel outliers, introduce an
  adaptive channel-wise scale:

  `S_j = max(|X_j|)^alpha / max(|W_j|)^(1-alpha)`

  where `alpha` is in `[0, 1]`, typically `0.5`. This adaptively moves quantization difficulty
  from activations into weights.

* **Activation clipping (backup only)**: insert `clamp(-65500, 65500)` before or after sensitive
  operators to prevent values from overflowing directly into `Infinity`.

  > [!WARNING]
  > Clipping forcibly truncates features outside the boundary. It is a safety fallback and may
  > slightly hurt higher-level reasoning quality.

---

## 7. Summary comparison of strategies

| Solution | Accuracy gain | Performance impact | Implementation complexity | Recommendation |
| :--- | :--- | :--- | :--- | :--- |
| **FP32 accumulator (`accum_precision`)** | Excellent | Almost none | Low | 5/5 (preferred) |
| **AIMET automatic mixed-precision search** | Excellent | Automatically approaches Pareto optimum | Medium (requires a PyTorch tuning setup) | 4/5 |
| **Local CPU fallback (FP32)** | Perfect | Severe slowdown if switching often | Medium | 3/5 (only for a few operators) |
| **ONNX activation scaling / clipping** | Good | Very small | Higher (requires ONNX graph edits) | 2/5 (backup option) |
