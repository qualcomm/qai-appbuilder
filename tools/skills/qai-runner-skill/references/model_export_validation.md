# Model Export,Patch and Validation

This guide covers the best practices for exporting source models to ONNX and validating them before QNN conversion.

## 1. Export to ONNX

### Precision Recommendation

Prefer exporting ONNX in **FP32** for maximum compatibility.

If reduced precision is required, **FP16** is generally better supported than **BF16** across ONNX tooling and downstream QNN conversion flows.

BF16 export may be possible by casting the model and inputs to `torch.bfloat16`, but support is toolchain-dependent and should be treated as experimental unless validated end-to-end.

If you choose BF16, validation in [Section 2](#2-validation-workflow) is mandatory, and you must also confirm the downstream QNN converter/runtime accepts the exported graph.

Always prefer using a **dedicated Python script** for exporting models. This approach is superior to CLI commands because it allows for:
- **Reproducibility**: The export parameters are locked in code.
- **Debugging**: You can easily inspect the model state before export.
- **In-Memory Patching**: You can fix unsupported operators without modifying the library source code.


> **Transformer decoder models**: If the model uses a decoder-only or decoder-side transformer structure, validate it as separate prefill and decode graphs instead of a single generic ONNX export. Use `skills/aipc-toolkit/references/transformer_models_qairt.md` for the transformer-specific ONNX contract, KV-cache I/O requirements, causal-mask patch guidance, and prefill/decode validation workflow.



### Opset Version Guidance

Choose the ONNX opset version carefully to avoid QNN conversion issues:

| Opset | Status | Notes |
|-------|--------|-------|
| 13 | Safe | Widely supported, but lacks some newer ops |
| 17 | **Avoid** | `Resize` op version conversion fails; use 18+ |
| 18 | **Recommended** | Best QNN compatibility; default for PyTorch 2.x exporter |
| 19?20 | May work | Watch for op version warnings during conversion |

```python
torch.onnx.export(
    model,
    dummy_input,
    "model.onnx",
    opset_version=18,  # Recommended for QNN
)
```

> **Known benign warnings**: Some ops (e.g., `LeakyRelu` at opset 16+) produce `WARNING_OP_VERSION_NOT_SUPPORTED` during QNN conversion. These are warnings, not errors ? the converter handles them internally. Do not waste time downgrading opset to suppress them.

### PyTorch 2.9+ Export API Changes

PyTorch 2.9 made the new `torch.export`-based ONNX exporter the default.
The legacy TorchScript-based exporter is still available via `dynamo=False`.

**Two export paths:**

| PyTorch version | Default exporter | Parameter | Best for |
|----------------|-----------------|-----------|----------|
| < 2.9 | TorchScript (legacy) | `dynamic_axes` | All models |
| >= 2.9 | `torch.export`-based (new) | `dynamic_shapes` | Simple models, control flow |
| >= 2.9 | TorchScript (legacy) | `dynamo=False` + `dynamic_axes` | Models with `*args` in forward |

**When to use `dynamo=False` (legacy exporter):**

The new exporter fails for models whose `forward()` signature uses `*args`
(e.g., KV-cache decoders that accept a variable number of past KV tensors).
The pytree structure mismatch produces this error:

```
ValueError: treespec.unflatten(leaves): `leaves` has length 62 but the spec
refers to a pytree that holds 3 items
```

Use `dynamo=False` after an in-memory operator patch if the new exporter optimizes the patched graph
back into an unsupported operator or fails to honor the requested lower opset. Re-export, inspect the
ONNX op list, and rerun the converter dry-run before continuing.

**Legacy exporter (recommended for KV-cache decoders):**

```python
torch.onnx.export(
    model,
    (input_ids, position_ids, *past_kv_flat),
    'model.onnx',
    input_names=input_names,
    output_names=output_names,
    dynamic_axes=dynamic_axes,
    opset_version=18,
    dynamo=False,  # Required for models with *args
)
```

**New exporter (simple models, PyTorch >= 2.9):**

```python
from torch.export import Dim

dynamic_shapes = {
    'input': {0: Dim('batch'), 2: Dim('height'), 3: Dim('width')},
}
torch.onnx.export(
    model,
    (dummy_input,),
    'model.onnx',
    dynamic_shapes=dynamic_shapes,
    opset_version=18,
)
```

**Common export errors and fixes:**

| Error | Cause | Fix |
|-------|-------|-----|
| `treespec.unflatten(leaves): leaves has length N but spec holds M items` | New exporter with `*args` model | Add `dynamo=False` |
| `Failed to convert 'dynamic_axes' to 'dynamic_shapes'` | New exporter with `dynamic_axes` | Use `dynamo=False` or switch to `dynamic_shapes` |
| `UserWarning: 'dynamic_axes' is not recommended when dynamo=True` | Mixed API usage | Add `dynamo=False` |
| `torch._dynamo.exc.UserError: Detected mismatch` | `dynamic_shapes` structure wrong | Match `dynamic_shapes` to model's pytree structure |

### Operator Patching

For detailed guidance on patching unsupported operators (e.g., `Einsum`, `GridSample`), see **[In-Memory Operator Patching](operator_patching.md)**.

**Quick template:**
```python
import torch
import types

def patch_model_for_qnn(model):
    def patched_forward(self, x):
        # Implementation using MatMul, Reshape, Transpose, etc.
        return ...

    # Replace the forward method of a specific layer instance
    # This does NOT change the installed python package
    for name, module in model.named_modules():
        if isinstance(module, TargetLayerClass):
            module.forward = types.MethodType(patched_forward, module)

# Usage
model = load_original_model()
patch_model_for_qnn(model)
torch.onnx.export(model, dummy_input, "model.onnx", opset_version=13)
```

> ⚠️ **Validation is mandatory after patching** — see [Section 2](#2-validation-workflow).

## 2. Validation Workflow

After export (and especially after patching), you must verify that the ONNX model's output matches the original model's output.

```python
import numpy as np
import onnxruntime as ort

# Run both models on the same preprocessed input
original_output = original_model(input_data)
onnx_session = ort.InferenceSession("model.onnx")
onnx_output = onnx_session.run(None, {"input": input_data})

```

**Note:** Small numerical differences are common. **Confirm with the user** if the error is acceptable for their use case.

### `onnx.checker.check_model()` — large model caveat (>2 GB)

For models whose protobuf exceeds 2 GB, loading the full model object before checking raises:

```
ValueError: This protobuf of onnx model is too large (>2GB).
Call check_model with model path instead.
```

**Fix**: always pass the **file path string**, not the loaded model object:

```python
import onnx

# ✅ Correct — works for any size
onnx.checker.check_model("model.onnx")

# ❌ Fails for models > 2 GB
model = onnx.load("model.onnx")
onnx.checker.check_model(model)   # ValueError
```

This applies to all validation steps (post-export, post-patch, post-simplify).

### Task-Specific Validation (Recommended)
For computer vision tasks like object detection:
- **Visual Check**: Generate annotated images from both models and compare them.
- **Result Check**: Compare high-level outputs (bounding box coordinates, class labels, and confidence scores).

If the detection results are identical or very similar, the model is likely safe for conversion even if there is a minor numerical MSE.

## 3. Post-Patching Importance
If you have applied an operator replacement patch, functional validation is **mandatory**. AI-generated or manual patches can occasionally introduce off-by-one errors or axis misalignments that raw numerical checks might miss but visual checks will catch.

## 4. ONNX External Data Files

PyTorch 2.9+ uses a `torch.export`-based ONNX exporter by default. For models with large weights,
it automatically splits the output into two files:

```
model.onnx        ← graph structure only (small, e.g. 300 KB)
model.onnx.data   ← weight tensors (large, e.g. 32 MB+)
```

This is the ONNX external data format. Both files must be present together for any downstream tool
(`onnxruntime`, `qnn-onnx-converter`, `onnxsim`, deployment to target) to work.

### Check after export

```bash
ls *.onnx.data 2>/dev/null && echo "⚠️  external data present — treat as a pair"
```

### Conversion

`qnn-onnx-converter` and `aipc_convert_fp.py` handle external data automatically as long as
`.onnx` and `.onnx.data` are in the same directory. Do not move one without the other.

### Deployment to target

Always copy both files together:

```bash
# ✅ Correct — copies both
scp model.onnx model.onnx.data ubuntu@target:/workdir/

# ❌ Wrong — leaves .data behind, ORT will fail with:
#    "filesystem error: cannot get file size: No such file or directory [model.onnx.data]"
scp model.onnx ubuntu@target:/workdir/
```

If you use a glob, make sure it covers both:

```bash
scp *.onnx *.onnx.data ubuntu@target:/workdir/ 2>/dev/null || true
```

### `onnxsim` and external-data models

`onnxsim.simplify()` may fail on models that use the external-data format with:

```
onnx.onnx_cpp2py_export.checker.ValidationError:
    The model does not have an ir_version set properly.
```

This happens because `onnxsim` loads the full protobuf in-memory and the external-data
round-trip can corrupt the `ir_version` field for very large models.

**Workaround**: wrap `onnxsim` in a `try/except` and skip silently on failure — the model
is still valid for conversion even without simplification:

```python
import onnx, onnxsim

onnx.checker.check_model("model.onnx")   # path-based, always safe

try:
    model = onnx.load("model.onnx")
    model_sim, ok = onnxsim.simplify(model)
    if ok:
        onnx.save(model_sim, "model.onnx")
        print("Simplified OK")
    else:
        print("onnxsim: no change")
except Exception as e:
    print(f"onnxsim skipped ({e})")
    # model.onnx is still valid — proceed to conversion
```

> ⚠️ Do **not** block conversion on an `onnxsim` failure. If `onnx.checker.check_model()`
> passes, the model is ready for `qnn-onnx-converter`.

### Legacy exporter (`dynamo=False`)

The TorchScript-based exporter (`dynamo=False`) embeds weights inline — it produces a single
self-contained `.onnx` file with no `.data` companion. If you need a single-file artifact
(e.g. for simpler deployment), use `dynamo=False` for the export.

> **`diffusers` pipeline components**: Always use `dynamo=False` when exporting
> `UNet2DConditionModel`, `AutoencoderKL`, `CLIPTextModel`, or any other `diffusers`
> sub-model. The new `torch.export`-based exporter (default in PyTorch ≥ 2.9) can fail
> on these models with shape-guard or pytree errors. `dynamo=False` (TorchScript legacy
> exporter) is the stable, tested path for all diffusion pipeline components.
>
> Also note: `StableDiffusionPipeline` and similar pipeline objects are **not**
> `torch.nn.Module` subclasses — they have no `.eval()` method. Call `.eval()` on each
> extracted sub-model (`pipe.unet.eval()`, `pipe.vae.eval()`, etc.) individually.
