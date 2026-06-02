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

> ⚠️ **CAUTION**: Under detailed HTP `optrace` profiling, the Chrometrace reader library (`libQnnChrometraceProfilingReader.so`) may fail with `Error printing stats.` due to incompatibilities with microsecond cycle metrics. If this occurs, use the following robust fallback strategies:
>
> **Fallback A: HTP Reader (Recommended for HTP hardware stats)**
> ```bash
> ${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/qnn-profile-viewer \
>     --reader ${QAIRT_SDK_ROOT}/lib/x86_64-linux-clang/libQnnHtpProfilingReader.so \
>     --input_log qairt_profile_output/qnn-profiling-data_0.log \
>     --output htp_stats.json
> ```
> *(This will also print extremely rich, human-readable per-layer Execution Cycles to stdout, which you can redirect to a `.txt` file).*
>
> **Fallback B: JSON Reader & Chrome Trace Converter**
> First, parse the profile log into standard QNN JSON:
> ```bash
> ${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/qnn-profile-viewer \
>     --reader ${QAIRT_SDK_ROOT}/lib/x86_64-linux-clang/libQnnJsonProfilingReader.so \
>     --input_log qairt_profile_output/qnn-profiling-data_0.log \
>     --output qairt_profile_output/profile_json.json
> ```
> Then, run the skill utility `convert_qnn_to_trace.py` to translate this QNN JSON format into a standard, visualize-friendly Google Chrome Trace JSON (`chromeTrace.json`):
> ```bash
> python skills/aipc-toolkit/scripts/convert_qnn_to_trace.py \
>     --input qairt_profile_output/profile_json.json \
>     --output qairt_profile_output/chromeTrace.json
> ```
> This script dynamically discovers HTP execution cycles and microsecond times directly from the JSON messages, scaling and mapping cycles into microsecond-based timelines to completely bypass native reader visualization crashes.
>
> **Fallback C: Standard CSV (Default Reader)**
> ```bash
> ${QAIRT_SDK_ROOT}/bin/x86_64-linux-clang/qnn-profile-viewer \
>     --input_log qairt_profile_output/qnn-profiling-data_0.log \
>     --output profile_default.csv
> ```

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

### Profiling Level Choice (SNPE)

- Use `--profiling_level detailed` when you need the human-readable per-layer timings and overall performance summary.
- Use `--profiling_level linting` when you need Chrome Trace export via `snpe-diagview --chrometrace`.

When using the `aipc` launcher + `onnxwrapper.py`, SNPE profiling level can be selected via:
- `QAI_SNPE_PROFILING_LEVEL=detailed` (default)
- `QAI_SNPE_PROFILING_LEVEL=linting`

`linting` and `detailed` are different profiling levels; you cannot “enable both” in a single run. Run twice if you need both outputs.

**Step 2: Convert to CSV and preserve full text output**

```bash
${SNPE_ROOT}/bin/x86_64-linux-clang/snpe-diagview \
    --input_log snpe_output/SNPEDiag_0.log \
    --output snpe_profile.csv
```

Agents must preserve the complete `snpe-diagview` stdout because it contains the
human-readable profiling summary, including:
- `SNPE Create Statistics`
- `SNPE Execute Statistics (Averaged)`
- `Per-Subnet Execution Times`
- `Per-Layer Execution Times`

Recommended pattern:

```bash
${SNPE_ROOT}/bin/x86_64-linux-clang/snpe-diagview \
    --input_log snpe_output/SNPEDiag_0.log \
    --output snpe_profile.csv | tee snpe-diagview_stdout.txt
```

Minimum artifacts to collect for SNPE profiling:
- `SNPEDiag_0.log`
- `snpe_profile.csv`
- `snpe-diagview_stdout.txt`

`snpe_profile.csv` is machine-friendly, but the user-visible per-layer block is in
the plain-text stdout. Do not discard it.

**Step 3: Convert to Chrome Trace (Requires `linting`)**

```bash
${SNPE_ROOT}/bin/x86_64-linux-clang/snpe-diagview \
    --input_log snpe_output/SNPEDiag_0.log \
    --chrometrace snpe_profile_chrometrace
```

**Step 4: Visualize in Chrome**

Open `chrome://tracing` in Google Chrome and load the emitted `*.json`.

Notes:
- `snpe-diagview --chrometrace ...` is only supported for logs generated with `--profiling_level linting`.
- It is not supported for `detailed` logs (even if CSV export works).

## Key Points
- **QNN `.so`/`.dlc`:** Set `enable_profiling=True` directly — no pre-step needed
- **QNN `.bin`:** Must regenerate context binary with `--profiling` before collecting data
- **SNPE `.dlc`:** `enable_profiling=True` routes through `snpe-net-run` automatically
- **SNPE `snpe-diagview`:** Always keep both CSV and full stdout text; users often need the printed `Per-Layer Execution Times` section
- **Output format:** All paths produce a `chromeTrace.json` viewable in `chrome://tracing`

## Common Issues
- **No profiling output:** Verify the output directory (`qairt_profile_output/` or `snpe_output/`) exists after inference
- **Context binary missing optrace:** Regenerate with `--profiling` flag; existing `.bin` files without instrumentation will not produce profiling data
- **Chrometrace reader library fails with `Error printing stats.`**: This is a known compatibility issue under HTP `optrace` detailed profiling. Use Fallbacks A, B, or C to parse the log instead.
- **Missing per-layer text in final results:** The agent likely kept only the CSV. Re-run `snpe-diagview` and save stdout to `snpe-diagview_stdout.txt`.

## References
- [QNN HTP Optrace Profiling](https://docs.qualcomm.com/nav/home/htp_backend.html?product=1601111740009302#qnn-htp-optrace-profiling)
- [Profile Your Model](https://docs.qualcomm.com/doc/80-90441-15/topic/profile-your-model.html#panel-0-0-1)
- [Performance Analysis Using Benchmarking Tools](https://docs.qualcomm.com/doc/80-63442-4/topic/performance-analysis-using-benchmarking-tools.html)


## QNN QHAS / Optrace Profiling (Schematic-Based)

To generate complete QHAS (Qualcomm Hardware Analysis System) interactive HTML reports and advanced Chrome Traces (with HTP topology, dataflows, memory graphs, and sequencer tracing):

1. **Generate the Schematic Binary during Context Binary creation**:
   Ensure you pass `--profiling_level detailed --profiling_option optrace` to `qnn-context-binary-generator`:
   ```bash
   qnn-context-binary-generator \
       --profiling_level detailed \
       --profiling_option optrace \
       --backend ${QAIRT_SDK_ROOT}/lib/${TARGET_ARCH}/libQnnHtp.so \
       --model libmodel.so \
       --binary_file model.bin
   ```
   This will output:
   - `model.bin` (Context Binary)
   - `model_schematic.bin` (Schematic file required for QHAS visualization)

2. **Run inference with Optrace detailed profiling**:
   Run your `qnn-net-run` or ONNX wrapper with detailed optrace profiling to generate the raw log file:
   - `qairt_profile_output/qnn-profiling-data_0.log`

3. **Generate QHAS HTML Dashboard and Advanced Traces**:
   Run `qnn-profile-viewer` specifying both the raw log and the **schematic binary** using the `libQnnHtpOptraceProfilingReader.so` reader library:
   ```bash
   qnn-profile-viewer \
       --reader ${QAIRT_SDK_ROOT}/lib/${TARGET_ARCH}/libQnnHtpOptraceProfilingReader.so \
       --input_log qairt_profile_output/qnn-profiling-data_0.log \
       --schematic model_schematic.bin \
       --output qairt_profile_output/chromeTrace_optrace.json
   ```

This will automatically output the following elite diagnostic reports in the output folder:
- `chromeTrace_optrace.json`: Standard Chrome Trace timeline.
- `chromeTrace_optrace_htp.json`: Highly detailed HTP sequencer and memory hardware trace.
- `chromeTrace_optrace_qnn_htp_analysis_summary.html`: Interactive QHAS HTML diagnostic dashboard (includes graph visualization, op-by-op info, sorting, filtering, and latency charts).
- `chromeTrace_optrace_qnn_htp_analysis_summary.json`: Comprehensive QHAS JSON raw metrics file containing hardware-level topology analysis.
