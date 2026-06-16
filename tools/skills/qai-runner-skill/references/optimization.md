# AIPC Optimization Guidelines

This document outlines various optimization techniques for converting and deploying models using the QAIRT SDK.

## `--preserve_io` Optimization Pass

> Scope: This layout optimization guidance is **QNN-only** (`FLOW=QNN`).
> It does **not** apply to SNPE/DLC conversion flow.

By default, QAIRT model conversion scripts often use `--preserve_io datatype` (or `--preserve_io layout`) to ensure the converted model's input and output tensors match the original ONNX model exactly. While this simplifies integration, it can introduce unnecessary conversion overhead at the edges of the model network.

### Performance Impact

Removing the `--preserve_io` flag allows the QNN compiler to use native network layouts (e.g., NHWC instead of NCHW) or data types without injecting extra reshape or cast operations at the inputs and outputs. 

In observed benchmarks (e.g., on HTP targets):
- **With `--preserve_io datatype`**: Baseline latency and cycles.
- **Without `--preserve_io` (`--preserve-io-mode none`)**: Can yield up to **~13% latency reduction** and **~23% cycle reduction**.
- **Important**: Layout optimization **must not** use `--preserve-io-mode layout` because it forces layout preservation and can block layout changes.

*Example observed delta:*
- *With preserve:* 25,620 µs | 20,523,379 cycles
- *Without preserve:* 22,240 µs | 15,735,938 cycles

### How to Apply

When converting your model in **QNN flow** using AIPC toolkit scripts (`aipc_convert_fp.py`, `aipc_convert_int.py`, or `aipc_convert_aimet.py`), pass `--preserve-io-mode none` (or omit preserve-io flags entirely):

```bash
python scripts/aipc_convert_fp.py \
  --onnx model.onnx \
  --preserve-io-mode none \
  ...
```

### Performance Check Policy (Mandatory)

For layout optimization comparisons (baseline preserve-io vs optimized none), performance validation must be run with profiling disabled.

- Do not enable `SessionOptions.enable_profiling` during latency/FPS comparison.
- Do not pass profiling options (for example `--profiling_level` / `--profiling_option`) in the measurement run.
- If profiling data is needed, collect it in a separate run after the no-profiling performance result is recorded.
- Report both values clearly:
  - `Performance run (profiling OFF)`: source of latency/FPS decision.
  - `Profiling run (profiling ON)`: diagnostic-only, not the primary performance KPI.

### ⚠️ Caveats and Integration Changes

When `--preserve_io` is disabled, the graph's input and output tensors may change:
1. **Layout Changes**: Tensors may shift from `NCHW` to `NHWC`.
2. **Datatype Changes**: Float inputs might be expected as quantized integers, or vice-versa.

**Action Required**:
- Re-run the Model Inspector (`aipc_inspect_onnxio.py`) or check the generated `_net.json` / `.yaml` config to understand the new expected input/output shapes and types.
- You must update your preprocessing and postprocessing code to match these new layouts and datatypes. If the overhead of doing this reshaping/casting on the CPU is larger than the QNN edge overhead, keeping `--preserve_io` might actually be better for end-to-end latency. 
- Always profile end-to-end to verify that the optimization provides a net benefit for your specific application.

## Recommended: Persist layout-permute metadata in YAML

For models converted with layout optimization (`--preserve-io-mode none`), the runtime wrapper should know how to map tensors between ONNX space and compiled QNN space.

Use your IO YAML (loaded by `QAI_IO_CONFIG`) and add a `layout_qairt_none_convert` section:

```yaml
inputs:
  - name: images
    dtype: float32
outputs:
  - name: output0
    dtype: float32

layout_qairt_none_convert:
  inputs:
    images:
      onnx_to_qnn: [0, 2, 3, 1]
  outputs:
    output0:
      qnn_to_onnx: [0, 1, 2]
```

Notes:
- `onnx_to_qnn`: permutation applied before backend inference.
- `qnn_to_onnx`: permutation applied after backend inference.
- This YAML metadata should be treated as the primary integration contract (reviewable, versioned).
- `_net.json` can still be used as fallback/validation source when YAML metadata is missing.

### Optional: auto-infer layout conversion from ONNX dims

`onnxwrapper.py` can also infer permutation when YAML provides ONNX dimensions and runtime can read QAIRT dimensions:

```yaml
inputs:
  - name: images
    onnx_shape: [1, 3, 640, 640]
outputs:
  - name: output0
    onnx_shape: [1, 84, 8400]
```

Behavior:
- Wrapper compares ONNX dims from YAML vs QAIRT runtime dims from `getInputShapes()/getOutputShapes()`.
- If rank matches and axis order differs, wrapper auto-derives a permutation and applies layout conversion.
- Explicit `layout_qairt_none_convert` permutes still take priority over shape-based inference.
- If shapes contain repeated dimension values (for example `[1, 64, 64, 3]`), shape-based permutation can be ambiguous. In this case, do **not** rely on auto-infer; provide explicit permutes via `layout_qairt_none_convert` or `_net.json`.
