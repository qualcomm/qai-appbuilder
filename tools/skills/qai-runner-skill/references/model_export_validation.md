# Model Export, Patch and Validation

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


> **Transformer decoder models**: If the model uses a decoder-only or decoder-side transformer structure, validate it as separate prefill and decode graphs instead of a single generic ONNX export. Use `references/transformer_models_qairt.md` for the transformer-specific ONNX contract, KV-cache I/O requirements, causal-mask patch guidance, and prefill/decode validation workflow.

### ⏱️ Export Performance — Large Models Can Be Very Slow

For large models (e.g., Real-ESRGAN x4plus, 16M params), ONNX export time scales with input size:

| Input size | Approx. MACs | Export time estimate |
|-----------|-------------|---------------------|
| 256×256 | ~1175G | 2-5 min |
| 512×512 | ~4700G | **15-30+ min** |

**Recommended `torch.onnx.export` settings for large models:**

```python
torch.onnx.export(
    model, dummy_input, "model.onnx",
    opset_version=13,           # opset 13 is faster than 11 for large models
    do_constant_folding=False,  # skip constant folding — saves 10-20 min for large models
                                # QAIRT qnn-onnx-converter handles its own optimizations
    input_names=["input"],
    output_names=["output"],
)
```

> ⚠️ `do_constant_folding=True` (default) can add **15-30 minutes** to export time for large models
> because torch traces and folds every constant subgraph. Set to `False` for faster export.
> The QAIRT converter performs its own graph optimizations independently.

> ⚠️ **Use `timeout=0` for ONNX export** — model export time varies widely by model size.
> See SKILL.md for reference times (not for setting timeouts).


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
#### Common import error: `basicsr` → `torchvision.transforms.functional_tensor`

`basicsr/data/degradations.py:8` hard-imports `functional_tensor`, which was removed in torchvision ≥0.16, raising
`ModuleNotFoundError: No module named 'torchvision.transforms.functional_tensor'` (common for basicsr-dependent models such as Real-ESRGAN). Change that line to support both paths:
```python
try:
    from torchvision.transforms.functional_tensor import rgb_to_grayscale
except ImportError:
    from torchvision.transforms.functional import rgb_to_grayscale
```

### ⚠️ Model Architecture Modifications Before Export

Some models have branches or features that are valid in PyTorch but cause issues during QNN conversion.
**Always check if the model has optional branches that can be disabled before export.**

#### Pattern: Auxiliary Output Branches

Some models have auxiliary classifier branches used only during training.
These branches often contain operators (e.g., `Gather`, `Reshape` with dynamic shapes) that
QAIRT 2.45 `qnn-onnx-converter` cannot handle, causing `ReshapeOp::calculateShape` errors.

**General approach — disable training-only branches before export:**
```python
# Pattern: load with default settings, then disable training-only branches
model = SomeModel(weights=SomeModel_Weights.DEFAULT)

# Disable auxiliary branches (training-only, not needed for inference)
# Check model documentation or source for attribute names
if hasattr(model, 'aux_logits'):
    model.aux_logits = False   # disable aux branch flag
if hasattr(model, 'AuxLogits'):
    model.AuxLogits = None     # remove aux classifier module

model.eval()
torch.onnx.export(model, dummy_input, "model.onnx", opset_version=13)
```

> ⚠️ **Note**: Some model constructors enforce `aux_logits=True` when loading pretrained weights.
> In that case, set the attribute **after** loading (not via constructor argument).

**Symptom when aux branch causes conversion failure:**
```
ValueError: modeltools::ops::ReshapeOp::calculateShape: Unable to calculate ReshapeOp output shape
for op Reshape_post_/Gather. shape params dont result in same cumulative total sum.
89401 != -1104974904   <- garbage value indicates unresolved dynamic shape
```

**Other common training-only branches to check:**
- `model.training` mode — always call `model.eval()` before export
- Dropout layers — automatically disabled in eval mode
- BatchNorm running stats — use eval mode to freeze them
- Custom `forward()` with `if self.training:` branches — verify eval path is correct

### ⚡ ONNX Export Performance & Memory Optimization Rules (full set)

> SKILL.md Core Workflow Step 1 keeps only **Rule 5 (always FP32)** and **Rule 7 (always opset 18,
> no torch version check)** inline. The remaining rules + validated benchmark + the optimized export
> template live here. Apply ALL of these rules when writing or reviewing any `export_onnx.py`.

| # | Rule | Why |
|---|------|-----|
| 1 | `do_constant_folding=False` | True = `torch.onnx.export` folds constants 15–30+ min on large models (≥512×512 / ≥1000G MACs), can OOM. `qnn-onnx-converter` does its own folding — redundant. |
| 2 | Skip sanity forward pass for pretrained models | A 512×512 forward alone takes ~137–196s on CPU and produces 48 MB output tensors held during export. Keep it only for custom-trained / structurally modified models. |
| 3 | `del ckpt, weights` after `load_state_dict()` | Saves tens to hundreds of MB on large checkpoints. |
| 4 | If forward pass kept → `del out` before export | `torch.onnx.export` re-runs tracing internally; keeping `out` doubles peak RAM. |
| 5 | Always export FP32 ONNX (never FP16) | `qnn-onnx-converter` expects FP32; PyTorch CPU has no FP16 `Conv2d` (`slow_conv2d_cpu not implemented for 'Half'`). FP16 is applied later via `--precision 16` in QAIRT. |
| 6 | Suppress `prim::Constant shape inference missing` (torch 1.x only) | Non-fatal for opset 13 + torch 1.13.x. `warnings.filterwarnings("ignore", message="The shape inference of prim::Constant type is missing")`. Not needed on torch 2.x. |
| 7 | Always `opset_version=18` (NO `torch.__version__` check) | `python_x64_venv` is always torch 2.x (Setup-installed); torch 2.x min opset is 18 (lower auto-upgrades, downgrade fails on `Resize` etc.). Also `pip install onnxscript`. If `torchvision.transforms.functional_tensor` missing (≥0.16), patch `basicsr/data/degradations.py:8` with a `try/except ImportError` fallback to `functional.rgb_to_grayscale` (snippet above in this doc). This rule is about opset only — it does not change with the torch 2.9+ default-exporter switch below (confirmed on `python_x64_venv` torch 2.11.0); see "PyTorch 2.9+ Export API" after the export template for the separate `dynamo=False` / `dynamic_shapes` concern. |
| 8 | torch 2.x exports large models as `<model>.onnx` + `<model>.onnx.data` | Big-weight models split into graph (`.onnx`, ~hundreds KB) + weights (`.onnx.data`, e.g. 91 MB). **Both must stay in same dir** for `qnn-onnx-converter` / `onnxruntime`. Small `.onnx` file is normal — check for sibling `.onnx.data` BEFORE running diagnostic commands. See "ONNX External Data: Deployment & `onnxsim`" below for target-deployment steps and the `onnxsim` compatibility workaround. |

**Validated performance reference** (Real-ESRGAN x4plus, 512×512, 16M params, ~4700G MACs):

| Configuration | torch version | Total export time | Notes |
|---------------|--------------|------------------|-------|
| `do_constant_folding=True` + forward pass | 1.13.1 | ~275s | Baseline (unoptimized) |
| `do_constant_folding=False` + no forward pass | 1.13.1 | ~163s | ✅ Optimized (~40% faster) |
| `do_constant_folding=False` + no forward pass | **2.11.0** | **~41s** | ✅ Best (~85% faster than baseline) |

**Optimized export template:**
```python
import warnings, torch, onnx

# After model.load_state_dict():
del ckpt, weights          # Rule 3: free checkpoint memory

model.eval()
dummy = torch.zeros(1, 3, H, W, dtype=torch.float32)  # Rule 5: FP32 only
# Rule 2: skip sanity forward pass for pretrained models

with warnings.catch_warnings():                         # Rule 6: suppress warnings
    warnings.filterwarnings("ignore", message="The shape inference of prim::Constant type is missing")
    torch.onnx.export(
        model, dummy, r"${WORKSPACE}\<model_name>\<model_name>.onnx",
        opset_version=18,
        input_names=["input"], output_names=["output"],
        dynamic_axes=None,
        do_constant_folding=False,                      # Rule 1: skip constant folding
        verbose=False,
    )
```

### PyTorch 2.9+ Export API: New `torch.export` Exporter vs Legacy

Confirmed on this project's `python_x64_venv` (torch **2.11.0**, well past the 2.9 threshold):
PyTorch 2.9 made the new `torch.export`-based ONNX exporter the **default**. The legacy
TorchScript-based exporter is still available via `dynamo=False`. This is orthogonal to
Rule 7's opset choice — opset 18 is correct under either exporter.

**Two export paths:**

| PyTorch version | Default exporter | Dynamic-shape param | Best for |
|----------------|-----------------|-----------|----------|
| < 2.9 | TorchScript (legacy) | `dynamic_axes` | All models |
| >= 2.9 (our venv: 2.11.0) | `torch.export`-based (new) | `dynamic_shapes` | Simple models, control flow |
| >= 2.9 | TorchScript (legacy) via `dynamo=False` | `dynamic_axes` | Models whose `forward()` uses `*args` |

**When to force `dynamo=False` (legacy exporter):**

The new exporter fails for models whose `forward()` signature uses `*args`
(e.g., KV-cache decoders that accept a variable number of past-KV tensors — see
`genie_llm_contract.md` for our KV-cache decoder contract). The pytree structure mismatch
raises:

```
ValueError: treespec.unflatten(leaves): `leaves` has length 62 but the spec
refers to a pytree that holds 3 items
```

Also force `dynamo=False` if, after an in-memory operator patch (see
[Operator Patching](#operator-patching) below), the new exporter re-optimizes the patched
graph back into an unsupported op or silently drops the requested opset. Re-export,
re-inspect the op list with `qai_inspect_onnxio.py`, and rerun the converter dry-run before
continuing.

**Legacy exporter (`*args` models, e.g. KV-cache decoders):**
```python
torch.onnx.export(
    model,
    (input_ids, position_ids, *past_kv_flat),
    r"${WORKSPACE}\<model_name>\<model_name>.onnx",
    input_names=input_names,
    output_names=output_names,
    dynamic_axes=dynamic_axes,
    opset_version=18,
    dynamo=False,  # required — new exporter can't unflatten *args
)
```

**New exporter (default on torch >= 2.9; `dynamic_shapes` replaces `dynamic_axes`):**
```python
from torch.export import Dim

dynamic_shapes = {
    "input": {0: Dim("batch"), 2: Dim("height"), 3: Dim("width")},
}
torch.onnx.export(
    model,
    (dummy_input,),
    r"${WORKSPACE}\<model_name>\<model_name>.onnx",
    dynamic_shapes=dynamic_shapes,
    opset_version=18,
)
```

**Common export errors (new exporter):**

| Error | Cause | Fix |
|-------|-------|-----|
| `treespec.unflatten(leaves): leaves has length N but spec holds M items` | New exporter can't handle a `*args` model | Add `dynamo=False` |
| `Failed to convert 'dynamic_axes' to 'dynamic_shapes'` | Non-`None` `dynamic_axes` passed under the new exporter | Add `dynamo=False`, or switch to `dynamic_shapes` |
| `UserWarning: 'dynamic_axes' is not recommended when dynamo=True` | Mixed old/new API usage | Add `dynamo=False` |
| `torch._dynamo.exc.UserError: Detected mismatch` | `dynamic_shapes` structure doesn't match the model's actual pytree | Match `dynamic_shapes` to the model's real arg/tensor structure |

> The optimized export template above (Rules 1–8) passes `dynamic_axes=None` — since no
> dynamic axes are requested there, it is unaffected by the new/legacy exporter split.
> Only switch to `dynamic_shapes` (or add `dynamo=False`) when the model actually needs
> dynamic dimensions, or its `forward()` uses `*args`.

### ONNX External Data (`.onnx` + `.onnx.data`): Deployment & `onnxsim`

Building on Rule 8 above — beyond keeping both files in the same directory locally, two
more failure points show up once the model leaves this host:

**Deploying to a target device** (SSH/SCP path — see `remote_execution.md`):

Copy **both** files in the same command; forgetting `.onnx.data` is the most common miss:

```bash
# ✅ Correct — copies both
scp -P <port> -i <key_path> ${WORKSPACE}\<model_name>\<model_name>.onnx ${WORKSPACE}\<model_name>\<model_name>.onnx.data <user>@<host>:<working_folder>/

# ❌ Wrong — leaves .onnx.data behind
scp -P <port> -i <key_path> ${WORKSPACE}\<model_name>\<model_name>.onnx <user>@<host>:<working_folder>/
```

**Symptom of a missed `.onnx.data`:** the `.onnx` file copies cleanly and looks intact, but
on-device conversion or `onnxruntime` inference then fails with a weights-missing error
(e.g. `filesystem error: cannot get file size: No such file or directory
[<model_name>.onnx.data]`). If a glob is used instead of naming both files explicitly,
confirm it actually matches the `.data` sibling too, e.g.
`scp -P <port> -i <key_path> *.onnx *.onnx.data <user>@<host>:<working_folder>/`.

**`onnxsim` and external-data models:**

`onnxsim.simplify()` can fail on external-data models with:
```
onnx.onnx_cpp2py_export.checker.ValidationError:
    The model does not have an ir_version set properly.
```
This happens because `onnxsim` loads the full protobuf in memory, and the external-data
round-trip can corrupt the `ir_version` field on large models. `run_pipeline.py` does not
run an `onnxsim` pass itself (it hands the `.onnx` straight to `qnn-onnx-converter` /
`qairt-converter`, which do their own graph optimization) — this only matters if you add a
manual pre-simplification step in a custom export script. Wrap it in `try/except` and skip
on failure; the un-simplified model is still valid for conversion:

```python
import onnx, onnxsim

onnx.checker.check_model("<model_name>.onnx")   # path-based, always safe with external data

try:
    model = onnx.load("<model_name>.onnx")
    model_sim, ok = onnxsim.simplify(model)
    if ok:
        onnx.save(model_sim, "<model_name>.onnx")
    else:
        print("onnxsim: no change")
except Exception as e:
    print(f"onnxsim skipped ({e})")
    # <model_name>.onnx is still valid — proceed straight to run_pipeline.py
```

> Do **not** block conversion on an `onnxsim` failure — if `onnx.checker.check_model()`
> passes, hand the model straight to `run_pipeline.py` / `qnn-onnx-converter`.

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

# ✅ Allowed use of onnxruntime in this skill: CPUExecutionProvider for post-export ONNX numerical validation
# Run both models on the same preprocessed input
original_output = original_model(input_data)
onnx_session = ort.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])
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

`qnn-onnx-converter` and `qai_convert_fp.py` handle external data automatically as long as
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
