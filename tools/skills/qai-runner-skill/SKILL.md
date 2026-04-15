---
name: aipc-toolkit
description: AIPC, AI Porting Conversion. Tools and workflows for model conversion, inspection, operator patching, quantization, and inference to Qualcomm platform. Use this skill when working with AI model to ONNX, ONNX models for QNN/SNPE DLC, converting models to FP16/FP32, patching unsupported operators, generating context binaries, or implementing inference for QNN/SNPE DLC.
---

# AIPC Toolkit

## Trigger Phrases

**Always activate this skill** when the user mentions:

### Conversion
- "convert model to qnn" / "qnn conversion" / "convert to qnn"
- "convert model to dlc" / "snpe conversion" / "convert to dlc"
- "convert onnx to qnn" / "convert onnx to snpe"
- "generate context binary" / "context bin" / "qnn context binary"
- "run qnn inference" / "snpe inference" / "qairt inference"

### Operator Patching
- "operator not supported" / "unsupported operator"
- "patch operator" / "operator patch"
- "converter failed" / "conversion failed"
- "dry run failed" / "unsupported ops found"

### Diagnostics
- "check htp" / "htp ready" / "htp check"
- "aipc diagnose" / "environment check"

## When to Use

Use this skill for Qualcomm QAIRT/QNN/SNPE model bring-up:
- Export model to ONNX
- Inspect ONNX I/O
- Convert to QNN or SNPE/DLC
- Quantize model
- Generate context binaries
- Run inference and validation

## Required Guardrails

- Run skill scripts from their original skill path unless explicitly noted
- Do not swap out QAIRT toolchains ad-hoc
- `QAIRT_SDK_ROOT` must be set
- On Windows, do not rely on Python arch detection — use OS-native arch commands
- **Cross-platform shell commands:**
  - Python scripts via `subprocess.run()` — no shell quoting issues
  - **Inference execution policy (MANDATORY):**
    - Run inference via `scripts/aipc` wrapper only.
    - Do NOT call `snpe-net-run`, `qnn-net-run`, or raw backend CLIs directly for final inference/validation.
  - Avoid PowerShell variables (`$_`, `$env:`, `!`) in bash-invoked commands — use temp `.ps1` files with `-File` or Python `glob.glob()` instead
- **Escalation:** If conversion still fails after export/patch/retry, do not silently replace model architecture. Record error + logs + ONNX snapshot → escalate with full bundle. For B3/B4/B7 criteria → open `references/operator_patching.md`.
- **Dynamic-input ONNX:** If ONNX has dynamic inputs, pass explicit shapes during conversion. See `references/qnn_conversion.md` (QNN: `--input-dim`) or `references/snpe_conversion.md` (SNPE: `--source-model-input-shape`).

### ⚠️ CRITICAL: Context Binary Requirement (DO NOT SKIP)

ARM Windows: `.dll.bin` is **REQUIRED** — `.dll` alone will NOT load.
ARM Linux: `.so.bin` is **OPTIONAL** — `.so` works directly.
For full platform table, troubleshooting flow, and usage → open `references/context_binary.md`.

Linux cross-host note:
- If host arch differs from Linux target arch (e.g., x86 host + aarch64 target), context-binary generation is **best-effort only**.
- On generation error in this scenario, log the reason and proceed to inference with `.so`.
- Do not block unless inference itself fails.

### ⚠️ CRITICAL: Operator Patching — Exhaustive Patching Required

Continue patching ALL unsupported ops until no replacement patterns exist. Never fall back to CPU.
For patching rules, escalation policy (B3/B4/B7), and code templates → open `references/operator_patching.md`.

## Quick Start

Bootstrap a project folder:
```bash
python skills/aipc-toolkit/scripts/aipc_project_setup.py path/to/project
```

This attaches:
- `assets/aipc_AGENTS.md` -> `<project>/AGENTS.md`
- `assets/aipc_plan.md` -> `<project>/aipc_plan.md`

Then edit:
- `aipc_plan.md` Config section
- Placeholders in `AGENTS.md`

## Core Workflow

1. **Setup QAIRT environment**
   - Run `aienv.ps1` (Windows) or `source aienv.sh` (Linux)
   - Verify `QAIRT_SDK_ROOT` is set

2. **Export source model to ONNX**
   - Use model's export script (e.g., `export_onnx.py`)
   - Recommended: opset_version=13 or higher

3. **Inspect ONNX I/O and operator compatibility**
   - Run: `python aipc_inspect_onnxio.py model.onnx`
   - Run converter dry-run to detect unsupported operators
   - **If unsupported operators found → Proceed to Step 4**

4. **Operator Patching (if needed) — Approach Selection**

   **Decision Tree:**

   ```
   1. Can you access PyTorch model BEFORE ONNX export?
      ├─ YES → Go to 2
      └─ NO  → Use ONNX Surgery (Approach 3)

   2. Is the operator an explicit PyTorch module?
      ├─ YES → In-Memory Module Replacement (Approach 1)
      └─ NO  → Go to 3

   3. Is the operator generated during ONNX export?
      ├─ YES → Custom Symbolic Handlers (Approach 2)
      └─ NO  → ONNX Surgery (Approach 3)
   ```

   **Approach 1: In-Memory Model Patch (Preferred)**
   - Modify model.forward() or replace module instances
   - Use `references/operator_patching.md` templates
   - Export patched model → `model_patched.onnx`

   **Approach 2: Custom Symbolic Handlers (Excellent)**
   - Register handlers before export: `register_custom_op_symbolic()`
   - Define ONNX graph for unsupported aten ops
   - Export with handlers active → `model_patched.onnx`

   **Approach 3: ONNX Surgery (Fallback)**
   - Use when source model is not accessible
   - Directly modify ONNX graph to replace unsupported ops
   - Validate: `onnx.checker.check_model(model_patched.onnx)`

   **After Each Patch — Validation Gates:**

   | Gate | Check | Command | Pass Criteria |
   |------|-------|---------|---------------|
   | 1 | ONNX Validity | `onnx.checker.check_model()` | No exceptions |
   | 2 | Converter | `qnn-onnx-converter --dry_run` | No unsupported ops |
   | 3 | Numerical | Compare with baseline | Cosine ≥ 0.95 |

   - **Re-inspect:** Run dry-run to verify no unsupported ops remain
   - **Iterate:** If new unsupported ops found → repeat Step 4
   - **Track:** Update `aipc_plan.md` with ALL patched operators
   - **Stop when:** All ops resolved OR exit criteria met (see Escalation Policy)

5. **Convert float model**
   - QNN path: `python aipc_convert_fp.py --onnx model_patched.onnx ...`
   - SNPE path: `python aipc_convert_snpe.py --onnx model_patched.onnx ...`

6. **Optional: Quantization** (INT8/INT16/A16W8)
   - Use `aipc_convert_int.py` with calibration data

7. **Context binary generation**
  - ARM Windows: **REQUIRED** — inference will fail without it
  - ARM Linux: **OPTIONAL** — `.so` works directly
  - x86 Linux: Not applicable (CPU-only)
  - **If generation fails (Windows)**: → Return to Step 4 (operator patching) — continue until no replacement patterns exist
  - **If generation fails (Linux)**: → Can proceed with `.so` directly
  - **If generation fails (Linux cross-host/cross-arch)**: → Skip this step, log fallback, proceed with `.so`

8. **Inference + validation**
   - Use `aipc` wrapper to run inference script
   - Validate accuracy against ONNX baseline

> **⚠️ Step 7 MANDATORY for Windows ARM**: Without `.dll.bin`, model loading will FAIL.
> On Linux ARM, context binary is optional and `.so` fallback is valid.
> - See `references/inference.md` for model file resolution search order.

---

## Reference Map

Open only what you need:

| Topic | File |
|-------|------|
| Environment setup (Windows) | `references/win_qairt_setup.md` |
| Export + ONNX validation | `references/model_export_validation.md` |
| **Operator patching** | **`references/operator_patching.md`** |
| QNN conversion | `references/qnn_conversion.md` |
| SNPE conversion | `references/snpe_conversion.md` |
| Quantization | `references/model_quantization.md` |
| Context binary | `references/context_binary.md` |
| Inference | `references/inference.md` |
| Troubleshooting | `references/troubleshooting.md` |

## Script Index

| Script | Purpose |
|--------|---------|
| `scripts/aipc` | ONNX wrapper loader |
| `scripts/aipc_project_setup.py` | Project bootstrap |
| `scripts/aipc_inspect_onnxio.py` | ONNX I/O inspection |
| `scripts/aipc_convert_fp.py` | QNN float conversion |
| `scripts/aipc_convert_int.py` | QNN quantized conversion |
| `scripts/aipc_convert_snpe.py` | SNPE conversion wrapper |
| `scripts/aipc_dev_gen_contextbin.py` | Context binary generation |

> ⚠️ **Inference must use `scripts/aipc` wrapper** (including remote target runs). Direct `snpe-net-run`/`qnn-net-run` is for diagnostics only, not acceptance validation.
> ⚠️ **Always prefer the wrapper scripts** (`aipc_convert_fp.py`, `aipc_convert_int.py`) over calling `qnn-onnx-converter` or `qnn-model-lib-generator` directly.
