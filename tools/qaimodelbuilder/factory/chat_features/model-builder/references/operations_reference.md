# Operations Reference

> SKILL.md detail: flow matrix, DLC-portability, guardrails, workdir, config vars, scripts, pack-export. Placeholders `${APP_ROOT}`/`${WORKSPACE}`/`<python_x64_venv>`/`<python_arm64_venv>` from `${APP_ROOT}\data\config\qairt_env.json`.

---

## Flow Selection Guide

| Criteria | Flow A — DLC->bin (default) | Flow B — SNPE | Flow C — DLL->bin (legacy) |
|---|---|---|---|
| Output | `.dlc`->`.bin` | `.dlc` | `.bin`+`.cpp`+`.so`/`.dll` |
| Runtimes | HTP | DSP, CPU, GPU | HTP, CPU, GPU |
| Context binary | Required (from DLC) | Optional | Required (Win ARM)/Optional (Linux) |
| Quantization | FP32, FP16, **bf16**, W4A8, W4A16, W8A8, W8A8B8, W8A16, **W16A16** | FP16, FP32, INT8, A16W8 | FP16, FP32, INT8, A16W8, W4A16, W4A8, W8A8B8 |
| Target | WoS ARM64, ARM Linux, cross-SoC | Android, Embedded Linux | WoS ARM64 (legacy DLL) |
| Converter | `qairt-converter`+`qairt-quantizer`+`qnn-context-binary-generator` | `qairt-converter` | `qnn-onnx-converter` |
| Script | **`run_pipeline.py`** | `qai_convert_snpe.py` | `run_pipeline_legacy.py` |
| Advantage | **Default.** No VS ARM64. Full CLE/per-channel/bf16/w16a16/cross-SoC. | Simplest | Only ARM64 `.dll` path |

**Defaults:** WoS ARM64->**A** (C if DLL needed); ARM Linux->**A**; cross-device v73+v81->**A** (omit `--soc_optimized`); Android/DSP->**B**; x86 Linux CPU->**A**; validate without .bin->DLC via `QNNContext` (`--skip_contextbin`); uncertain->**A**.

---

## DLC Portability Question

Ask via `question` tool:

> "Should the DLC be **portable across HTP platforms** (v73 X Elite + v81 X2 Elite), or **optimised for a specific SoC**?"
> - **Option 1 (default):** Cross-platform — works v73+v81. `.bin` still per SoC.
> - **Option 2:** SoC-optimised — one HTP; ask which (v73=X Elite/SC8380XP; v81=X2 Elite/SM8750).

**Skip** when user signals: "optimise for this SoC"/"specific to X Elite"/"for X2 Elite only"->Opt 2; "portable"/"cross-SoC"/"all devices"->Opt 1; ambiguous->**ask**.

**CLI:** cross-platform=omit `--soc_optimized`; SoC-optimised=`--soc_optimized --htp_version v73|v81`.

**Failure-safe:** defaults cross-platform. `--htp_version` alone selects which `.bin`. Cross-platform DLC+`--htp_version v73` valid for X Elite.

---

## Guardrails — Full Detail

- **Timeout:** `timeout=0` (no limit) for ALL conversion/quantization. Ref->`references/win_qairt_setup.md § Reference Times`.
- **Inference:** only `qai_runner.py`/`qai_appbuilder`/`QNNContext`; `onnxruntime` only `CPUExecutionProvider`. Never `qnn-net-run` directly.
- **Benign HTP errors (non-fatal, never degrade to CPU):**
  - `setPowerConfig error 0x32c9` — BURST denied; run admin or `performance_profile: "default"`.
  - `Error 0x200: failed to close queue` — teardown timing; result written.
  - `m_CFBCallbackInfoObj is not initialized` — v81 callback init.
  - `DSP_INFO UNSUPPORTED_KEY: 49/50` — suppress `2>$null`.
- > ⚠️ NEVER `os._exit()` before destroying all `QNNContext` -> crash `0xC0000409`. `del` contexts first.
- **No PowerShell vars** (`$_`, `$env:`, `!`) in bash — use `.ps1 -File` or `glob.glob()`.
- **Encoding:** scripts set `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`. New scripts: same after `import sys`. Never `set PYTHONUTF8=1 && ...` (trailing-space crash).
- **Escalation:** conversion fails after export/patch/retry -> record error+logs+ONNX -> escalate. B3/B4/B7->`references/operator_patching.md`.
- **Dynamic-input ONNX:** explicit shapes — `qnn_conversion.md` (`--input-dim`) or `snpe_conversion.md` (`--source-model-input-shape`).
- **No deriving from artifacts** (`.bin`/`.cpp`/`_net.json`/`.so`/`.dll`/calib): each stage fresh.
- **No browsing QAIRT SDK source**; documented CLI/Python APIs only.
- **No hardcoded Doxygen HTML** (hash per build); use `<QAIRT_SDK_ROOT>\include\QNN\QnnTypes.h`.
- **Version annotations** historical — 2.x backward compatible. Truth: `qairt_env.json` `_version`. Use `%QAIRT_SDK_ROOT%` in `.bat`. (Exception: AI Hub prebuilt `.bin` bound to compile version — mismatch->`Error 5000`->belongs to `model-hub`.)

---

## Working Directory Convention (STRICTLY ENFORCED)

`${WORKSPACE}` = workspace root (default `C:\WoS_AI`). All artifacts under `${WORKSPACE}\<model>\`.

| Purpose | Path (`${WORKSPACE}\<model>\` prefix) |
|---|---|
| Project root | `.\` |
| ONNX | `<model>.onnx` |
| Output | `output\` |
| Calibration | `calib\` |
| Logs | `.\` |

**FORBIDDEN:** paths with `QAIModelBuilder`; user home/Downloads; CWD outside `${WORKSPACE}\`.

> ⚠️ SELF-CHECK: destination must start with `${WORKSPACE}\<model_name>\`. If not->STOP and fix.

**Bootstrap:**
```bat
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_workspace_init.py <model_name>
```
Renames existing->`<model_name>_bak_<ts>` (exits 1 on failure); creates `output/calib/`; copies `assets/plan.md` with `START_TIME`. `--no-templates` skips plan. Never reuse dir without this. Pipeline auto-places intermediates under `--output-root`.

> ⚠️ Do NOT substitute `mkdir`—diagnose first. Success=stdout `[OK] Workspace initialized: <path>`, NOT exit code. False failures: `'#' is not recognized...` (comment leaked); `path not found` (unsubstituted placeholders—read `qairt_env.json`); exit 1 no traceback (comment masking—check stdout).

---

## Project Configuration Variables (`plan.md`)

| Variable | Purpose | Example |
|---|---|---|
| `MODEL_NAME` | Model ID | `inception_v3` |
| `FLOW` | Framework | `QNN`/`SNPE` |
| `PRECISION` | Precision | `FP16`/`INT8`/`A16W8` |
| `HOST_DEVICE`/`TARGET_DEVICE` | Build/infer machine | `ARM WIN` |
| `HOST_ARCH`/`TARGET_ARCH` | Toolchain/target | `x86_64-windows-msvc`/`windows-aarch64` |
| `OUTPUT_DIR` | Output path | `${WORKSPACE}\inception_v3\output` |
| `CALIBRATION_DATA` | Calib source | image/raw folder (INT/A16W8 only) |
| `RETMOE_DEVICE_INFO` | Remote SSH | (optional) |
| `MODE` | Exec mode | `batch`(default)/`interactive` |
| `HOST_OS` | Platform (auto) | `windows-arm64`/`linux-x64`/`linux-aarch64` |
| `ADB_DEVICE_ID` | ADB serial | `8347dcb1` (multi-device, linux-x64) |
| `ADB_DEVICE_OS` | Board OS | `android`(default)/`linux` |
| `ADB_DSP_VERSION` | HTP/DSP | `v73`/`v75`/`v79`/`v81` |
| `ADB_TARGET_ARCH` | Arch override | `aarch64-android` (non-standard only) |

**Architecture derivation:**
- `ARM WIN`/`windows-arm64` -> HOST `x86_64-windows-msvc`, TARGET `windows-aarch64`, SHELL powershell
- `X86 LINUX`/`linux-x64` -> HOST `x86_64-linux-clang`, TARGET `aarch64-oe-linux-gcc11.2` (cross via ADB), SHELL bash
- `ARM LINUX`/`linux-aarch64` -> HOST/TARGET both `aarch64-oe-linux-gcc11.2`, SHELL bash

---

## Script Index (under `${APP_ROOT}/factory/chat_features/model-builder/scripts/`)

> Do NOT read script source—docs are authoritative. Read only if behavior absent from all docs.

| Script | Purpose | venv | Doc |
|---|---|---|---|
| `qai_runner.py` | ONNX/QNN loader | ARM64 | `inference.md § qai_runner.py` |
| `qai_workspace_init.py` | Init workspace | x64 | this doc § Working Directory |
| `qai_inspect_onnxio.py` | ONNX I/O inspect | x64 | `core_workflow.md § Step 2` |
| `qai_convert_fp.py` | **Flow C** FP (`qnn-onnx-converter`) | x64 | `qnn_conversion.md` (float) |
| `qai_convert_int.py` | **Flow C** quant (INT8/A16W8/A8W8B8) | x64 | `qnn_conversion.md` (quant) |
| `qai_convert_snpe.py` | SNPE conversion | x64 | `snpe_conversion.md` |
| `qai_dev_gen_contextbin.py` | Context binary `.dll`/`.dlc` (`--auto-config`) | x64 | `context_binary.md`; CLE->`model_quantization.md` |
| `run_pipeline.py` | **Flow A** ONNX->DLC->.bin. CLE/bf16/w16a16. `--soc_optimized`. | x64 | `core_workflow.md § Step 4` & `qnn_conversion.md` |
| `run_pipeline_legacy.py` | **Flow C** ONNX->DLL->.bin (VS ARM64) | x64 | `core_workflow.md § Step 4` (legacy) |
| `model_config.json` | Multi-precision config | — | `qnn_conversion.md § End-to-End Pipeline` |
| `adb_runner.py` | ADB deploy+run | x64 | `core_workflow.md § Step 7B`; `adb_execution.md` |
| `qai_pack_export.py` | App Builder Pack export | x64 | `pack_export.md` |

**Inference templates** (`scripts/inference/`) — start with `infer_generic.py`. Ref: `inference.md § Inference Script Templates`.

| Template | Type | Notes |
|---|---|---|
| `infer_generic.py` | Any | I/O shapes, raw output |
| `infer_classify.py` | Classification | Softmax+Top-K |
| `infer_detect.py` | Detection | YOLO/SSD, NMS |
| `infer_segment.py` | Segmentation | Mask overlay |
| `infer_sr.py` | Super-resolution | Auto NCHW/NHWC |

---

## Pack Export (Phase 7 · App Builder Integration)

After Phase 6 (cosine OK, `REPORT.md` has `END_TIME`, `inference_manifest.json` exists):

```bat
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_export.py ^
  --workdir ${WORKSPACE}\{MODEL_NAME} ^
  --model-name {MODEL_NAME} ^
  --precision {PRECISION}
```
Other params from `inference_manifest.json`. Full->`references/pack_export.md § 2`.

---

## Remote Device Execution (Optional)

When `RETMOE_DEVICE_INFO` set, context-binary/inference/validation run remote via SSH. ① `MODE=batch`+`RETMOE_DEVICE_INFO`->MUST complete remote deploy+inference+logs; ② unreachable->Blocking B5 (stop, ask); ③ absolute paths on remote. Format->`references/remote_execution.md`.
