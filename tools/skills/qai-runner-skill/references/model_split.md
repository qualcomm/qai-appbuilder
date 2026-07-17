# Model Splitting for Large Context Binaries

Use this reference when a QNN context binary is too large to be memory-mapped by the DSP
SMMU on the target device, causing context load failure at runtime.

> **When to use this reference**:
> - Context binary generation on the host succeeds, but loading on the target fails
> - Runtime log contains `fastrpc memory map` errors or `err 1002`
> - Context binary file size exceeds ~1 GB
> - VTCM sweep (`vtcm_mb=0,1,2,3,4,8`) does not resolve the failure
> - You need the context binary path (not JIT `.so`) for production deployment

---

## 1. Symptom — DSP SMMU Map Failure

When a context binary is too large for the DSP SMMU window, the target device logs:

```
fastrpc memory map for fd: N with length: XXXXXXXXX failed with error: 0x1
fastrpc memory map error reporting failed
Mapping buffer fd N to FastRpc failed on domain 3
SharedMemoryMod failed to Map Buffer to SMMU for domain 0
Failed to map buffer of size XXXXXXXXX
Failed to map weights buffer to device!
Could not allocate persistent weights buffer!
Failed to initialize graph memory
Failed to initialize graph with id N context N deviceId 0 coreId 0 pdId 0 with err 1002
Context create from binary failed for deviceId 0 coreId 0 pdId 0 for context N, err 1002
```

**Root cause**: The DSP SMMU (System Memory Management Unit) has a fixed maximum contiguous
mapping window per context (device- and firmware-dependent, typically 512 MB–1 GB on
embedded QCS/SA platforms). A context binary that exceeds this limit cannot be loaded as a
single unit.

**Key distinction from VTCM errors**:
- VTCM errors (`Request feature vtcm size … unsupported`) → fix by sweeping `vtcm_mb`
- SMMU map errors (`fastrpc memory map … error: 0x1`, `err 1002`) → fix by splitting the model

**VTCM sweep does not help** for SMMU failures — the binary size is determined by the model
weights, not the VTCM budget. Changing `vtcm_mb` produces the same binary size.

---

## 2. Decision Tree

```
Context binary load fails on target
         │
         ▼
Is the error "fastrpc memory map … error: 0x1" or "err 1002"?
         │
    YES  │                          NO → see troubleshooting.md (VTCM / transport errors)
         ▼
Is context binary > ~800 MB?
         │
    YES  │                          NO → check soc_id/dsp_arch mapping first
         ▼
Is JIT .so fallback acceptable for this deployment?
         │
    YES  │                          NO → must split model
         ▼                               ▼
  Use JIT .so path             Split model into parts
  (see §3 below)               (see §4 below)
```

---

## 3. Immediate Workaround — JIT `.so` Fallback

If context binary deployment is not strictly required (e.g. bring-up, validation, or
devices with sufficient on-device JIT compilation time), the `onnxwrapper` automatically
falls back to loading the `.so` model library directly when the `.so.bin` context binary
fails to load.

### Why JIT `.so` avoids the SMMU limit

The SMMU failure is caused by how the two loading paths deliver weights to the DSP:

**Context binary path (`.so.bin`) — bulk mmap, fails for large models**

`QnnContext_createFromBinary()` receives the entire `.bin` as a single contiguous buffer
in host memory, then calls `fastrpc_mmap` to map that buffer into DSP address space in
one shot. The DSP SMMU must create a single contiguous mapping covering the full buffer.
If the buffer exceeds the SMMU window (typically 512 MB–1 GB on embedded QCS/SA platforms),
the `mmap` syscall fails with `error: 0x1` → `err 1002`. The weights never reach the DSP.

**Model library path (`.so`) — on-device JIT, no single large mmap**

`QnnContext_create()` builds the graph from the model library (`.so`) at runtime. The HTP
compiler runs **on-device**, compiling the graph incrementally. Weights are transferred to
the DSP in smaller chunks during compilation — each chunk fits within the SMMU window.
The compiled result lives in DSP-managed memory, not in a single host-side buffer.
There is no single large `fastrpc_mmap` call, so the SMMU limit is never hit as a
single-shot constraint.

**Summary**: `.so.bin` = one giant mmap → hits SMMU limit for large models.
`.so` = incremental on-device JIT compilation → no single large mmap → always loads.

### How the wrapper selects and falls back

The wrapper's `_find_qnn_model_file()` builds a candidate list in priority order:
`.onnx.so.bin` (context binary) first, then `.onnx.so` / `lib<model>.so` (model library).
It selects the first existing file and passes it to `QNNContext`. When the context binary
is present but `QNNContext` fails to load it (SMMU error), `qai_appbuilder` internally
falls back to JIT compilation from the `.so`. The wrapper then auto-generates a
`<model>.htp.autogen.yaml` for I/O config.

**Confirming JIT fallback is active**:
```
[QNN] Selected QNN model file (runtime=HTP): onnx_models/<model>.onnx.so.bin
# followed by fastrpc errors, then inference proceeds via JIT
# A file <model>.htp.autogen.yaml is created in the working directory
```

**Limitation**: JIT compilation happens at first load on the target device, adding
latency on the first inference call. Subsequent calls use the compiled graph cached
in device memory (not persisted across process restarts).

**For production**: JIT fallback is not recommended for fixed-SoC deployment where
deterministic load time is required. Use model splitting (§4) instead.

---

## 4. General Model Splitting Workflow

Split the large model into two (or more) sequential sub-graphs, each producing a context
binary small enough to fit within the DSP SMMU window (~800 MB is a safe target per part).

### 4.0 Minimal example — pure sequential model

Before tackling skip connections or complex architectures, understand the pattern with the
simplest possible case: a model whose layers run strictly one after another with no
branching.

```python
import torch, onnx

# Original large sequential model (e.g. ResNet, EfficientNet, plain MLP)
# model.children() gives top-level blocks in forward order
blocks = list(model.children())   # e.g. 20 blocks total
split  = len(blocks) // 2         # cut at the midpoint

class PartA(torch.nn.Module):
    def __init__(self): super().__init__(); self.seq = torch.nn.Sequential(*blocks[:split])
    def forward(self, x): return self.seq(x)

class PartB(torch.nn.Module):
    def __init__(self): super().__init__(); self.seq = torch.nn.Sequential(*blocks[split:])
    def forward(self, x): return self.seq(x)

part_a, part_b = PartA().eval(), PartB().eval()

# Trace Part A to get the boundary tensor shape
with torch.no_grad():
    boundary = part_a(dummy_input)   # shape e.g. [1, 512, 14, 14]

# Export both parts — output of Part A feeds directly into Part B
torch.onnx.export(part_a, dummy_input,  'model_part_a.onnx',
                  input_names=['input'],  output_names=['boundary'],
                  opset_version=18, dynamo=False)
torch.onnx.export(part_b, boundary,      'model_part_b.onnx',
                  input_names=['boundary'], output_names=['output'],
                  opset_version=18, dynamo=False)

onnx.checker.check_model('model_part_a.onnx')  # path-based for >2 GB
onnx.checker.check_model('model_part_b.onnx')
```

Inference chains the two sessions — Part A output feeds Part B input directly:

```python
import onnxruntime as ort

sess_a = ort.InferenceSession('model_part_a.onnx')
sess_b = ort.InferenceSession('model_part_b.onnx')

def run(x):
    boundary = sess_a.run(['boundary'], {'input': x})[0]
    return sess_b.run(['output'], {'boundary': boundary})[0]
```

This is the baseline pattern. All more complex cases (skip connections, multi-input
decoders, attention layers) follow the same structure — the only difference is the
number and shape of tensors crossing the boundary.

### 4.1 Identify a split point

A good split point satisfies all of the following:

1. **Clean tensor boundary** — the cut falls on a named intermediate tensor that is
   already computed and stored in memory (not mid-operation). Avoid cutting inside a
   residual block or attention layer where intermediate activations are reused.

2. **Minimal interface width** — the tensors crossing the boundary (outputs of Part A,
   inputs of Part B) should be as few and as small as possible. A single feature-map
   tensor is ideal; many skip connections are acceptable but increase inter-part I/O cost.

3. **Roughly balanced weight distribution** — each part should hold approximately half
   the total parameters so both context binaries stay below the SMMU limit.

4. **No shared mutable state** — neither part should depend on internal state (e.g.
   running statistics, KV-cache) that is modified by the other part during a single
   forward pass.

5. **Boundary tensor magnitude** — check the value range of every tensor that crosses
   the boundary. If any boundary tensor has values outside roughly `[-50, 50]` in
   FP32, FP16 accumulation error in the receiving part may cause significant numerical
   divergence. Prefer split points where all boundary tensors have `std < 5` and
   `abs(max) < 50`. If large-magnitude tensors must cross the boundary, see
   §5 (FP16 Precision at Split Boundaries).

**How to find the split point**:

```python
# Print cumulative parameter count at each named module boundary
total = sum(p.numel() for p in model.parameters())
cumulative = 0
for name, module in model.named_children():
    params = sum(p.numel() for p in module.parameters())
    cumulative += params
    print(f"{name:40s}  {params/1e6:6.1f}M  cumulative {cumulative/total*100:5.1f}%")
# Look for a child module near the 50% cumulative mark
```

**Common split patterns by architecture**:

| Architecture | Natural split point | Part A outputs | Part B inputs |
|---|---|---|---|
| Sequential (ResNet, VGG, EfficientNet) | Middle layer block | Single feature map | Same feature map |
| Encoder-decoder (UNet, SegNet) | End of encoder / bottleneck | Bottleneck + skip tensors | Bottleneck + skip tensors |
| Two-tower (CLIP, dual-encoder) | Already separate towers | — | — (export as separate models) |
| Transformer (ViT, BERT) | Middle transformer block | Hidden state `[B, T, D]` | Same hidden state |
| Autoregressive decoder | Embedding + first N layers | Hidden state | Hidden state |

> **Two-tower models** (CLIP, dual-encoder retrieval): the towers are already independent
> — export them as separate ONNX files rather than splitting a single graph.
> See `references/multi_component_pipeline.md`.

### 4.2 Write Part A and Part B wrapper modules

The general pattern is the same for any architecture:

```python
import torch

class ModelPartA(torch.nn.Module):
    """Runs the model from input up to the split point.
    Outputs: all tensors needed by Part B (intermediate + any skip connections).
    """
    def __init__(self, model):
        super().__init__()
        # Assign only the sub-modules belonging to Part A
        self.part = model.first_half   # adapt to your model's attribute names

    def forward(self, *inputs):
        # Run Part A sub-modules
        x = self.part(*inputs)
        # Return every tensor Part B needs — name them clearly
        return x   # or (x, skip1, skip2, ...) if skip connections cross the boundary


class ModelPartB(torch.nn.Module):
    """Runs the model from the split point to the output.
    Inputs: all tensors produced by Part A.
    """
    def __init__(self, model):
        super().__init__()
        self.part = model.second_half  # adapt to your model's attribute names

    def forward(self, x, *extra_inputs):
        # Run Part B sub-modules
        return self.part(x, *extra_inputs)
```

**Rules**:
- Assign only the sub-modules each part needs — do not hold a reference to the full model
  inside a part wrapper (it would double the memory footprint during export).
- All tensors crossing the boundary must appear as explicit `forward()` arguments and
  return values — no implicit state, no `self` attributes mutated during forward.
- Keep `forward()` signatures flat (no `*args` with variable length if avoidable) so
  ONNX export can assign stable input/output names.

### 4.3 Trace output shapes before export

Always trace Part A first to get the exact shapes of the boundary tensors, then use those
shapes to build Part B dummy inputs:

```python
with torch.no_grad():
    out_a = part_a(*dummy_inputs_a)

# out_a may be a single tensor or a tuple
if isinstance(out_a, torch.Tensor):
    boundary_shapes = [tuple(out_a.shape)]
    boundary_tensors = [out_a]
else:
    boundary_shapes = [tuple(t.shape) for t in out_a]
    boundary_tensors = list(out_a)

print("Boundary tensor shapes:", boundary_shapes)
# Use these shapes to construct dummy inputs for Part B
dummy_inputs_b = tuple(torch.zeros(*s) for s in boundary_shapes)
```

### 4.4 Export each part to ONNX

```python
import onnx

os.makedirs(ONNX_DIR, exist_ok=True)

# Part A
torch.onnx.export(
    part_a,
    dummy_inputs_a,
    f"{ONNX_DIR}/{MODEL_NAME}_part_a.onnx",
    opset_version=18,
    input_names=part_a_input_names,    # e.g. ["input"]
    output_names=part_a_output_names,  # e.g. ["bottleneck", "skip_0", "skip_1"]
    dynamic_axes=None,
    dynamo=False,
)
onnx.checker.check_model(f"{ONNX_DIR}/{MODEL_NAME}_part_a.onnx")  # path-based for >2GB

# Part B
torch.onnx.export(
    part_b,
    dummy_inputs_b,
    f"{ONNX_DIR}/{MODEL_NAME}_part_b.onnx",
    opset_version=18,
    input_names=part_b_input_names,    # must match part_a_output_names for boundary tensors
    output_names=part_b_output_names,  # e.g. ["output"]
    dynamic_axes=None,
    dynamo=False,
)
onnx.checker.check_model(f"{ONNX_DIR}/{MODEL_NAME}_part_b.onnx")
```

### 4.5 Convert and generate context binaries for each part

```bash
export QAIRT_TMP_DIR=/workspace/qairt_tmp   # set if /tmp is full

for PART in {MODEL_NAME}_part_a {MODEL_NAME}_part_b; do
  # Convert
  python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
    --onnx        {ONNX_DIR}/${PART}.onnx \
    --output-root {OUTPUT_DIR} \
    --precision   16 \
    --preserve-io-mode datatype \
    --target-arch {TARGET_ARCH}

  # Create per-part SoC config (graph_names must match the part name)
  echo "{\"graphs\":[{\"graph_names\":[\"${PART}\"],\"vtcm_mb\":0,\"O\":3}],\
\"devices\":[{\"soc_id\":{SOC_ID},\"dsp_arch\":\"{DSP_ARCH}\",\
\"cores\":[{\"perf_profile\":\"burst\",\"rpc_control_latency\":50}]}]}" \
    > /tmp/soc{SOC_ID}_{DSP_ARCH}_${PART}.conf

  echo "{\"backend_extensions\":{\"shared_library_path\":\
\"$QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtpNetRunExtensions.so\",\
\"config_file_path\":\"/tmp/soc{SOC_ID}_{DSP_ARCH}_${PART}.conf\"}}" \
    > /tmp/soc{SOC_ID}_{DSP_ARCH}_${PART}.json

  # Generate context binary
  $QAIRT_SDK_ROOT/bin/x86_64-linux-clang/qnn-context-binary-generator \
    --backend     $QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtp.so \
    --model       {OUTPUT_DIR}/test_libs_${PART}_fp16_{TARGET_ARCH}/{TARGET_ARCH}/lib${PART}.so \
    --binary_file lib${PART}.so \
    --output_dir  {OUTPUT_DIR} \
    --config_file /tmp/soc{SOC_ID}_{DSP_ARCH}_${PART}.json
done

# Verify each part is below the SMMU limit
ls -lh {OUTPUT_DIR}/lib{MODEL_NAME}_part_*.so.bin
# Each should be < 800 MB
```

### 4.6 Deploy with ONNX-match naming

```bash
# Deploy context binaries with ONNX-matching names
for PART in {MODEL_NAME}_part_a {MODEL_NAME}_part_b; do
  cp {OUTPUT_DIR}/lib${PART}.so.bin {ONNX_DIR}/${PART}.onnx.so.bin
done

# Copy all required files to target (ONNX + context binary + YAML — all three)
scp {ONNX_DIR}/{MODEL_NAME}_part_*.onnx \
    {ONNX_DIR}/{MODEL_NAME}_part_*.onnx.so.bin \
    {ONNX_DIR}/{MODEL_NAME}_part_*.yaml \
    ubuntu@<target>:<workdir>/onnx_models/
```

> ⚠️ Always deploy the `.yaml` alongside `.onnx` — the wrapper uses it to restore ONNX
> output order after HTP reorders tensors at compile time.

### 4.7 Chain parts in the inference script

```python
import onnxruntime as ort

def load_session(path):
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    return ort.InferenceSession(str(path), sess_options=opts)

part_a_sess = load_session(f"onnx_models/{MODEL_NAME}_part_a.onnx")
part_b_sess = load_session(f"onnx_models/{MODEL_NAME}_part_b.onnx")

def run_model(*inputs):
    # Run Part A
    out_a = part_a_sess.run(None, dict(zip(part_a_input_names, inputs)))

    # Pass Part A outputs directly as Part B inputs
    # (boundary tensor names must match between parts)
    out_b = part_b_sess.run(part_b_output_names,
                            dict(zip(part_b_input_names, out_a)))
    return out_b[0]
```

Run via `aipc` wrapper:

```bash
export QAI_QNN_LIBS_DIR=$QAIRT_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
export LD_LIBRARY_PATH=$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH
export ADSP_LIBRARY_PATH=$QAIRT_SDK_ROOT/lib/hexagon-v{DSP_ARCH}/unsigned
export QAI_QNN_RUNTIME=HTP

python aipc infer_{MODEL_NAME}.py
```

---

## 5. FP16 Precision at Split Boundaries

When a model is split and each part is converted to FP16, the intermediate tensors that
cross the part boundary are computed in FP16 by the receiving part. If those tensors
contain large values, FP16 accumulation error in the receiving part's convolutions can
cause significant numerical divergence — even when each part passes CPU validation
individually.

### Diagnosis

Run each part in isolation on the target device and compare output statistics against
the CPU FP32 baseline:

```python
# CPU FP32 reference
out_cpu = part_b_cpu.run(None, inputs)[0]
print(f"CPU:  std={out_cpu.std():.4f}  range=[{out_cpu.min():.3f},{out_cpu.max():.3f}]")

# HTP FP16 (via aipc wrapper on target)
out_htp = part_b_htp.run(None, inputs)[0]
print(f"HTP:  std={out_htp.std():.4f}  range=[{out_htp.min():.3f},{out_htp.max():.3f}]")
```

**Warning signs**:
- HTP output `std` is more than 20% smaller than CPU FP32 → FP16 underflow/saturation
- HTP output range is significantly narrower than CPU → large activations being clipped
- Denoising loop diverges (latent `std` grows unboundedly) → accumulated error per step

**Root cause**: FP16 has ~3 decimal digits of precision. When skip connections carry
values in the range `[-114, 8]` (as seen in SD1.5 `up_blocks[0]` residuals), the
receiving FP16 convolution accumulates rounding errors that compound over many channels.
The output is systematically scaled down, causing the denoising loop to diverge.

### Fix options (in order of preference)

**Option 1 — Move the split point** (preferred)

Choose a split boundary where all crossing tensors have small magnitude. For UNet-style
models, the encoder output (bottleneck) is typically well-behaved (`std ≈ 2–3`), while
decoder skip connections from deep up-blocks can be large (`std > 5`, range > ±50).
Prefer splitting at the encoder/bottleneck boundary rather than inside the decoder.

**Option 2 — Accept JIT `.so` fallback for the problematic part**

If the context binary for a large part fails to load (SMMU) or produces wrong FP16
outputs, the JIT `.so` path compiles the graph on-device at first load. JIT uses the
same FP16 weights but the on-device compiler may apply different precision strategies.
This is acceptable for bring-up and non-latency-critical deployments.

**Option 3 — Reduce precision further (INT8)**

INT8 quantization reduces context binary size by ~4× vs FP32 (vs ~2× for FP16), which
may bring the binary below the SMMU limit without splitting. INT8 also uses fixed-point
arithmetic that is less susceptible to the large-activation FP16 issue. Use
`aipc_convert_int.py` with calibration data.

**Option 4 — `--custom_io` with FP32 boundary tensors** (advanced)

Force specific boundary tensors to FP32 I/O using `--custom_io`. This keeps the
internal ops in FP16 but ensures the inter-part tensors are exchanged in FP32,
preventing precision loss at the boundary.

> ⚠️ **`--custom_io` constraints**:
> - Only valid for non-quantized models (`--float_bitwidth 16` or `32`).
> - Must specify `Layout: Model/Custom` explicitly for all 4D tensors (NCHW) to avoid
>   silent transpose of the output tensor.
> - The graph name in the context binary changes to `<model>_customio` — rename the
>   `.so.bin` to match the ONNX filename for wrapper discovery.
> - Validate output shape and `std` on target before accepting.

```yaml
# Example custom_io.yaml — force boundary tensor to float32, preserve NCHW layout
 - IOName: boundary_tensor
   Layout:
     Model: NCHW
     Custom: NCHW
   Datatype: float32
```

```bash
qnn-onnx-converter -i model_part_b.onnx -o model_part_b.cpp \
  --float_bitwidth 16 --custom_io custom_io.yaml
```

### Validated SD1.5 UNet split (QCS9075, QAIRT 2.47)

The 4-part split (down+mid | up[0] | up[1] | up[2]+up[3]+conv_out) was tested on
QCS9075 (soc_id=77, dsp_arch=v73). Results:

| Part | Contents | `.so.bin` | CPU std | HTP std | Status |
|---|---|---|---|---|---|
| A | conv_in + time_emb + down_blocks + mid_block | 671 MB | 2.71 | 2.71 | ✅ Match |
| B | up_blocks[0] | 311 MB | 2.01 | 2.01 | ✅ Match |
| C | up_blocks[1] | 495 MB | 5.48 | **4.36** | ❌ FP16 error |
| D | up_blocks[2]+[3]+conv_out | 178 MB | 1.00 | **0.23** | ❌ FP16 error |

Parts C and D diverge because `up_blocks[0]` skip connections (Part B outputs) carry
values in the range `[-114, 8]` — large enough to cause FP16 accumulation error in
the receiving convolutions.

**Working solution**: use the original single-model UNet context binary (1.7 GB).
Despite the SMMU `fastrpc memory map` errors at load time, the binary loads successfully
on QCS9075 (the errors are non-fatal on this device). The single-model FP16 UNet
produces numerically correct outputs because the compiler optimizes the full graph
end-to-end, avoiding the inter-part precision boundary.

**Key lesson**: SMMU map errors at context binary load time are not always fatal.
Test whether inference actually completes before concluding the binary cannot be used.

## 6. Validation After Splitting

Run a numerical comparison between the original single-model ONNX CPU output and the
chained split output on the same input:

```python
import numpy as np, onnxruntime as ort

# Baseline: original unsplit model on CPU
orig_sess = ort.InferenceSession(f"onnx_models/{MODEL_NAME}.onnx")
out_orig = orig_sess.run(None, dict(zip(orig_input_names, dummy_inputs)))[0]

# Split: chained parts via aipc (QNN HTP)
out_split = run_model(*dummy_inputs)

cosine = float(np.dot(out_orig.flatten(), out_split.flatten()) /
               (np.linalg.norm(out_orig) * np.linalg.norm(out_split)))
print(f"Cosine similarity (orig vs split): {cosine:.4f}")
# Pass criteria: >= 0.99 (FP16), >= 0.95 (INT8)
```

---

## 7. Summary Table

| Approach | Context binary | Load time | Production-ready | When to use |
|---|---|---|---|---|
| **JIT `.so` fallback** | Not used | Slow (first call) | ❌ No | Bring-up, validation, non-latency-critical |
| **Model split** | One per part | Fast (pre-compiled) | ✅ Yes | Production, fixed-SoC, latency-critical |
| **Reduce precision** (INT8) | Smaller binary | Fast | ✅ Yes | When accuracy loss is acceptable |

---

## 8. Known Issues

| Symptom | Cause | Fix |
|---|---|---|
| `fastrpc memory map … error: 0x1` | Context binary > DSP SMMU window | Split model or use JIT fallback |
| `err 1002` / `Failed to initialize graph memory` | Same as above | Same as above |
| VTCM sweep does not change binary size | VTCM controls on-chip scratch, not weight mapping | Use model split — VTCM is not the constraint |
| Part B input shapes mismatch at export | Boundary tensor shapes not traced before export | Trace Part A with `torch.no_grad()` and print all output shapes first |
| `autogen.yaml` created but outputs wrong | JIT fallback active — YAML auto-generated without ONNX I/O info | Deploy correct `.yaml` from `aipc_inspect_onnxio.py` alongside `.onnx` |
| Part A holds reference to full model | Wrapper assigned full model, not sub-modules | Assign only the sub-modules each part needs in `__init__` |
| HTP part output `std` is 20–80% smaller than CPU FP32 | FP16 accumulation error from large boundary tensors (skip connections with range > ±50) | Move split point away from large-activation boundaries; or use INT8; or test JIT `.so` fallback |
| Denoising loop diverges (latent `std` grows unboundedly) | Accumulated FP16 error per step from wrong UNet output scale | Diagnose per-part output `std` on HTP vs CPU; fix the part with the largest deviation |
| `fastrpc memory map … error: 0x1` at load but inference still completes | SMMU map error is non-fatal on some devices (e.g. QCS9075) | Test whether inference actually produces correct output before concluding the binary is unusable |
| `--custom_io` output tensor has wrong shape (e.g. NHWC instead of NCHW) | Layout not specified in custom_io config — HTP returns native NHWC | Add `Layout: Model: NCHW / Custom: NCHW` for all 4D tensors in custom_io.yaml |

---

## 9. Special Case — UNet (Encoder-Decoder with Skip Connections)

UNet-style models (Stable Diffusion UNet, SegNet, U-Net medical imaging) have skip
connections that carry feature maps from the encoder to the decoder. These tensors must
cross the Part A → Part B boundary explicitly, making the interface wider than a simple
sequential split.

> ⚠️ **FP16 precision warning**: UNet decoder skip connections (from `up_blocks[0]`
> onwards) can carry large values (range > ±50, `std > 5`). Splitting inside the
> decoder causes FP16 accumulation error in the receiving part. **Prefer splitting at
> the encoder/bottleneck boundary** (after `down_blocks + mid_block`, before any
> `up_blocks`). If the resulting Part B is still too large for the SMMU window, test
> whether the single-model context binary loads despite SMMU warnings before splitting
> further. See §5 for diagnosis and fix options.

**Split point**: after the encoder (down-blocks) and bottleneck (mid-block), before the
decoder (up-blocks). This is the natural architectural boundary and produces roughly
equal parameter counts on each side.

**Interface tensors**:
- `bottleneck`: the mid-block output feature map `[B, C, H, W]`
- `skip_0 … skip_N`: all residual feature maps from the down-blocks, in order
  (these are passed to the up-blocks as `res_hidden_states_tuple`)

**Part A wrapper** (encoder + bottleneck → bottleneck + skips):

```python
class UNetPartA(torch.nn.Module):
    def __init__(self, unet):
        super().__init__()
        # Assign only encoder sub-modules — do NOT hold the full unet
        self.get_time_embed  = unet.get_time_embed
        self.time_embedding  = unet.time_embedding
        self.conv_in         = unet.conv_in
        self.down_blocks     = unet.down_blocks
        self.mid_block       = unet.mid_block

    def forward(self, sample, timestep, encoder_hidden_states):
        t_emb = self.get_time_embed(sample=sample, timestep=timestep)
        emb   = self.time_embedding(t_emb)
        sample = self.conv_in(sample)

        down_block_res_samples = (sample,)
        for down_block in self.down_blocks:
            sample, res_samples = down_block(
                hidden_states=sample,
                temb=emb,
                encoder_hidden_states=encoder_hidden_states,
            )
            down_block_res_samples += res_samples

        sample = self.mid_block(sample, emb,
                                encoder_hidden_states=encoder_hidden_states)
        # Return bottleneck + all skip tensors + time embedding
        # (time embedding is needed by up-blocks in Part B)
        return (sample, emb) + down_block_res_samples
```

**Part B wrapper** (bottleneck + skips → output):

```python
class UNetPartB(torch.nn.Module):
    def __init__(self, unet):
        super().__init__()
        # Assign only decoder sub-modules
        self.up_blocks   = unet.up_blocks
        self.conv_norm_out = getattr(unet, "conv_norm_out", None)
        self.conv_act      = getattr(unet, "conv_act", None)
        self.conv_out      = unet.conv_out

    def forward(self, sample, emb, encoder_hidden_states, *down_block_res_samples):
        for up_block in self.up_blocks:
            res = down_block_res_samples[-len(up_block.resnets):]
            down_block_res_samples = down_block_res_samples[:-len(up_block.resnets)]
            sample = up_block(
                hidden_states=sample,
                temb=emb,
                res_hidden_states_tuple=res,
                encoder_hidden_states=encoder_hidden_states,
            )
        if self.conv_norm_out: sample = self.conv_norm_out(sample)
        if self.conv_act:      sample = self.conv_act(sample)
        return self.conv_out(sample)
```

> ⚠️ The exact internal API (`get_time_embed`, `down_block` return signature, etc.)
> depends on the `diffusers` version. Always inspect `unet.forward()` source before
> writing wrappers. The pattern above targets `diffusers >= 0.20`.

**Tracing boundary shapes** (required before export):

```python
part_a = UNetPartA(unet).eval()
with torch.no_grad():
    out_a = part_a(dummy_sample, dummy_timestep, dummy_enc_hid)
# out_a[0] = bottleneck, out_a[1] = emb, out_a[2:] = skip tensors
print("bottleneck:", out_a[0].shape)
print("emb:       ", out_a[1].shape)
print("skips:     ", [t.shape for t in out_a[2:]])
```

**Output names** for ONNX export:

```python
part_a_output_names = (
    ["bottleneck", "time_emb"]
    + [f"skip_{i}" for i in range(len(out_a) - 2)]
)
part_b_input_names = (
    ["bottleneck", "time_emb", "encoder_hidden_states"]
    + [f"skip_{i}" for i in range(len(out_a) - 2)]
)
```

---

## See Also

- `references/host_context_binary_gen.md` — VTCM sweep, soc_id/dsp_arch config
- `references/multi_component_pipeline.md` — per-component export/conversion loop
- `references/troubleshooting.md` — transport errors, skel load failures
- `references/model_export_validation.md` — `dynamo=False` for diffusers, `check_model` path API
