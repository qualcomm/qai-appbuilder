# Model Export, Patch and Validation

This guide covers the best practices for exporting source models to ONNX and validating them before QNN conversion.

## 1. Export to ONNX

Always prefer using a **dedicated Python script** for exporting models. This approach is superior to CLI commands because it allows for:
- **Reproducibility**: The export parameters are locked in code.
- **Debugging**: You can easily inspect the model state before export.
- **In-Memory Patching**: You can fix unsupported operators without modifying the library source code.

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
| 7 | Always `opset_version=18` (NO `torch.__version__` check) | `python_x64_venv` is always torch 2.x (Setup-installed); torch 2.x min opset is 18 (lower auto-upgrades, downgrade fails on `Resize` etc.). Also `pip install onnxscript`. If `torchvision.transforms.functional_tensor` missing (≥0.16), patch `basicsr/data/degradations.py:8` with a `try/except ImportError` fallback to `functional.rgb_to_grayscale` (snippet above in this doc). |
| 8 | torch 2.x exports large models as `<model>.onnx` + `<model>.onnx.data` | Big-weight models split into graph (`.onnx`, ~hundreds KB) + weights (`.onnx.data`, e.g. 91 MB). **Both must stay in same dir** for `qnn-onnx-converter` / `onnxruntime`. Small `.onnx` file is normal — check for sibling `.onnx.data` BEFORE running diagnostic commands. |

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

### Task-Specific Validation (Recommended)
For computer vision tasks like object detection:
- **Visual Check**: Generate annotated images from both models and compare them.
- **Result Check**: Compare high-level outputs (bounding box coordinates, class labels, and confidence scores).

If the detection results are identical or very similar, the model is likely safe for conversion even if there is a minor numerical MSE.

## 3. Post-Patching Importance
If you have applied an operator replacement patch, functional validation is **mandatory**. AI-generated or manual patches can occasionally introduce off-by-one errors or axis misalignments that raw numerical checks might miss but visual checks will catch.
