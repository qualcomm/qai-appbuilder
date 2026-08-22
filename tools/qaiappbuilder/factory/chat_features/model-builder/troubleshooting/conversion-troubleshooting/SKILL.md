---
skill_id: conversion-troubleshooting
tier: base
triggers: ["Graph Compose failure", "unable to find graphName", "Wrong number of Parameters 5", "Conv2d failed 3110", "loadRemoteSymbols 4000", "0x80000406", "arm64x", "aarch64", "arch mismatch"]
sources: ["references/context_binary.md", "references/qnn_conversion.md"]
---

# Conversion Troubleshooting (base)

> 🧭 Diagnosis framework: `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/_diagnosis-framework.md`; this SKILL covers conversion/compilation failures.

## Responsibility

Diagnose and fix failures in the ONNX → C++/bin → DLL → context-binary (`.bin`) chain on WoS ARM64:
graph-name mismatches, missing VS ARM64 environment, missing HTP runtime files, architecture
mismatches, and 0-byte/`bins/` output traps. Root causes are usually **environment/config** — check those first before touching the graph.

## Trigger signals

- `Graph Compose failure` / `unable to find graphName:<x>` / `MODEL_INVALID_ARGUMENT_ERROR`
- `Wrong number of Parameters 5` / `Op specific validation failed` / `Conv2d failed 3110`
- `No CMAKE_C_COMPILER` / `VCTargetsPath.vcxproj` / `BaseOutputPath not set`
- `loadRemoteSymbols failed with err 4000` / `DspTransport.openSession qnn_open failed, 0x80000406`
- `arm64x` vs `aarch64` DLL load errors

## Core knowledge

### Structured troubleshooting flow

```
1. `windows-arm64` / `linux-aarch64` → HTP `.bin` MANDATORY. `windows-x64` / `linux-x64` → the same QNN HTP `.bin` is emitted; loading rules → `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md`. `linux-aarch64` optional (`.so` works).
2. Read the error: operator name / code (0xc26) / "unsupported" / "validation failed".
3. If it's an operator → hand off to operator-patching skill.
4. If it's env/config → fix per tables below, re-convert.
5. All patterns exhausted → escalate B7 / B8; consider .dlc or CPU/GPU alternative.
```

### #1 cause: graph_names mismatch

`graph_names` in `htp_backend_config_v{73|81}.json` **must exactly match** the DLL graph name (Flow C only; Flow A uses `.dlc` + `--soc_model`). Graph name = **stem of `--output_path`** (`output/my_model.cpp` → `"my_model"`).

```
[ERROR] getQnnGraphConfigFromInfo() unable to find graphName:qnn_model ...
[ERROR] ... got MODEL_INVALID_ARGUMENT_ERROR
Graph Compose failure
```
**Fix:** `--auto-config`, or set `graph_names` = output_path stem.

### `Wrong number of Parameters 5` / `Conv2d failed 3110` — MISSING VS ARM64 ENV

Almost always missing ARM64 build env, not an operator issue. Tools need `vcvarsall.bat arm64`.

- Run inside `.bat` with `call "%_VCVARSALL%" arm64` at top. `cmd /c` does **NOT** inherit env.
- For **DLC→bin**: same error = `QnnHtpV73Stub.dll` or `QnnHtpPrepare.dll` missing from CWD.
- Only patch `.cpp` if error persists **after** correct env.

### HTP runtime files must be in working directory

Generator resolves `.cat`/`Skel.so` relative to **process CWD**. `qai_dev_gen_contextbin.py` handles this automatically.

- **v73:** `QnnHtp.dll`, `libqnnhtpv73.cat`, `libQnnHtpV73Skel.so`
- **v81:** `QnnHtp.dll`, `QnnHtpV81Stub.dll`, `libqnnhtpv81.cat`, `libQnnHtpV81Skel.so`
- **DLC→bin adds:** `QnnModelDlc.dll`, `QnnHtp*Stub.dll`, `QnnHtpPrepare.dll`, `QnnHtpNetRunExtensions.dll`

**Missing →** `loadRemoteSymbols failed with err 4000` / `0x80000406`. (DLC→bin: `4000` alone = non-fatal.)

### `arm64x` ≠ `aarch64`

`lib/arm64x-windows-msvc/` = ARM64EC. Generator **cannot load arm64x DLLs** → always use `lib/aarch64-windows-msvc/`.
v81: `--backend` MUST be `QnnHtp.dll`, NOT `QnnHtpV81Stub.dll`.

### 0-byte / `bins/` trap

Same `--output_dir` for multiple models → `bins/` subdir for first, 0-byte placeholders for rest.
**Fix:** dedicated `--output_dir` per model. Always verify `.bin` size (valid = several MB).

> `qnn-context-binary-generator.exe` returns non-zero even on success and emits `Unknown Key` warnings.
> Check `.bin` exists and is non-empty; don't rely on exit code.

### Common Errors Quick Reference

| Error | Cause | Fix |
|-------|-------|-----|
| `unable to find graphName` | graph_names ≠ DLL graph name | `--auto-config`; or = `--output_path` stem |
| `Graph Compose failure` | config mismatch or unsupported op | check graph_names; check operator support |
| `No CMAKE_C_COMPILER` | VS ARM64 env not initialized | `vcvarsall.bat arm64` in same `.bat` |
| `Unable to load backend: QnnHtp.dll` | DLL not in working dir | copy `QnnHtp.dll` to working dir |
| `Backend version mismatch` | wrong SDK version | same SDK version for all steps |
| `Wrong number of Parameters 5` | missing VS ARM64 env or `.cat`/`Skel.so` | vcvarsall arm64; HTP files in CWD |
| `VCTargetsPath` / `BaseOutputPath not set` | BuildTools MSBuild can't compile ARM64 | `vc_targets_path` from `qairt_env.json`; VS 2022 **Community** |

### Architecture preflight (mandatory)

Do **not** use `platform.machine()` / `$env:PROCESSOR_ARCHITECTURE` (emulation-affected).
Use `(Get-WmiObject Win32_Processor).Architecture` (12=ARM64, 9=x64) or `dumpbin /headers`.

| `HOST_OS`      | Input model lib | Action |
|----------------|-----------------|--------|
| `windows-arm64` | ARM64 `.dll` / `.dlc` | ✅ Native HTP generator (Flow A `.dlc`; Flow C `.dll`) |
| `windows-x64`   | `.dlc` only | ✅ x86 host generator via `qai_dev_gen_contextbin_x86.py` (auto-delegated). ARM64 `.dll` rejected. |
| `linux-aarch64` | aarch64 `.so` / `.dlc` | ✅ Native HTP generator |
| `linux-x64`     | `.dlc` | x86 host generator via `qai_dev_gen_contextbin_x86.py` (see `x64-host-notes.md`). `.so` blocked — run on ARM device. |

### Tool path rules (WoS ARM64, QAIRT 2.45+)

**Legacy Flow C (DLL pipeline) toolchain paths** — the DLC pipeline (Flow A, default) uses `qairt-converter` + `qairt-quantizer` from `bin/<host_arch>/` and skips DLL compilation entirely, so this table only matters when the user runs `run_pipeline_legacy.py`:

| Step | Tool | Arch dir |
|------|------|----------|
| ONNX → C++/bin (Flow C) | `qnn-onnx-converter` | `bin/x86_64-windows-msvc/` (Python, x86 emulation) |
| C++/bin → DLL (Flow C) | `qnn-model-lib-generator` | `bin/aarch64-windows-msvc/` (**NOT x86_64** — most common mistake) |
| DLL/DLC → `.bin` (both flows) | `qnn-context-binary-generator.exe` | `bin/aarch64-windows-msvc/` (native ARM64) |

Prefer wrappers (`run_pipeline.py` for DLC path, `run_pipeline_legacy.py` for DLL path, `qai_dev_gen_contextbin.py --auto-config`) — they handle env init, HTP file copy, and arch dirs automatically.

Mismatch → **do not run** the generator locally; instruct user to run on target device.

## HTP Hard-Constraint Failure Root-Cause Table

> Chip-agnostic constraints. **Failure ≠ unconvertible** — most can be worked around by partitioning / rewriting / reducing scale.

| Symptom | Root cause | Action |
|---------|------------|--------|
| `qnn-context-binary-generator failed on HTP` | Graph exceeds HTP compiler scale limit | Partition to CPU; reduce resolution; audit int32/5D Gather |
| HTP rejects int32 Gather | HTP does not accept int32 indices | Change index dtype / one-hot×MatMul / keep embedding on CPU |
| 5D Gather inefficient / compile failure | 5D scalar-index extremely inefficient | → **Slice + Squeeze** |
| `unsupported operator` | Unsupported op | Equivalent rewrite / partition to CPU / adjust opset |
| `qairt-converter error` | Graph structure rejected | Fix shapes / shape_inference / partition out |
| `qnn-net-run failed on device` | Runtime execution failure | Audit I/O contract; partition to isolate; reduce scale |
| Export/convert timeout | Graph too large / dynamic loop | Reduce size; partition subgraph; fix shapes |

### Pre-conversion HTP-friendly rewrite rules

After each rewrite, **validate output cosine≈1.0** (mathematically identical rewrites only); revert otherwise:

| Rewrite | Description | Hard constraint avoided |
|---------|-------------|-------------------------|
| **5D Gather → Slice + Squeeze** | Equivalent substitution for 5D scalar-index Gather | Directly avoids "5D Gather inefficient" |
| **Slice(step=-1) → fixed-index Gather** | Replace reverse slice with fixed-index Gather | HTP is more efficient with fixed-index Gather |
| **Slice constant folding** | `Shape→Gather→Div` dynamic `ends` folded to constants when input shape is fixed | Eliminates dynamic shapes |
| **Where→Add** (attention mask) | `Where(Equal(mask,0),-1e4,s)` → `(s+1e4)*mask-1e4` | Eliminates expensive Where on HTP |

> Discipline: do not rewrite qairt internal fusion patterns; fewer ops ≠ easier to compile; MHA→SHA split overhead often outweighs gains; patching only some MatMuls corrupts numerics. Acceptance criterion = "cosine≈1 and compilation passes".

### Pre-conversion checklist

1. int32-index Gather → HTP rejects; rewrite or keep on CPU
2. 5D scalar-index Gather → Slice+Squeeze
3. Dynamic shapes → fix + constant-fold
4. Large graph / giant attention → partition early
5. Detection/segmentation post-processing head → partition to CPU
6. Non-standard attention / SSD anchor → equivalent rewrite or partition out

> *int32 Gather→rewrite; 5D Gather→split; dynamic shape→fix; large graph→partition; post-processing→CPU.*

## Blocking Conditions

- **B8** — context binary fails on an HTP host (`windows-arm64` / `linux-aarch64`). Do NOT degrade to `.dll`; do NOT retry x86_64 generator on an ARM64 DLL. 0-byte generator = damaged SDK file → `sdk-integrity-recovery`. On x64 hosts see `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` before reporting the run as real-device performance.
- **B5** — target device unavailable → stop, ask user.

## Escalation

Escalate when: failure persists after env fix + patch + retry; no rewrite for required op; runtime rejects graph. Bundle: original + patched ONNX, commands, logs, repro steps.

Full commands + backend config → `references/context_binary.md` and `references/qnn_conversion.md`.
