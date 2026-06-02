---
name: aipc-toolkit
description: AIPC, AI Porting Conversion. Tools and workflows for QAIRT/AIPC project setup, model conversion, inspection, operator patching, quantization, context-binary generation, and inference on Qualcomm platforms. Use this skill when creating or initializing an AIPC project, exporting AI models to ONNX, converting ONNX models to QNN or SNPE/DLC, converting models to FP16/FP32, patching unsupported operators, generating context binaries, or implementing inference for QNN/SNPE DLC.
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
- "detect target soc" / "qairt devinfo" / "detect device info"

### Project Setup
- "create aipc project" / "create project" / "init project" / "setup project"
- "aipc init" / "aipc setup"

## When to Use

Use this skill for Qualcomm QAIRT/QNN/SNPE model bring-up:
- Export model to ONNX
- Inspect ONNX I/O
- Convert to QNN or SNPE/DLC
- Quantize model
- Generate context binaries
- Run inference and validation
- Create QAIRT/AIPC project

## Required Guardrails

- Run skill scripts from their original skill path unless explicitly noted
- Do not swap out QAIRT toolchains ad-hoc
- `QAIRT_SDK_ROOT` must be set
- On Windows, do not rely on Python arch detection — use OS-native arch commands
- On ARM64 Windows, `platform.machine()` returns `AMD64` under x86_64 emulation.
  Prefer the minimal QAIRT device probe script for SoC detection. Avoid CIM/WMI when possible.
  **Model library target must match the Python process arch**, not the CPU arch:
  - x86_64 emulated Python → `windows-x86_64` model DLL
  - ARM64 native Python → `windows-aarch64` model DLL
  - ARM64X/CHPE hybrid QNN runtime DLLs (`arm64x-windows-msvc`) bridge x86_64→native HTP
- **Cross-platform shell commands:**
  - Python scripts via `subprocess.run()` — no shell quoting issues
  - **Inference execution policy (MANDATORY):**
    - Run inference via `scripts/aipc` wrapper only.
    - If remote target execution is configured (for example `RETMOE_DEVICE_INFO` is set in project config),
      you MUST skip local host inference runs.
    - In this mode, acceptance and validation MUST be executed on the remote target only.
    - Local host inference is not allowed as an acceptance substitute.
    - Do NOT call `snpe-net-run`, `qnn-net-run`, or raw backend CLIs directly for final inference/validation.
  - Avoid PowerShell variables (`$_`, `$env:`, `!`) in bash-invoked commands — use temp `.ps1` files with `-File` or Python `glob.glob()` instead
- **Escalation:** If conversion still fails after export/patch/retry, do not silently replace model architecture. Record error + logs + ONNX snapshot → escalate with full bundle. For B3/B4/B7 criteria → open `references/operator_patching.md`.
- **Dynamic-input ONNX:** If ONNX has dynamic inputs, pass explicit shapes during conversion. See `references/qnn_conversion.md` (QNN: `--input-dim`) or `references/snpe_conversion.md` (SNPE: `--source-model-input-shape`).

### ⚠️ CRITICAL: Context Binary & Model Library Architecture (DO NOT SKIP)

**Context binary** (`.dll.bin` / `.so.bin`):
- ARM Windows: **PREFERRED** for fixed-SoC deployment — `.dll` (with ARM64X/CHPE runtime) also works.
- ARM Linux: **OPTIONAL** — `.so` works directly.
- The binary is SoC-specific and **platform-independent** — same `.bin` works whether host is x86 or ARM64.

**Model library** (`.dll` / `.so`):
- Must match the **host process architecture**, not the target CPU.
- On ARM64 Windows, the QAIRT Python venv runs under x86_64 emulation → compile with `-t windows-x86_64`.
- Compiling for `windows-aarch64` produces a DLL that the x86_64 emulated Python cannot load.

**Context binary generation** runs on the HOST (x86), not on the target device.
- The host uses `qnn-context-binary-generator` with `soc_id`/`dsp_arch` config to compile a binary for the target SoC.
- `--model` input must be a model library matching the **host** toolchain arch: `windows-x86_64` on Windows, `x86_64-linux-clang` on Linux.
- The resulting `.bin` is then deployed to the target for inference.
- Do not add target cross-compilation steps unless the user explicitly asks for target model-library build.
- `soc_id` and `dsp_arch` are **mandatory** — identify them from the target device before generation.
- Do **not** use `/sys/devices/soc0/soc_id` — that is the Linux kernel ID, not the QAIRT `soc_id`.
- For how to read `soc_id`/`dsp_arch` from the target and full generation commands → open `references/host_context_binary_gen.md`.

For platform table, troubleshooting flow, and usage → open `references/host_context_binary_gen.md`.

**ARM64X/CHPE note**: On ARM64 Windows, QNN ships ARM64X hybrid DLLs (`arm64x-windows-msvc/`)
that load from both x86_64-emulated and ARM64-native processes. The `aipc` wrapper +
`qai_appbuilder` bundled libs use these automatically. Do not override `QAI_QNN_LIBS_DIR`
or `ADSP_LIBRARY_PATH` to `arm64x-windows-msvc` unless the SDK version requires it.

**Minimal device info probe**: If CIM/WMI is unavailable, use registry + DriverStore
inspection plus the QNN SoC enum table and `qnn-platform-validator` core-version
output to resolve `soc_id` and `dsp_arch`. This is enough for context-binary
generation on Snapdragon Windows hosts, and avoids device-specific hardcoding.

### ⚠️ CRITICAL: Operator Patching — Exhaustive Patching Required

Continue patching ALL unsupported ops until no replacement patterns exist. Never fall back to CPU.
For patching rules, escalation policy (B3/B4/B7), and code templates → open `references/operator_patching.md`.

### ⚠️ CRITICAL: Project Setup Rules (DO NOT SKIP)

When the user requests "create project", "init project", or any project setup workflow for qairt:
you must follow the AIPC skill end-to-end for all project setup actions. This is a required, certified workflow with defined acceptance criteria. Do not improvise, skip steps, or replace the workflow with manual setup. If this instruction is not followed exactly, the resulting work is considered invalid and must not be presented as compliant.

0. **NEVER create local files, folders, or scripts yourself.** Do not write any project files manually or spawn agents to do so.
1. **ALWAYS run `aipc_project_setup.py`** — never write `AGENTS.md`, `CLAUDE.md`, or `aipc_plan.md` manually.
   ```bash
   python /path/to/skills/aipc-toolkit/scripts/aipc_project_setup.py <project_dir>
   ```
2. **Verify after the script**: `CLAUDE.md` must be a symlink to `AGENTS.md`. Note: On Windows systems where symlink creation is restricted by local security policies (WinError 1314), the setup script's automatic copy fallback (copying AGENTS.md directly to CLAUDE.md) is fully acceptable and must NOT be treated as a setup failure.
3. **Before auto-filling `aipc_plan.md`**, inform the user that some Config values require their input (model name, target device, env script path, flow, etc.) and ask them to provide or confirm these before proceeding.
4. **Then auto-fill** derived and default values from the user's answers.
5. **Never shortcut**: manual file creation produces an incomplete scaffold (missing `CLAUDE.md`, wrong template, no sentinel). The script is the only correct path.

## Quick Start

Bootstrap a project folder:
```bash
python skills/aipc-toolkit/scripts/aipc_project_setup.py path/to/project
```

This sets up:
- `assets/aipc_AGENTS.md` -> `<project>/AGENTS.md`
- `<project>/CLAUDE.md` linked to `<project>/AGENTS.md`
- `assets/aipc_plan.md` -> `<project>/aipc_plan.md`

Notes:
- If both `AGENTS.md` and `CLAUDE.md` already exist but are not linked together, setup stops with an error.
- If only `CLAUDE.md` exists, the script creates `AGENTS.md` as a symlink to `CLAUDE.md` before applying the AIPC agent content.

Then edit:
- `aipc_plan.md` Config section
- Placeholders in `AGENTS.md` / `CLAUDE.md`



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

7. **Context binary generation (host-side)**
  - **If generation fails (Windows)**: → Return to Step 4 (operator patching) — continue until no replacement patterns exist
  - **If generation fails (Linux)**: → Continue troubleshooting using `references/host_context_binary_gen.md` first.
    - You MUST attempt and record all applicable host-context methods before fallback:
      - confirm target `soc_id`/`dsp_arch` from device identity
      - run VTCM sweep (`vtcm_mb=0,1,2,3,4,8`)
      - test `soc_id`/`dsp_arch` alternatives from QAIRT mapping
      - if needed, test architecture-based config (`htp_arch`) / no-`soc_id` path
    - Only after all above methods fail with logs may you proceed with `.so` directly on Linux.
  - See `references/host_context_binary_gen.md` for full commands and config templates

8. **Inference + validation**
   - Use `aipc` wrapper to run inference script
   - Validate accuracy against ONNX baseline.
   - If remote target execution is configured, you MUST run inference/validation on the remote target only.
   - In this case, skip local host inference entirely.
   - Final pass/fail must come from remote target results.

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
| Host context binary gen | `references/host_context_binary_gen.md` |
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
| `scripts/aipc_dev_gen_contextbin.py` | Context binary generation (on-device / legacy) |
| `scripts/aipc_dev_gen_contextbin_x86.py` | Host-side context binary generation (x86 host → target SoC) |
| `scripts/aipc_qairt_devinfo.ps1` | QAIRT SoC and DSP/HTP architecture auto-detection (Windows on Snapdragon) |


> ⚠️ **Inference must use `scripts/aipc` wrapper** (including remote target runs). Direct `snpe-net-run`/`qnn-net-run` is for diagnostics only, not acceptance validation.
> ⚠️ **Always prefer the wrapper scripts** (`aipc_convert_fp.py`, `aipc_convert_int.py`) over calling `qnn-onnx-converter` or `qnn-model-lib-generator` directly.
