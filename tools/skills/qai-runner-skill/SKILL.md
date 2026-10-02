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
- "qai diagnose" / "environment check"
- "detect target soc" / "qairt devinfo" / "detect device info"
- "fastrpc memory map" / "err 1002" / "smmu" / "context binary too large"
- "split model" / "model split" / "split unet" / "context binary size"

### Project Setup
- "create aipc project" / "init aipc project" / "setup aipc project"
- "qai init" / "qai setup"
- "multi-component" / "pipeline export" / "stable diffusion export" / "diffusers export"
- "export unet" / "export vae" / "export text encoder" / "sub-model export"

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
- **`QAIRT_TMP_DIR` — set when `/tmp` is full**: `qnn-onnx-converter` copies external-data ONNX
   weights to a temp directory before processing. On servers where `/tmp` is a small `tmpfs`, this
   fails with `Failed to copy external data to the disk at: /tmp/... permission or space issue`.
   Set `QAIRT_TMP_DIR` to a path with at least 2× the model's weight file size free:
   ```bash
   export QAIRT_TMP_DIR=/path/with/enough/space   # e.g. /workspace/qairt_tmp
   ```
   Check before any converter call: `df -h /tmp` — if `Use%` is 100%, set `QAIRT_TMP_DIR`.
- **LONG-RUNNING JOBS — CRASH RESILIENCE (MANDATORY):** Any operation expected to take >2 minutes
  (conversion, context binary generation, inference) MUST be launched as a detached systemd service
  so it survives agent session crashes:
  ```bash
  systemd-run --user --unit=aipc-<task> --description="<desc>" bash /path/to/script.sh
  ```
  - Write all output to a persistent log file (not stdout): `exec >> /path/to/job.log 2>&1`
  - Use idempotent skip-if-done guards: `if [ ! -f output.so ]; then ...; fi`
  - Poll progress: `systemctl --user status aipc-<task>.service --no-pager` + `tail -20 log`
  - **NEVER use `nohup ... &` or `tmux`** — both are killed when the agent session ends.
- **HOST OOM FOR LARGE MODELS:** Check `free -h` and model size before conversion.
  If available RAM < 2× model size, run `qnn-onnx-converter` and `qnn-model-lib-generator`
  on the remote target device instead. For an explicitly authorized deployment-feasibility
  test, the controller may set `AIPC_DISABLE_MEMORY_GATE=1`; in that case continue on the
  selected host, record the measured RAM/disk limits, and stop immediately on the first
  resource or converter failure. This override disables only the preflight routing gate; it
  does not permit ignoring an actual OOM, disk-full, or failed-command result.
  ```bash
  # rsync ONNX + external data to remote
  rsync -av onnx_models/ user@remote:work/onnx_models/
  # run conversion remotely (x86 converter works on ARM Linux too)
  ssh user@remote "cd work && python qai_convert_fp.py --onnx onnx_models/model.onnx \
    --host-arch x86_64-linux-clang --target-arch aarch64-ubuntu-gcc9.4 ..."
  # rsync .so / .bin / .cpp back
  rsync -av user@remote:work/qairt_output/ qairt_output/
  ```
  Check: `df -h /tmp` and `free -h` before any conversion; if RAM < 2× model size, use remote path.
- **PYTHON VERSION ON ARM LINUX (QAIRT 2.44+):** Ubuntu 24.04 ARM Linux requires Python 3.12.
  The default `aienv.sh` venv may activate Python 3.10, which is **incompatible** and causes:
  `NotImplementedError: On Linux aarch64, Python 3.12 is required. Got: 3.10`
  Always verify before running any converter or wrapper:
  ```bash
  python3 --version                                    # must be 3.12+
  $QAIRT_SDK_ROOT/bin/check-python-dependency --dry-run
  ```
  If Python 3.10 is active, create a 3.12 venv once. **Prefer `uv`** (faster, no pip needed):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv if not present
  uv venv ~/pyqairt312 --python 3.12
  uv pip install --python ~/pyqairt312 \
    diffusers transformers onnxruntime onnx Pillow numpy
  source ~/pyqairt312/bin/activate
  $QAIRT_SDK_ROOT/bin/check-python-dependency          # installs QAIRT-specific deps
  ```
  Fallback (no uv): `python3.12 -m venv ~/pyqairt312 --system-site-packages`
  Use `~/pyqairt312/bin/python3` for all subsequent converter and inference calls.
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
    - Run inference via `scripts/qai` wrapper only.
    - **MANDATORY**: You MUST use the `onnxwrapper.py` provided within the `aipc-toolkit` skill (`scripts/onnxwrapper.py`).
    - **PROHIBITED**: NEVER use or fallback to the `onnxwrapper.py` bundled with the QAIRT SDK or `qai_appbuilder` package (e.g., from `site-packages/qai_appbuilder/onnxwrapper.py`). The skill version contains critical patches for AIPC workflows.
    - Before any final inference run, perform **wrapper artifact preflight**:
      - print which QNN artifact will be selected for `{ONNX_FILE}`
      - remove or quarantine stale matched artifacts (platform-dependent: `<model>.onnx.dll.bin` on Windows, `<model>.onnx.so.bin` on Linux) before deploying a new one
      - in context-binary mode, prefer ONNX-matching deployment filename: `<model>.onnx.dll.bin` on Windows, `<model>.onnx.so.bin` on Linux
      - the wrapper discovers context binaries by appending platform-specific suffixes to the ONNX path; see `onnxwrapper.py:_find_qnn_model_file()` for the full candidate list
    - If remote target execution is configured (for example `REMOTE_DEVICE_INFO` is set in project config),
      you MUST skip local host inference runs.
    - In this mode, acceptance and validation MUST be executed on the remote target only.
    - Local host inference is not allowed as an acceptance substitute.
    - **`qai_appbuilder` version mismatch:** If `python qai` segfaults (exit 139) when loading
      a context binary, the installed `qai_appbuilder` version is incompatible with the QAIRT SDK.
      **Preferred fix — build from source against the exact QAIRT SDK version:**
      ```bash
      git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
      cd qai-appbuilder
      uv pip install --python <venv> wheel setuptools pybind11 build
      export QNN_SDK_ROOT=/path/to/qairt/<version>
      export QAI_TOOLCHAINS=aarch64-oe-linux-gcc11.2   # Linux ARM; see BUILD.md for other platforms
      python -m build -w
      uv pip install --python <venv> --force-reinstall dist/qai_appbuilder-*.whl
      ```
      The built wheel version matches the QAIRT SDK exactly (e.g. `2.47.0`), resolving the segfault.
      See `references/inference.md` §qai-appbuilder-build for full steps and platform notes.
      **Fallback** (build not feasible): use `qnn-net-run` CLI directly
      (see `references/inference.md` §qnn-net-run-fallback). Record in Issue Log.
    - Do NOT call `snpe-net-run`, `qnn-net-run`, or raw backend CLIs directly for final inference/validation.
    - **Linux ARM runtime-libs pinning (MANDATORY for remote acceptance):**
      - Do not rely on implicit wrapper auto-resolution when multiple target toolchain lib folders exist.
      - Explicitly set `QAI_QNN_LIBS_DIR` to the intended runtime directory for deployment and prepend it to `LD_LIBRARY_PATH`.
      - Example:
        ```bash
        export QAI_QNN_LIBS_DIR="$QAIRT_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"
        export LD_LIBRARY_PATH="$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH"
        export ADSP_LIBRARY_PATH="$QAIRT_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned"
        ```
      - If not pinned, mixed runtime loading may produce:
        - `Failed to load skel, error: 4000`
        - `Transport layer setup failed: 14001`
        - `Failed to parse platform config: 14001`
        - `Unsupported SoC model ...` / `Invalid dsp arch ...`
  - Avoid PowerShell inline pipelines and variables (`$_`, `$env:`, `!`) in command arguments passed via `-Command` (e.g., `Where-Object { $_.LastWriteTime }` gets expanded to `{ .LastWriteTime }` by the outer shell, breaking syntax). Always write PowerShell pipelines to a temporary `.ps1` file and execute it using the `-File` parameter, or use Python globbing/filesystem functions instead.
  - **Console Encoding Guardrail**: Local Windows shells can use non-UTF-8 encodings (such as `cp950` or `cp437`), which frequently trigger `UnicodeEncodeError` or `UnicodeDecodeError` when processing console outputs with special characters. Always enforce UTF-8 encoding/decoding where possible, and use `errors='replace'` or `errors='ignore'` in Python subprocess handling.
- **Escalation:** If conversion still fails after export/patch/retry, do not silently replace model architecture. Record error + logs + ONNX snapshot → escalate with full bundle. For B3/B4/B7 criteria → open `references/operator_patching.md`.
- **Dynamic-input ONNX:** If ONNX has dynamic inputs, pass explicit shapes during conversion. See `references/qnn_conversion.md` (QNN: `--input-dim`) or `references/snpe_conversion.md` (SNPE: `--source-model-input-shape`).

### ⚠️ CRITICAL: Context Binary & Model Library Architecture (DO NOT SKIP)

**Context binary** (`.dll.bin` / `.so.bin`):
- ARM Windows: **PREFERRED** for fixed-SoC deployment — `.dll` (with ARM64X/CHPE runtime) also works.
- ARM Linux: **OPTIONAL** — `.so` works directly.
- The binary is SoC-specific and **platform-independent** — same `.bin` works whether host is x86 or ARM64.

**Model library** (`.dll` / `.so`):
- Must match the **host process architecture**, not the target CPU.
- On ARM64 Windows, the QAIRT Python venv runs under x86_64 emulation → qnn-model-lib-generator tool compile with `-t windows-x86_64`.
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
that load from both x86_64-emulated and ARM64-native processes. The `qai` wrapper +
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
1. **ALWAYS run `qai_project_setup.py`** — never write `AGENTS.md`, `CLAUDE.md`, or `plan.md` manually.
   ```bash
   python /path/to/skills/aipc-toolkit/scripts/qai_project_setup.py <project_dir>
   ```
2. **Verify after the script**: `CLAUDE.md` must be a symlink to `AGENTS.md`. Note: On Windows systems where symlink creation is restricted by local security policies (WinError 1314), the setup script's automatic copy fallback (copying AGENTS.md directly to CLAUDE.md) is fully acceptable and must NOT be treated as a setup failure.
3. **Before auto-filling `plan.md`**, inform the user that some Config values require their input (model name, target device, env script path, flow, etc.) and ask them to provide or confirm these before proceeding.
4. **Then auto-fill** derived and default values from the user's answers.
5. **Never shortcut**: manual file creation produces an incomplete scaffold (missing `CLAUDE.md`, wrong template, no sentinel). The script is the only correct path.

## Quick Start

Bootstrap a project folder:
```bash
python skills/aipc-toolkit/scripts/qai_project_setup.py path/to/project
```

This sets up:
- `assets/qai_AGENTS.md` -> `<project>/AGENTS.md`
- `<project>/CLAUDE.md` linked to `<project>/AGENTS.md`
- `assets/plan.md` -> `<project>/plan.md`

Notes:
- If both `AGENTS.md` and `CLAUDE.md` already exist but are not linked together, setup stops with an error.
- If only `CLAUDE.md` exists, the script creates `AGENTS.md` as a symlink to `CLAUDE.md` before applying the AIPC agent content.

Then edit:
- `plan.md` Config section
- Placeholders in `AGENTS.md` / `CLAUDE.md`



## Core Workflow

1. **Setup QAIRT environment**
   - Run `aienv.ps1` (Windows) or `source aienv.sh` (Linux)
   - Verify `QAIRT_SDK_ROOT` is set

2. **Export source model to ONNX**
   - Use model's export script (e.g., `export_onnx.py`)
   - Recommended: opset_version=13 or higher

3. **Inspect ONNX I/O and operator compatibility**
   - Run: `python qai_inspect_onnxio.py model.onnx`
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
   - **Track:** Update `plan.md` with ALL patched operators
   - **Stop when:** All ops resolved OR exit criteria met (see Escalation Policy)

5. **Convert float model**
   - QNN path: `python qai_convert_fp.py --onnx model_patched.onnx ...`
   - SNPE path: `python qai_convert_snpe.py --onnx model_patched.onnx ...`

6. **Optional: Quantization** (INT8/INT16/A16W8)
   - QNN: use `qai_convert_int.py` (QAIRT default) or `qai_convert_aimet.py` (AIMET path) with calibration data.
   - SNPE/DLC: use `qairt-converter` to create an FP32 DLC, then `qairt-quantizer` to quantize it.
   - Treat `snpe-dlc-quant` as a legacy fallback only for older SDKs without `qairt-quantizer`.

7. **Context binary generation (host-side)**
  - For ARM target context generation, do not run `qai_dev_gen_contextbin_x86.py` without explicit target config.
    - Preferred: use direct `qnn-context-binary-generator` flow from `references/host_context_binary_gen.md`.
    - If using `qai_dev_gen_contextbin_x86.py`, pass `--config_file <backend_extension.json>` (with `soc_id` / `dsp_arch` config).
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
   - Use `qai` wrapper to run inference script
   - Validate accuracy against ONNX baseline.
   - Perform wrapper preflight before final run:
     - verify selected artifact path
     - clean stale matched `.bin` files in workdir (including `.onnx.dll.bin` on Windows, `.onnx.so.bin` on Linux)
     - ensure deployed context binary name matches ONNX discovery rule: `<model>.onnx.dll.bin` on Windows, `<model>.onnx.so.bin` on Linux
     - on Windows, the wrapper also matches `<model>.onnx.dll.bin`, `<model>.dll.bin`, `<model>.dll` in that order
   - On Linux ARM targets, set `QAI_QNN_LIBS_DIR` explicitly to intended runtime libs dir before running `qai`.
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
| **Multi-component pipelines** | **`references/model-architectures/multi_component_pipeline.md`** |
| **Operator patching** | **`references/operator_patching.md`** |
| QNN conversion | `references/qnn_conversion.md` |
| SNPE conversion | `references/snpe_conversion.md` |
| Quantization | `references/model_quantization.md` |
| Context binary | `references/context_binary.md` |
| Host context binary gen | `references/host_context_binary_gen.md` |
| Large context binary / model split | `references/model-architectures/model_split.md` |
| Inference | `references/inference.md` |
| Troubleshooting | `references/troubleshooting.md` |
| Optimization | `references/optimization.md` |

## Script Index

| Script | Purpose |
|--------|---------|
| `scripts/qai` | ONNX wrapper loader |
| `scripts/qai_project_setup.py` | Project bootstrap |
| `scripts/qai_inspect_onnxio.py` | ONNX I/O inspection |
| `scripts/qai_convert_fp.py` | QNN float conversion |
| `scripts/qai_convert_int.py` | QNN quantized conversion (QAIRT path) |
| `scripts/qai_convert_aimet.py` | QNN/SNPE quantized conversion using AIMET (Linux only) |
| `scripts/qai_convert_snpe.py` | SNPE conversion wrapper |
| `scripts/qai_dev_gen_contextbin.py` | Context binary generation (on-device / legacy) |
| `scripts/qai_dev_gen_contextbin_x86.py` | Host-side context binary generation (x86 host → target SoC) |
| `scripts/qai_qairt_devinfo.ps1` | QAIRT SoC and DSP/HTP architecture auto-detection (Windows on Snapdragon) |


> ⚠️ **Inference must use `scripts/qai` wrapper** (including remote target runs). Direct `snpe-net-run`/`qnn-net-run` is for diagnostics only, not acceptance validation.
> ⚠️ **Always prefer the wrapper scripts** (`qai_convert_fp.py`, `qai_convert_int.py`, `qai_convert_aimet.py`) over calling `qnn-onnx-converter` or `qnn-model-lib-generator` directly.
