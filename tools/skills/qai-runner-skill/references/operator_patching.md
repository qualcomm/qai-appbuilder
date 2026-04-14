# In-Memory Operator Patching

> **Purpose**: Replace unsupported operators (e.g., `Einsum`, `GridSample`, `ScatterND`) with QNN/SNPE-compatible equivalents **without modifying library source code**.

---

## RULE 1: Check Input Types BEFORE Choosing a Pattern

Every unsupported operator patch depends on the **data types of its inputs**.
The same operator (e.g., `Mod`) has completely different patch strategies for
INT64 vs FLOAT inputs. Choosing the wrong pattern is the #1 cause of patch failures.

### How to Determine Input Types

When a converter or context binary error mentions an unsupported operator:

1. **Note the operator name and node** from the error log (e.g., `/model.23/Mod`)
2. **Open the ONNX model in Netron** (https://netron.app) or inspect with Python:
   ```python
   import onnx
   model = onnx.load("model.onnx")
   for node in model.graph.node:
       if node.op_type == "Mod":  # replace with your op
           print(f"Node: {node.name}")
           print(f"  Inputs: {list(node.input)}")
           print(f"  Outputs: {list(node.output)}")
   ```
3. **Check the input tensor types** — look at the producer of each input:
   - If input comes from **TopK** → it's INT64 (indices)
   - If input comes from **Conv/MatMul/Softmax** → it's FLOAT
   - If input comes from **Constant** → check the constant's dtype in Netron
4. **Match the type signature** to the Error → Action table below

### Quick Type Check Reference

| Producer Node Type | Output Tensor Type |
|-------------------|-------------------|
| TopK (indices output) | INT64 |
| Constant (dims=[], data_type=7) | INT64 scalar |
| Constant (dims=[], data_type=1) | FLOAT32 scalar |
| Conv / MatMul / Gemm | FLOAT32 |
| Softmax / Sigmoid / Relu | FLOAT32 |
| Reshape / Transpose | Inherits input type |

### Manual Type-First Decision Tree

If patching manually, follow this flow:

```
Step 1: Identify the failing operator from error log
        └─→ e.g., "Mod" at node "/model.23/Mod"

Step 2: Determine input types (see "How to Determine Input Types" above)
        └─→ INT_INT = highest success rate (type-preserving)
        └─→ FLOAT_FLOAT = lower success rate (may need Floor)
        └─→ FLOAT_CONST = medium success rate (Cast chain needed)

Step 3: Match the Error → Action table row
        └─→ Apply the suggested ONNX surgery code

Step 4: Validate
        └─→ onnx.checker.check_model()
        └─→ qnn-onnx-converter --dry_run
        └─→ Compare accuracy with original model

Step 5: If validation fails, try next-ranked pattern in table
```

### Operator Pattern Table (Type-Aware)

| Operator | Input A Type | Input B Type | Pattern | Operators Needed | QNN Compatible | Priority |
|----------|-------------|-------------|---------|-----------------|---------------|----------|
| **Mod** | INT64/32 | INT64/32 | `Sub(a, Mul(b, Div(a,b)))` | Div, Mul, Sub | ✅ Yes | ★★★★★ |
| **Mod** | FLOAT | FLOAT | `Sub(a, Mul(b, Floor(Div(a,b))))` | Div, Floor, Mul, Sub | ⚠️ Floor may fail | ★★ |
| **Mod** | FLOAT | CONST(int) | `Sub(a, Mul(b, Cast(Cast(Div(a,b),INT),FLOAT)))` | Div, Cast, Mul, Sub | ⚠️ Type mismatch risk | ★★ |
| **Floor** | INT64/32 | — | `Identity(x)` (no-op) | none | ✅ Yes | ★★★★★ |
| **Floor** | FLOAT | — | `Cast(Cast(x,INT32),FLOAT)` | Cast | ⚠️ Interpreted by QNN | ★★ |
| **Ceil** | FLOAT | — | `Neg(Floor(Neg(x)))` | Neg, Floor | ⚠️ Floor may fail | ★★ |
| **Round** | FLOAT | — | `Floor(Add(x, 0.5))` | Add, Floor | ⚠️ Floor may fail | ★★ |
| **Einsum** | FLOAT | — | Decompose to MatMul+Transpose+Reshape | MatMul, Transpose, Reshape | ✅ Yes | ★★★★ |
| **ScatterND** | FLOAT | — | Where + Add + Mul | Where, Add, Mul | ✅ Yes | ★★★ |

### Critical Insights from Real Projects

1. **Type-preserving patches have the highest success rate.** When both inputs are INT, keep everything in INT. Avoid Cast whenever possible.

2. **ONNX integer division truncates toward zero.** For positive values, `Div(INT, INT)` is equivalent to `floor(a/b)`. This means `Mod` decomposition works correctly without needing a separate Floor operator.

3. **Constants in INT type stay INT.** Don't assume FLOAT just because the model is FP16. TopK outputs INT64 indices, and constant values from the graph may be INT64 even in a float model.

4. **Cast-based patches often fail QNN validation.** The QNN converter rejects mixed-type operations (e.g., Mul with one INT32 input and one FLOAT input). If you must use Cast, add an `Add(0.0)` after the final Cast to break the type inference chain.

5. **Dry-run warnings are not always blockers.** `qnn-onnx-converter --dry_run` may report "unsupported version" for operators like MaxPool, but actual conversion may succeed. Always test actual conversion even with dry-run warnings.

---

> ⚠️ **Manual Patching Process**
>
> Operator patching is a manual process. Follow this workflow:
> 1. Identify the unsupported operator from the error log
> 2. Check its input types using Netron or the Python snippet above
> 3. Follow the Error → Action table below to find the correct replacement pattern
> 4. Apply the ONNX surgery code to your model
> 5. Validate with `onnx.checker.check_model()`, dry-run, and accuracy comparison (see Validation section below)

---

## Error → Action Table

When QNN conversion or context binary generation fails, match the error to the action below.

### How to Use This Table

1. **Identify the error** from converter logs (look for "unsupported", "validation failed", error codes like `0xc26`)
2. **Determine input types** (see "How to Determine Input Types" above)
3. **Match the row** that fits your operator + input type combination
4. **Apply the patch code** to your ONNX model
5. **Validate** with `onnx.checker.check_model()` then re-convert

---

### Mod Operator

| Error | Input A Type | Input B Type | Action (ONNX Surgery Code) | Success Rate |
|-------|-------------|-------------|---------------------------|-------------|
| `0xc26 Op validation failed` | INT64 or INT32 | INT64 or INT32 | Replace with: `Div(a,b) → Mul(b, div_result) → Sub(a, mul_result)` — all stay in INT domain | ★★★★★ |
| `0xc26 Op validation failed` | FLOAT | FLOAT | Replace with: `Div(a,b) → Floor(div_result) → Mul(b, floor_result) → Sub(a, mul_result)` — ⚠️ Floor may also fail | ★★ |
| `0xc26 Op validation failed` | FLOAT | CONST(int value) | Replace with: `Div(a,b) → Cast(INT32) → Cast(FLOAT) → Mul(b, cast_result) → Sub(a, mul_result)` — ⚠️ Add `Add(0.0)` after final Cast to break type chain | ★★ |

**Code Example — INT64 Mod (highest success rate):**
```python
import onnx
from onnx import helper

# Replace Mod node: output = input_a - input_b * (input_a / input_b)
# All operations stay in INT64 domain — no Cast needed
div_node = helper.make_node("Div", [input_a, input_b], ["div_out"], name="mod_div")
mul_node = helper.make_node("Mul", [input_b, "div_out"], ["mul_out"], name="mod_mul")
sub_node = helper.make_node("Sub", [input_a, "mul_out"], [output_name], name="mod_sub")
# Replace in graph: all_nodes[mod_idx:mod_idx+1] = [div_node, mul_node, sub_node]
```

**Why this works:** ONNX INT division truncates toward zero, which equals floor for positive values. No Cast needed, no type mismatch.

---

### Floor Operator

| Error | Input Type | Action | Success Rate |
|-------|-----------|--------|-------------|
| `unsupported version` | INT64 or INT32 | Remove node — Floor of integer is itself. Replace with Identity or rewire input→output directly | ★★★★★ |
| `unsupported version` | FLOAT | Replace with: `Cast(x, INT32) → Cast(FLOAT)` — ⚠️ QNN interprets Cast; may cause downstream type issues | ★★ |

---

### Cast Operator

| Error | Context | Action | Severity |
|-------|---------|--------|----------|
| `Only numerical type cast is supported` | Any Cast node | **This is a WARNING, not an error.** Conversion may still succeed. Verify with actual conversion, not just dry-run. | Warning |
| `Tensor mismatch 0x32 != 0x216` | Cast followed by Mul/Add | Add `Add(0.0)` after the final Cast to break type inference chain: `Cast → Add(0.0) → Mul` | Medium |

---

### Einsum Operator

Einsum is one of the most commonly unsupported operators in QNN/SNPE conversion. It appears frequently in attention mechanisms, contrastive heads, and vision-language models. The good news: **most Einsum equations can be decomposed into supported base operators** (`MatMul`, `Transpose`, `Reshape`, `ReduceSum`).

| Error | Einsum Equation | Action | Success Rate |
|-------|----------------|--------|-------------|
| `unsupported / not implemented` | `bij,bjk->bik` | Replace with `MatMul` | ★★★★★ |
| `unsupported / not implemented` | `bhij,bhjk->bhik` | Replace with batched `MatMul` via Reshape + MatMul + Reshape | ★★★★ |
| `unsupported / not implemented` | `bmchw,bnmc->bmhwn` | Decompose to: Permute + Reshape + MatMul + Reshape (see pattern A below) | ★★★★ |
| `unsupported / not implemented` | `bchw,bkc->bkhw` | Decompose to: Permute + Reshape + MatMul + Permute + Reshape (see pattern B below) | ★★★★ |
| `unsupported / not implemented` | Other | Decompose to: Transpose + Reshape + MatMul + ReduceSum based on equation | ★★★ |

---

#### Pattern A: 5D Einsum → MatMul (Attention-style)

**Equation:** `bmchw,bnmc->bmhwn`

**Example:** MaxSigmoidAttnBlock in YOLO-Worldv2 (`/model.12/attn/Einsum`)

**Meaning:**
- Input A: `[b, m, c, h, w]` — image feature embedding
- Input B: `[b, n, m, c]` — guide/text embedding
- Output: `[b, m, h, w, n]` — attention weights

**Replacement:**
```python
# Original: aw = torch.einsum("bmchw,bnmc->bmhwn", embed, guide)
#
# Step-by-step decomposition:
# 1. embed: [b, m, c, h, w] -> permute(0,1,3,4,2) -> [b, m, h, w, c] -> reshape -> [b*m, h*w, c]
# 2. guide: [b, n, m, c]    -> permute(0,2,3,1)    -> [b, m, c, n]   -> reshape -> [b*m, c, n]
# 3. matmul: [b*m, h*w, c] @ [b*m, c, n] -> [b*m, h*w, n]
# 4. reshape: [b*m, h*w, n] -> [b, m, h, w, n]

bs, _, h, w = x.shape
embed_r = embed.permute(0, 1, 3, 4, 2).reshape(bs * self.nh, h * w, self.hc)
guide_r = guide.permute(0, 2, 3, 1).reshape(bs * self.nh, self.hc, guide.shape[1])
aw = torch.matmul(embed_r, guide_r).view(bs, self.nh, h, w, guide.shape[1])
```

**Operators used:** `Permute`, `Reshape`, `MatMul` — all QNN-compatible ✅

**Why it works:** Einsum is essentially batched matrix multiplication with dimension rearrangement. By explicitly permuting and reshaping, we expose the underlying MatMul structure that QNN can optimize.

---

#### Pattern B: 4D Einsum → MatMul (Contrastive Head-style)

**Equation:** `bchw,bkc->bkhw`

**Example:** BNContrastiveHead in YOLO-Worldv2 (`/model.22/cv4.0/Einsum`)

**Meaning:**
- Input A: `[b, c, h, w]` — image features (spatial)
- Input B: `[b, k, c]` — text/class embeddings
- Output: `[b, k, h, w]` — per-pixel class scores

**Replacement:**
```python
# Original: x = torch.einsum("bchw,bkc->bkhw", x, w)
#
# Step-by-step decomposition:
# 1. x:     [b, c, h, w] -> permute(0,2,3,1) -> [b, h, w, c] -> reshape -> [b, h*w, c]
# 2. w:     [b, k, c]    -> transpose(1,2)   -> [b, c, k]
# 3. matmul: [b, h*w, c] @ [b, c, k] -> [b, h*w, k]
# 4. reshape: [b, h*w, k] -> permute(0,2,1) -> [b, k, h*w] -> reshape -> [b, k, h, w]

bs, c, h, w_dim = x.shape
x_r = x.permute(0, 2, 3, 1).reshape(bs, h * w_dim, c)
w_r = w.transpose(1, 2)  # [b, c, k]
out = torch.matmul(x_r, w_r)  # [b, h*w, k]
out = out.permute(0, 2, 1).reshape(bs, w.shape[1], h, w_dim)
```

**Operators used:** `Permute`, `Reshape`, `Transpose`, `MatMul` — all QNN-compatible ✅

**Why it works:** The Einsum computes a dot product between each spatial location and each class embedding. This is exactly a batched matrix multiplication once we flatten the spatial dimensions.

---

#### Pattern C: Simple Batched MatMul

**Equation:** `bij,bjk->bik`

**Replacement:**
```python
# Original: out = torch.einsum("bij,bjk->bik", A, B)
# Direct replacement:
out = torch.matmul(A, B)
```

---

#### Pattern D: Multi-Batch Dimensions

**Equation:** `bhij,bhjk->bhik`

**Replacement:**
```python
# Original: out = torch.einsum("bhij,bhjk->bhik", A, B)
# Decomposition:
# A: [b, h, i, j], B: [b, h, j, k]
# Merge batch dims: [b*h, i, j] @ [b*h, j, k] -> [b*h, i, k] -> reshape -> [b, h, i, k]
b, h, i, j = A.shape
_, _, _, k = B.shape
A_r = A.reshape(b * h, i, j)
B_r = B.reshape(b * h, j, k)
out = torch.matmul(A_r, B_r).reshape(b, h, i, k)
```

---

#### General Einsum Decomposition Algorithm

For arbitrary Einsum equations, follow this systematic approach:

1. **Identify shared indices** (appear in both inputs but not output) → these are **reduced** dimensions
2. **Identify batch indices** (appear in both inputs AND output) → these stay as batch dims
3. **Identify output-only indices** → these come from one input each
4. **Reshape** to merge batch dims into one, merge reduced dims into one
5. **MatMul** on the merged tensors
6. **Reshape** output back to original batch structure

**Example walkthrough:** `abcde,afgc->abgde`
- Shared (reduced): `c` (appears in both, not in output)
- Batch: `a` (in both and output)
- From input 1: `b`, `d`, `e` (only in first input and output)
- From input 2: `f`, `g` (`f` reduced, `g` in output)
- Decomposition:
  ```python
  # A: [a, b, c, d, e] -> reshape to [a, b*d*e, c]
  # B: [a, f, g, c]    -> reshape to [a, c, f*g]
  # MatMul: [a, b*d*e, c] @ [a, c, f*g] -> [a, b*d*e, f*g]
  # Reshape: [a, b, d, e, f, g] -> permute/reshape -> [a, b, g, d, e]
  ```

---

#### Einsum Patch Template (In-Memory PyTorch Patch)

```python
import torch
import torch.nn as nn

def patch_einsum_to_matmul_forward(self, x: torch.Tensor, guide: torch.Tensor) -> torch.Tensor:
    """
    Replace Einsum with MatMul for QNN compatibility.
    Adapt dimensions to match your specific equation.
    
    For equation "bmchw,bnmc->bmhwn":
    - x: [b, m, c, h, w]
    - guide: [b, n, m, c]
    - output: [b, m, h, w, n]
    """
    bs = x.shape[0]
    nh = self.nh  # number of heads (m dimension)
    hc = self.hc  # head channels (c dimension)
    
    h, w = x.shape[-2:]
    
    # Reshape for matmul
    embed_r = x.permute(0, 1, 3, 4, 2).reshape(bs * nh, h * w, hc)
    guide_r = guide.permute(0, 2, 3, 1).reshape(bs * nh, hc, guide.shape[1])
    
    # MatMul
    out = torch.matmul(embed_r, guide_r)
    
    # Reshape back
    return out.view(bs, nh, h, w, guide.shape[1])


# Apply patch to all instances of the target module
def patch_model_einsum(model, target_module_class):
    """Replace forward method for all instances of target_module_class."""
    for name, module in model.named_modules():
        if isinstance(module, target_module_class):
            print(f"[PATCH] Replacing Einsum in: {name}")
            module.forward = patch_einsum_to_matmul_forward.__get__(module, target_module_class)
    return model
```

---

#### Einsum Validation Checklist

After patching Einsum, verify:

| Check | Command | Pass Criteria |
|-------|---------|---------------|
| ONNX checker | `onnx.checker.check_model()` | No exceptions |
| Dry-run | `qnn-onnx-converter --dry_run` | No "Einsum" in unsupported list |
| Numerical parity | Compare with original on test input | Cosine ≥ 0.99 |
| Output shape | `original.shape == patched.shape` | Exact match |
| Class scores | Check all class channels (not just top-1) | All within tolerance |

> ⚠️ **Critical:** Einsum patches can silently change numerical behavior if dimensions are misaligned. Always validate **all output channels**, not just the top-1 detection. In YOLO-World, a patched Einsum that passes for `person` class may still fail for `bus` class if the contrastive head precision is affected.

---

### ScatterND Operator

| Error | Index Pattern | Action | Success Rate |
|-------|--------------|--------|-------------|
| `unsupported` | Non-overlapping indices | Replace with: `Gather` old values → `Where` mask → `Add` updates | ★★★★ |
| `unsupported` | Overlapping indices | Requires loop or custom op — escalate B7 | ★ |

---

### GridSample Operator

| Error | Mode | Action | Success Rate |
|-------|------|--------|-------------|
| `unsupported` | Bilinear | Replace with: AffineGrid generator + Resize(bilinear) | ★★ |
| `unsupported` | Nearest/Bicubic | Complex decomposition — consider model architecture change | ★ |

---

### MaxPool Operator

| Error | Context | Action | Success Rate |
|-------|---------|--------|-------------|
| `MaxPool: unsupported version` / `dilations: unsupported in Converter` | Any MaxPool2d (even with dilation=1) | **This is a WARNING, not a blocking error.** Conversion succeeds (exit code 0). Do NOT patch. | ★★★★★ |
| `MaxPool: unsupported version` | dilation > 1 | May fail actual conversion. Test with actual conversion, not just dry-run. If it fails, replace with Slice+Stack+Max pattern. | ★★★ |

**Critical Insight:** PyTorch ONNX export always adds `dilations=[1,1]` and `ceil_mode=0` attributes to MaxPool nodes, even when using default values. The QNN converter flags `dilations: unsupported in Converter` as a **warning in the dry-run table**, but **actual conversion still succeeds**.

**Verification test results:**

| Test | Exit Code | Result |
|------|-----------|--------|
| Dry-run (`--dry_run`) | 0 | MaxPool listed in warning table |
| Actual FP16 conversion | 0 | `Conversion complete!` — `.cpp` + `.bin` generated |
| Context binary generation | 0 | `.dll.bin` generated successfully |
| Inference on HTP | 0 | Correct outputs (once HTP driver is stable) |

**What this means:** If you see `MaxPool: unsupported version` in dry-run output, **proceed with conversion** — it will succeed. Do not waste time patching MaxPool2d unless actual conversion fails (which is rare).

**When TO patch MaxPool2d:**
- Only if actual conversion (not dry-run) fails with a MaxPool-related error
- Replace with `Slice + Stack + ReduceMax` pattern (see below)

**MaxPool2d replacement pattern (last resort):**
```python
# Only use if actual conversion fails, not for dry-run warnings
class QNNMaxPool(nn.Module):
    def __init__(self, kernel_size, stride=1, padding=0):
        super().__init__()
        self.k, self.s, self.p = kernel_size, stride, padding
    
    def forward(self, x):
        if self.p > 0:
            x = F.pad(x, [self.p]*4, value=float('-inf'))
        # 25 slices (5x5 kernel) + stack + max
        slices = []
        for di in range(self.k):
            for dj in range(self.k):
                s = x[:, :, di:di+x.shape[2]-self.k+1, dj:dj+x.shape[3]-self.k+1]
                slices.append(s)
        return torch.stack(slices, dim=2).max(dim=2)[0]
```
> ⚠️ **Warning:** This replacement increases model size significantly (e.g., +3.5 MB for YOLO-World SPPF) and may introduce FP16 precision loss in low-confidence class channels. Only use if absolutely necessary.

---

### Generic Unsupported Operator

| Error | Known Replacement? | Action |
|-------|-------------------|--------|
| Any operator not in table above | Yes — search references | Apply documented replacement pattern |
| Any operator not in table above | No known pattern exists | **Escalate as Blocking Condition B7** — document operator name, input types, and attempted approaches |

---

## Escalation Policy

Stop patching and escalate when ANY condition is met:

| Condition | Code | Evidence Required | Action |
|-----------|------|-------------------|--------|
| No replacement pattern exists | **B7** | Operator name, input types, literature search results | Document and escalate to user |
| Patch changes model semantics | **B4** | Description of semantic change, accuracy impact | Describe change, await user approval |
| 7+ iterations with same ops failing | **B3** | List of attempted patches, dry-run logs, ONNX snapshot | Escalate, consider alternative flow |

**Progress Assessment (at iteration 5+):**
- Resolving ops faster than discovering new ones? → Continue
- New ops appearing faster than resolved? → Escalate early

---

## ⚠️ CRITICAL: Never Use CPU Runtime as Workaround

**When context binary generation fails due to unsupported operators:**

| ❌ Not Allowed | ✅ Required |
|----------------|-------------|
| CPU fallback for unsupported operators | Patch operators for HTP/DSP compatibility |
| `QnnCpu.dll` context binary as solution | HTP-compatible operator decomposition |
| Skip patching and run on CPU only | Model must run on target accelerator (HTP/DSP) |

**Rationale:**
- Target platform is Qualcomm AI PC with HTP accelerator
- CPU-only inference defeats the purpose of QNN/SNPE conversion
- Context binary generation MUST succeed with HTP backend, not CPU
- **Blocking Condition B7**: If unable to patch for HTP, escalate to user (do not silently fall back to CPU)

---

## When to Patch

Patch your model when you encounter operator-related failures at **any stage** of the pipeline:

| Stage | Symptom | Action |
|-------|---------|--------|
| **ONNX Export** | Export fails or produces invalid graph | Patch before `torch.onnx.export()` |
| **Converter Dry-Run** | `qnn-onnx-converter --dry_run` flags unsupported op | Patch before conversion |
| **FP Conversion** | Conversion fails with "Unsupported operator" error | Patch ONNX, re-export, re-convert |
| **Context Binary** | HTP compilation fails (e.g., `QnnHtp.dll` error) | Patch ONNX, regenerate context binary |
| **Inference** | Runtime crash or incorrect output on target device | Patch ONNX, rebuild all artifacts |

**Common operators requiring patches:**

| Operator | Issue |
|----------|-------|
| `Einsum` | Not supported by QNN |
| `GridSample` | Limited support |
| `ScatterND` | Conversion failures |
| `Mod` | HTP unsupported |
| `Floor` | HTP unsupported |
| `Transpose` | HTP unsupported |
| `Ceil` | HTP unsupported |
| Custom attention | Varies by implementation |

**Note:** For operator replacement patterns, consult the QNN/SNPE documentation and search for equivalent implementations using supported base operators (`MatMul`, `Reshape`, `Transpose`, `Concat`, etc.). Each patch must be validated for numerical correctness.

---

## Integration with Agent Workflow

| Agent Phase | Patching Action |
|-------------|-----------------|
| **Model Export Agent** (Phase 1) | Apply in-memory patches before `torch.onnx.export()` |
| **Model Inspector Agent** (Phase 2) | Verify patched model via dry-run; if issues remain → loop back to Export Agent |

**Mode Behavior:**

| Mode | Patching Behavior |
|------|-------------------|
| `batch` | Apply patches autonomously; log all decisions in `aipc_plan.md` Issue Log |
| `interactive` | Ask for confirmation before applying patches, especially if semantics may change |

**Blocking Condition B4:** If a patch would change model semantics (e.g., replace attention with different behavior), **stop and ask user** for approval — regardless of mode.

**Tracking:** Record `PATCH_NEEDED` and `PATCH_OPS` in `aipc_plan.md` Prerequisites section.

---

## Approach Selection Decision Tree

**Use this decision tree to select the correct patching approach:**

```
Step 1: Can you modify the PyTorch export code?
├─ YES → Use Approach 1 (Custom Symbolic Handlers)
│   ├─ Best for: torch.mod, torch.einsum, custom aten ops
│   ├─ Register before: torch.onnx.export()
│   └─ Success rate: Highest (clean graph structure)
│
└─ NO → Go to Step 2

Step 2: Is the unsupported op a known PyTorch module?
├─ YES → Use Approach 2 (Module Replacement)
│   ├─ Best for: Replaceable nn.Module instances
│   ├─ Patch: module.forward() in-memory
│   └─ Success rate: High (direct control)
│
└─ NO → Use Approach 3 (ONNX Surgery)
    ├─ Last resort: Direct ONNX graph modification
    ├─ Risk: Topological sort issues, numerical drift
    └─ Success rate: Variable (depends on graph complexity)
```

**Key Principle**: Always prefer **Approach 1** when possible — it's cleaner and more reliable than post-export patching.

### Approach Comparison

| Criteria | Approach 1: Custom Symbolic | Approach 2: Module Replace | Approach 3: ONNX Surgery |
|----------|---------------------------|---------------------------|-------------------------|
| **When to use** | PyTorch source accessible | PyTorch module can be swapped | PyTorch source NOT accessible |
| **Implementation** | Register before export | Replace module.forward() | Modify ONNX graph directly |
| **Graph structure** | Clean (built-in during export) | Clean (built-in during export) | Complex (post-export modification) |
| **Numerical stability** | High | High | Variable |
| **Difficulty** | Low | Medium | High |
| **Success rate** | ✅ Highest | ✅ High | ⚠️ Variable |

---

## Patching Principles

### ✅ DO

- **Patch in-memory only** — modify the model instance, not the library
- **Validate after patching** — compare outputs before and after (see [Validation](#validation))
- **Use supported operators** — build replacements from `MatMul`, `Reshape`, `Transpose`, `Concat`, etc.
- **Preserve mathematical equivalence** — ensure the patched logic matches the original operator
- **Log all patches** — record which layers were patched and why
- **Inspect first, patch second** — run dry-run to identify exact unsupported ops before writing any patch code
- **Patch only what's needed** — target specific unsupported operators; do not rewrite the entire model

### ❌ DON'T

- **Never modify library source code** — this breaks reproducibility and causes version conflicts
- **Don't patch without validation** — always verify numerical parity
- **Avoid complex patches** — if a replacement is too complex, consider model architecture changes
- **Don't ignore small errors** — even minor numerical differences can compound
- **Don't read all source code** — you don't need to understand the entire model; just identify and patch the unsupported ops
- **Don't over-patch** — if dry-run passes, stop patching; unnecessary patches introduce numerical risk

---

## Patching Template

> **Hint: Identify Before Patching (PyTorch — Approach 1)**
>
> 1. **Run dry-run first** to get the exact list of unsupported operators
> 2. **Inspect the PyTorch model** using `named_modules()` to find which layers use the unsupported op
> 3. **Patch only those layers** — no need to read or rewrite the entire source code
> 4. **Re-run dry-run** — if it passes, stop; don't add unnecessary patches
>
> ```python
> # Quick inspection: find all module types in the PyTorch model
> for name, module in model.named_modules():
>     print(f"{name}: {type(module).__name__}")
> ```
>
> This approach avoids over-engineering and reduces numerical risk.

> **Hint: Identify in ONNX Graph (ONNX Surgery — Approach 2)**
>
> If you don't have PyTorch source and must patch the ONNX directly:
>
> ```python
> import onnx
>
> model = onnx.load("model.onnx")
>
> # List all operator types in the ONNX graph
> op_types = set(node.op_type for node in model.graph.node)
> print("Operators in model:", sorted(op_types))
>
> # Find nodes using a specific unsupported op
> for node in model.graph.node:
>     if node.op_type == "Einsum":  # replace with your unsupported op
>         print(f"Found {node.op_type} at node: {node.name}")
> ```
>
> This helps you locate exactly which nodes need modification without guessing.

The following generic template shows how to patch a model in-memory. For specific operator replacement patterns, you must derive them based on the mathematical definition of the operator and available supported operators.

```python
import torch
import types

def patch_model_for_qnn(model):
    """
    Replace unsupported operators with QNN-compatible equivalents.
    This modifies the model instance in-memory only — the installed 
    Python package remains unchanged.
    """
    
    def patched_forward(self, x):
        # Implementation using supported operators:
        # MatMul, Reshape, Transpose, Concat, etc.
        # Ensure mathematical equivalence to original op
        return ...
    
    # Replace forward method for specific layer instances
    for name, module in model.named_modules():
        if isinstance(module, TargetLayerClass):
            print(f"[PATCH] Replacing forward in: {name}")
            module.forward = types.MethodType(patched_forward, module)
    
    return model

# Usage
model = load_original_model()
patched_model = patch_model_for_qnn(model)

torch.onnx.export(
    patched_model, 
    dummy_input, 
    "model.onnx",
    opset_version=13,  # Use 13-17 for QNN compatibility
    input_names=["input"],
    output_names=["output"]
)
```

---

## Validation

**Validation is mandatory after patching.** AI-generated or manual patches can introduce:
- Off-by-one errors
- Axis misalignments  
- Numerical instability

### 1. Numerical Validation

```python
import numpy as np
import onnxruntime as ort

# Run both models on identical preprocessed input
original_output = original_model(input_data)

onnx_session = ort.InferenceSession("model.onnx")
onnx_output = onnx_session.run(None, {"input": input_data})

# Compare outputs
mse = np.mean((original_output - onnx_output) ** 2)
cosine_sim = np.dot(original_output.flatten(), onnx_output.flatten()) / (
    np.linalg.norm(original_output.flatten()) * np.linalg.norm(onnx_output.flatten())
)

print(f"MSE: {mse:.6f}")
print(f"Cosine Similarity: {cosine_sim:.4f}")
```

**Acceptable thresholds:**

| Metric | FP16/FP32 | INT8/A16W8 |
|--------|-----------|------------|
| Cosine Similarity | ≥ 0.99 | ≥ 0.95 |
| MSE | < 1e-4 (task-dependent) | task-dependent |

> ⚠️ **Confirm with user** if numerical error is acceptable for their use case.

### 2. Task-Specific Validation (Recommended)

For computer vision tasks (e.g., object detection):

- **Visual Check**: Generate annotated images from both models and compare
- **Result Check**: Compare high-level outputs:
  - Bounding box coordinates
  - Class labels
  - Confidence scores

**If detection results are identical or very similar**, the model is likely safe for conversion even with minor numerical MSE.

---

## Post-Patch Validation — Mandatory Gates

**After EACH patch iteration, run ALL validation gates before proceeding:**

### Gate 1: ONNX Structural Validity

```bash
python -c "import onnx; onnx.checker.check_model('model_patched.onnx')"
```

**Pass criteria:**
- ✓ No exceptions raised
- ✓ Graph is well-formed
- ✓ All tensor types consistent

**Fail action:** Fix ONNX structure, re-validate

---

### Gate 2: Converter Compatibility

```bash
# QNN Flow
{QAIRT_ROOT}/bin/{HOST_ARCH}/qnn-onnx-converter --input_network model_patched.onnx --dry_run

# SNPE Flow  
{QAIRT_ROOT}/bin/{HOST_ARCH}/qairt-converter --input_network model_patched.onnx --dry_run
```

**Pass criteria:**
- ✓ "Model ops, op attributes, inputs and outputs have been evaluated"
- ✓ No "unsupported operator" errors

**Fail action:** Identify new unsupported ops, return to patching

---

### Gate 3: Numerical Sanity (if baseline available)

```python
import numpy as np
import onnxruntime as ort

# Run both models on same input
original_output = original_model(input_data)
onnx_session = ort.InferenceSession("model_patched.onnx")
onnx_output = onnx_session.run(None, {"input": input_data})

# Compare
cosine_sim = np.dot(original_output.flatten(), onnx_output.flatten()) / (
    np.linalg.norm(original_output.flatten()) * np.linalg.norm(onnx_output.flatten())
)
print(f"Cosine similarity: {cosine_sim:.4f}")
```

**Pass criteria:**
- ✓ Output shapes match
- ✓ Cosine similarity ≥ 0.95 (initial patch)
- ✓ No NaN/Inf introduced

**Fail action:** Review patch for numerical stability issues

---

### Gate 4: Full Conversion (final iteration only)

```bash
python aipc_convert_fp.py --onnx model_patched.onnx ...
```

**Pass criteria:**
- ✓ "Conversion complete!"
- ✓ .bin, .cpp, .json generated

**Fail action:** Review converter error logs, identify root cause

---

### Decision Matrix

| Gate 1 | Gate 2 | Gate 3 | Action |
|--------|--------|--------|--------|
| ✅ Pass | ✅ Pass | ✅ Pass | Proceed to next iteration |
| ❌ Fail | — | — | Fix ONNX structure |
| ✅ Pass | ❌ Fail | — | More patching needed |
| ✅ Pass | ✅ Pass | ❌ Fail | Review patch numerical stability |

---

## Post-Patch Verification (Final)

**After ALL patches complete, before conversion:**

1. **Run converter dry-run** to confirm all unsupported operators are resolved:
   ```bash
   # QNN Flow
   {QAIRT_ROOT}/bin/{HOST_ARCH}/qnn-onnx-converter --input_network model.onnx --dry_run

   # SNPE Flow
   {QAIRT_ROOT}/bin/{HOST_ARCH}/qairt-converter --input_network model.onnx --dry_run
   ```

2. **Confirm no unsupported ops flagged** — if any remain, apply additional patches and re-run dry-run.

3. **Hand off to Model Inspector Agent** — proceed to Phase 2 only after dry-run passes.

---

## Troubleshooting

| Issue | Stage | Possible Cause | Solution |
|-------|-------|---------------|----------|
| Patch doesn't apply | Export | Wrong layer type | Use `print(type(module))` to debug |
| Output mismatch | Validation | Incorrect replacement logic | Verify mathematical equivalence |
| ONNX export fails | Export | Patch breaks graph | Check tensor shapes and dtypes |
| Conversion fails | Conversion | Unsupported op remains | Run dry-run, identify remaining ops |
| Context binary fails | Context Bin | HTP incompatibility | Patch ONNX, rebuild all artifacts |
| Inference crashes | Runtime | Op not supported on target | Verify patch, rebuild, retest |
| Output differs on device | Runtime | Precision or axis issue | Check preprocessing, validate on target |
| Dry-run still flags ops | Conversion | Patch incomplete | Re-inspect ONNX, identify remaining unsupported ops |
| Patch not logged | Batch mode | Forgot to record decision | Log in `aipc_plan.md` Issue Log before proceeding |

---

## References

- Agent Workflow: [`../assets/aipc_AGENTS.md`](../assets/aipc_AGENTS.md)
- Project Plan: [`../assets/aipc_plan.md`](../assets/aipc_plan.md)
- Model Export Guide: [`model_export_validation.md`](model_export_validation.md)
- QNN Conversion: [`qnn_conversion.md`](qnn_conversion.md)
- SNPE Conversion: [`snpe_conversion.md`](snpe_conversion.md)
- Troubleshooting: [`troubleshooting.md`](troubleshooting.md)

---

## Operator Issues by Pipeline Stage

> **Note:** This section is for reference only. The core patching workflow is described above.

Operator compatibility problems can surface at different points. Here's how to identify and resolve them:

### Stage 1: ONNX Export

**Symptoms:**
- `torch.onnx.export()` raises `OperatorExportTypes.ONNX` error
- Exported ONNX model has `Undefined` or `Custom` operators

**Detection:**
```bash
python skills/aipc-toolkit/scripts/aipc_inspect_onnxio.py model.onnx
```

**Resolution:**
- Apply in-memory patch **before** export
- Re-export with `opset_version=13` or higher

---

### Stage 2: Converter Dry-Run

**Symptoms:**
- `qnn-onnx-converter --dry_run` reports unsupported operators
- Conversion log shows: `Error: Operator 'Einsum' is not supported`

**Detection:**
```bash
# QNN Flow
{QAIRT_ROOT}/bin/{HOST_ARCH}/qnn-onnx-converter --input_network model.onnx --dry_run

# SNPE Flow
{QAIRT_ROOT}/bin/{HOST_ARCH}/qairt-converter --input_network model.onnx --dry_run
```

**Resolution:**
- Patch ONNX model
- Re-run dry-run to confirm all ops are supported

---

### Stage 3: FP/INT Conversion

**Symptoms:**
- `aipc_convert_fp.py` or `aipc_convert_int.py` fails mid-conversion
- Error: `Conversion failed: Unsupported operator 'GridSample'`

**Detection:**
- Check conversion logs for failing operator name

**Resolution:**
1. Apply patch to source model
2. Re-export ONNX
3. Re-run conversion script
4. **Do not reuse old `.bin`/`.cpp`/`.so` artifacts** — regenerate all

---

### Stage 4: Context Binary Generation (QNN Only)

**Symptoms:**
- `aipc_dev_gen_contextbin.py` fails during HTP compilation
- Error mentions specific layer or op (e.g., `Failed to compile layer 'Einsum_123'`)

**Detection:**
```bash
python skills/aipc-toolkit/scripts/aipc_dev_gen_contextbin.py \
  --model_lib libmodel.so \
  --output libmodel.so.bin
```

**Resolution:**
1. Patch ONNX model (root cause is usually an unsupported op in the graph)
2. Re-export ONNX
3. Re-convert to QNN (regenerate `.bin`/`.cpp`/`.so`)
4. Re-generate context binary

> ⚠️ **Important**: Context binary failures often trace back to ONNX-level operator issues. Always patch at the ONNX level, not the context binary level.

---

### Stage 5: Inference Runtime

**Symptoms:**
- Inference crashes on target device (HTP/DSP/CPU/GPU)
- Output is incorrect or NaN
- Error: `QnnHtp: Failed to execute graph`

**Detection:**
- Run inference with verbose logging
- Compare ONNX CPU output vs. QNN/SNPE output

**Resolution:**
1. Verify ONNX model has no unsupported ops
2. Re-validate patch correctness (see [Validation](#validation))
3. Rebuild all artifacts from patched ONNX
4. Test on target device again

---

## Patch Pattern Catalog

**Organized by pattern type (not operator):**

### Type A: Type Conversion Patterns

**Use when:** Operator fails due to tensor type constraints

#### Pattern A1: Cast Chain
```
Structure: Cast(T1) → Cast(T2) → ... → Cast(Tn)
Use case: Type-sensitive operators (Floor, Round, etc.)
I/O preservation: Input/output types must match original
Validation: Verify no precision loss for expected value range
```

#### Pattern A2: Type-Preserving Decomposition
```
Structure: Op(T) → [SubOp1(T), SubOp2(T), ...] → Combine(T)
Use case: Complex op that can be decomposed
I/O preservation: All intermediate types match input type
Validation: Numerical equivalence at each stage
```

---

### Type B: Mathematical Decomposition Patterns

**Use when:** Operator can be expressed as composition of simpler ops

#### Pattern B1: Primitive Decomposition
```
Structure: ComplexOp → [PrimitiveOp1, PrimitiveOp2, ...]
Use case: High-level ops not supported by backend
I/O preservation: Exact mathematical equivalence required
Validation: Symbolic verification + numerical testing
```

#### Pattern B2: Numerically Stable Alternative
```
Structure: UnstableOp → StableEquivalent
Use case: Operator causes numerical issues in target backend
I/O preservation: Equivalent within numerical tolerance
Validation: Condition number analysis, edge case testing
```

---

### Type C: Structural Modification Patterns

**Use when:** Operator structure conflicts with backend requirements

#### Pattern C1: Attribute Modification
```
Structure: Op(attr=X) → Op(attr=Y) where Y is backend-compatible
Use case: Attribute values not supported (e.g., dilation > 1)
I/O preservation: Output may differ slightly; document tolerance
Validation: Compare output with original for typical inputs
```

#### Pattern C2: Graph Restructuring
```
Structure: SubGraph1 → EquivalentSubGraph2
Use case: Graph pattern not supported by backend
I/O preservation: I/O tensors must match exactly
Validation: End-to-end numerical comparison
```

---

### Type D: Backend-Specific Workarounds

**Use when:** Backend has known limitations

#### Pattern D1: CPU Fallback
```
Structure: UnsupportedOp → CPU execution
Use case: Op not available on target backend (HTP/DSP)
I/O preservation: Exact equivalence
Validation: Performance impact assessment
```

#### Pattern D2: Precision Relaxation
```
Structure: HighPrecisionOp → LowerPrecisionEquivalent
Use case: Backend supports limited precision
I/O preservation: Within precision tolerance
Validation: Accuracy impact on validation set
```

---

### Pattern Documentation Template

For each new pattern discovered, document:

```markdown
#### Pattern {X}{n}: {Name}
- **Applicability:** When to use
- **Structure:** Diagram or pseudocode
- **I/O Preservation:** Requirements
- **Known Limitations:** Edge cases, tolerances
- **Validation Checklist:** Tests to run
- **Example Implementation:** Code snippet

---

## Post-Patch Validation

After applying any operator patch, validate correctness before proceeding.

### Validation Gates (run in order)

| Gate | Check | Command | Pass Criteria |
|------|-------|---------|---------------|
| 1 | ONNX Validity | `onnx.checker.check_model(patched.onnx)` | No exceptions |
| 2 | Shape Match | Compare output shapes of original vs patched | Shapes identical |
| 3 | Numerical Match | Run both models with same input | Cosine ≥ 0.99 (FP) / ≥ 0.95 (INT8) |
| 4 | QNN Dry-Run | `qnn-onnx-converter --dry_run --input_network patched.onnx` | No unsupported ops |
| 5 | QNN Conversion | `aipc_convert_fp.py --onnx patched.onnx ...` | Succeeds without errors |

### Gate 3: Numerical Comparison — How to Do It

Run both models (original and patched) with the **same input data** and compare outputs:

```python
import numpy as np
import onnxruntime as ort

# Load both models
orig = ort.InferenceSession("original.onnx")
patched = ort.InferenceSession("patched.onnx")

# Same input for both
input_name = orig.get_inputs()[0].name
input_data = np.random.randn(*orig.get_inputs()[0].shape).astype(np.float32)
# Or use real data: input_data = preprocess_your_image(...)

# Run inference
out_orig = orig.run(None, {input_name: input_data})
out_patch = patched.run(None, {input_name: input_data})

# Compare each output
for i, (o, p) in enumerate(zip(out_orig, out_patch)):
    # Cosine similarity
    cos = np.dot(o.flatten(), p.flatten()) / (
        np.linalg.norm(o.flatten()) * np.linalg.norm(p.flatten())
    )
    # Max absolute difference
    max_diff = np.abs(o - p).max()
    # Mean absolute difference
    mean_diff = np.abs(o - p).mean()

    print(f"Output {i}:")
    print(f"  Shape: orig={o.shape} patched={p.shape}")
    print(f"  Cosine similarity: {cos:.6f}")
    print(f"  Max abs diff:      {max_diff:.6e}")
    print(f"  Mean abs diff:     {mean_diff:.6e}")
    print(f"  PASS" if cos >= 0.99 else f"  FAIL (threshold: 0.99)")
```

**Interpretation:**
- Cosine ≥ 0.999: Bit-identical or near-identical — patch is correct
- Cosine 0.99–0.999: Minor numerical drift — acceptable for most use cases
- Cosine 0.95–0.99: Noticeable drift — investigate, may be acceptable for INT8
- Cosine < 0.95: Significant error — patch is incorrect, try different pattern

### What to Do If Validation Fails

| Failure | Likely Cause | Action |
|---------|-------------|--------|
| Gate 1 (ONNX checker) | Invalid graph structure | Check topological order, tensor names |
| Gate 2 (Shape mismatch) | Wrong output tensor routing | Verify output_name matches original |
| Gate 3 (Low cosine) | Wrong pattern or type mismatch | Try next pattern in Error → Action table |
| Gate 4 (Dry-run fails) | Replacement ops also unsupported | Check if Floor/Cast introduced new issues |
| Gate 5 (Conversion fails) | Type inference error | Add `Add(0.0)` after Cast to break type chain |
