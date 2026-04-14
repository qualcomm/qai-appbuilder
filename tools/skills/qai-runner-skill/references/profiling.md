# HTP Profiling Guide

## Overview
Collect and visualize per-layer profiling data for QNN and SNPE models running on the HTP backend.

## Prerequisites
- QAIRT SDK or SNPE SDK environment
- A compiled model (`.so` / `.dlc` / `.bin`)
- Google Chrome (for trace visualization, optional )

## QNN Profiling

When using the ONNX wrapper inference (`onnxwrapper`), the profiling path depends on the model format:

| Model format | Pre-step required |
|---|---|
| `.so` / `.dlc` (model library) | None — set `enable_profiling=True` directly |
| `.bin` (context binary) | Regenerate the context binary with `--profiling` first |

#### QNN model library (`.so` / `.dll`)

**Step 1: Enable profiling in the session**

```python
import onnxruntime as ort  # resolved to onnxwrapper by the skill

sess_options = ort.SessionOptions()
sess_options.enable_profiling = True          # routes inference through qnn-net-run
sess = ort.InferenceSession("model.onnx", sess_options)
```

When `enable_profiling=True`, the wrapper bypasses `QNNContext.Inference()` and calls `qnn-net-run` directly with `--profiling_level=detailed --profiling_option=optrace`.

**Step 2: Run inference**

Run inference as normal. Profiling logs are written automatically to `qairt_profile_output/` in the current working directory.

**Step 3: Collect profiling logs**

```
qairt_profile_output/qnn-profiling-data_0.log
```

**Step 4: Convert logs to Chrome Trace format**

```bash
${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/qnn-profile-viewer \
    --reader ${QAIRT_SDK_ROOT}/lib/x86_64-linux-clang/libQnnChrometraceProfilingReader.so \
    --input_log qairt_profile_output/qnn-profiling-data_0.log \
    --output chromeTrace.json
```

**Step 5: Visualize in Chrome**

Open `chrome://tracing` in Google Chrome and load `chromeTrace.json` to inspect per-layer execution times and identify bottlenecks.

#### QNN context binary (`.bin`)

The context binary must be regenerated with optrace instrumentation before profiling data can be collected.

**Step 1: Regenerate context binary with profiling enabled**

```bash
python scripts/aipc_dev_gen_contextbin.py --model /path/to/libmodel.so --profiling
```

This passes `--profiling_level detailed --profiling_option optrace` to `qnn-context-binary-generator`, embedding optrace instrumentation in the output `.bin`.

**Steps 2–5**: Same as the model library path above, using the generated `.bin` as the model path.

## SNPE Profiling

When `enable_profiling=True` and the model is a `.dlc` file, the wrapper routes inference through `snpe-net-run` with profiling flags forwarded — equivalent to the direct invocation shown in Step 1 below.

**Step 1: Run snpe-net-run with profiling**

```bash
${SNPE_ROOT}/bin/x86_64-linux-clang/snpe-net-run \
    --container model.dlc \
    --input_list input_list.txt \
    --output_dir snpe_output \
    --perf_profile burst \
    --profiling_level detailed
```

This writes `SNPEDiag_0.log` (and optionally `SNPEDiag_1.log`, …) to `snpe_output/`.

**Step 2: Convert to CSV**

```bash
${SNPE_ROOT}/bin/x86_64-linux-clang/snpe-diagview \
    --input_log snpe_output/SNPEDiag_0.log \
    --output snpe_profile.csv
```

**Step 3: Convert to Chrome Trace**

```bash
${SNPE_ROOT}/bin/x86_64-linux-clang/snpe-diagview \
    --input_log snpe_output/SNPEDiag_0.log \
    --output chromeTrace.json \
    --output_format chrometrace
```

**Step 4: Visualize in Chrome**

Open `chrome://tracing` in Google Chrome and load `chromeTrace.json`.

## Key Points
- **QNN `.so`/`.dlc`:** Set `enable_profiling=True` directly — no pre-step needed
- **QNN `.bin`:** Must regenerate context binary with `--profiling` before collecting data
- **SNPE `.dlc`:** `enable_profiling=True` routes through `snpe-net-run` automatically
- **Output format:** All paths produce a `chromeTrace.json` viewable in `chrome://tracing`

## Common Issues
- **No profiling output:** Verify the output directory (`qairt_profile_output/` or `snpe_output/`) exists after inference
- **Context binary missing optrace:** Regenerate with `--profiling` flag; existing `.bin` files without instrumentation will not produce profiling data

## References
- [QNN HTP Optrace Profiling](https://docs.qualcomm.com/nav/home/htp_backend.html?product=1601111740009302#qnn-htp-optrace-profiling)
- [Profile Your Model](https://docs.qualcomm.com/doc/80-90441-15/topic/profile-your-model.html#panel-0-0-1)
- [Performance Analysis Using Benchmarking Tools](https://docs.qualcomm.com/doc/80-63442-4/topic/performance-analysis-using-benchmarking-tools.html)