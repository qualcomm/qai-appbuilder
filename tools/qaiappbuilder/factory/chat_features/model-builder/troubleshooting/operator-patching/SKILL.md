---
skill_id: operator-patching
tier: base
triggers: ["unsupported operator", "0xc26 Op validation failed", "Einsum", "Mod", "Floor", "ScatterND", "GridSample", "dry-run flags op"]
sources: ["references/operator_patching.md", "SKILL.md Step 3"]
---

# Operator Patching (base)

> 🧭 Diagnosis framework: `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/_diagnosis-framework.md`; this SKILL covers unsupported operator replacement.

## Responsibility

Replace unsupported operators (`Einsum`, `GridSample`, `ScatterND`, `Mod`, `Floor`, `Ceil`, `Round`)
with QNN-compatible equivalents from supported base ops (`MatMul`, `Reshape`, `Transpose`,
`Concat`, `Div`, `Mul`, `Sub`, `Where`, `Add`), **in-memory only**. Patch at whichever stage fails.

## Trigger signals

- Converter/context-binary error: operator name + `unsupported` / `not implemented`
- `0xc26 Op validation failed` on a specific node
- `--dry_run` flags op as unsupported
- `Failed to compile layer 'Einsum_123'`

## Core knowledge

### RULE 1 — check input TYPES first

Same op needs different patch for INT vs FLOAT. Determine types from **producer node**:

| Producer | Output type |
|----------|-------------|
| TopK (indices) | INT64 |
| Constant data_type=7/=1 | INT64 / FLOAT32 |
| Conv / MatMul / Gemm | FLOAT32 |
| Softmax / Sigmoid / Relu | FLOAT32 |
| Reshape / Transpose | inherits |

```python
import onnx
m = onnx.load("model.onnx")
for n in m.graph.node:
    if n.op_type == "Mod":
        print(n.name, list(n.input), list(n.output))
```

### Approach decision tree

```
Can you modify the PyTorch export code?
├─ YES → Approach 1: Custom Symbolic Handlers (register before torch.onnx.export)
│        Best for torch.mod / torch.einsum / custom aten ops. Highest success (clean graph).
└─ NO  → Is the op a known nn.Module?
         ├─ YES → Approach 2: Module Replacement (patch module.forward in-memory). High success.
         └─ NO  → Approach 3: ONNX Surgery (direct graph edit). Last resort; topo-sort / drift risk.
```

### Error → Action table (type-aware)

| Op | Types | Action | ★ |
|----|-------|--------|---|
| **Mod** | INT/INT | `Sub(a, Mul(b, Div(a,b)))` | 5 |
| **Mod** | FLOAT/FLOAT | `Sub(a, Mul(b, Floor(Div(a,b))))` ⚠️ Floor may fail | 2 |
| **Mod** | FLOAT/CONST(int) | `Div→Cast(INT)→Cast(FLOAT)→Mul→Sub`; `Add(0.0)` after Cast | 2 |
| **Floor** | INT | Remove (identity) | 5 |
| **Floor** | FLOAT | `Cast(INT32)→Cast(FLOAT)` ⚠️ type issues | 2 |
| **Ceil** | FLOAT | `Neg(Floor(Neg(x)))` | 2 |
| **Round** | FLOAT | `Floor(Add(x,0.5))` | 2 |
| **Cast** | `Only numerical type cast supported` | WARNING — verify with actual conversion | — |
| **Cast** | `Tensor mismatch 0x32 != 0x216` | `Cast→Add(0.0)→Mul` | — |
| **Einsum** | FLOAT | Decompose to MatMul+Transpose+Reshape | 4 |
| **ScatterND** | non-overlapping | `Gather→Where(mask)→Add(updates)` | 4 |
| **ScatterND** | overlapping | escalate **B7** | 1 |
| **GridSample** | bilinear | AffineGrid + Resize(bilinear) | 2 |
| **GridSample** | nearest/bicubic | consider arch change | 1 |
| **MaxPool** | `dilations: unsupported` | **WARNING only** — conversion succeeds. **Do NOT patch.** | 5 |
| **MaxPool** | dilation>1 | `Slice+Stack+ReduceMax` (last resort) | 3 |
| Unknown op | — | escalate **B7** | — |

> **MaxPool:** PyTorch always adds `dilations=[1,1]`/`ceil_mode=0`. Dry-run warns but conversion + HTP succeed.

### Einsum — 5 patterns

Einsum = batched MatMul with dim rearrangement (permute+reshape to expose MatMul):

- **A.** `bmchw,bnmc->bmhwn` → `[b*m,h*w,c]@[b*m,c,n]` → reshape `[b,m,h,w,n]`
- **B.** `bchw,bkc->bkhw` → `[b,h*w,c]@[b,c,k]` → permute → `[b,k,h,w]`
- **C.** `bij,bjk->bik` → `torch.matmul(A,B)`
- **D.** `bhij,bhjk->bhik` → merge `[b*h,i,j]@[b*h,j,k]` → reshape
- **General:** shared indices=reduced; batch stays; merge batch+reduced → MatMul → reshape back.

## Validation Gates (ALL after EACH patch)

| Gate | Check | Pass |
|------|-------|------|
| 1 Structural | `onnx.checker.check_model()` | no exception |
| 2 Converter | `qnn-onnx-converter --dry_run` | no unsupported errors |
| 3 Numerical | orig vs patched (CPUExecutionProvider) | cosine ≥ 0.95, no NaN |
| 4 Full (final) | `python qai_convert_fp.py --onnx ...` | "Conversion complete!" |

**Cosine:** ≥0.999 correct · 0.99–0.999 OK · 0.95–0.99 investigate · <0.95 wrong pattern.
**On failure:** Gate1→topo/names. Gate2→more patching. Gate3→wrong pattern, next row. Gate4→`Add(0.0)` after Cast.

> ⚠️ Einsum patches can silently change numerics — validate **all output channels**.

## Discipline

| ❌ Forbidden | ✅ Required |
|---|---|
| CPU fallback / `QnnCpu.dll` as solution | Patch for HTP/DSP compatibility |
| Skip patching, CPU only | Model MUST run on HTP |

Can't patch → escalate B7. Patch in-memory only; validate each patch; stop if dry-run passes (don't over-patch).

## Escalation

| Condition | Code | Evidence |
|-----------|------|----------|
| No pattern exists | **B7** | op name, types, search |
| Patch changes semantics | **B4** | describe change, impact |
| 7+ iterations, no progress | **B3** | patches, logs, ONNX |

Iteration 5+: resolving faster than discovering → continue; new ops faster → escalate.

Full patterns + code → `references/operator_patching.md`.
