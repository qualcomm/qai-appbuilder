# Core Workflow — Step Details

> Per-step detail for the 8-step Core Workflow. SKILL.md keeps the spine + pointers here.
> Placeholders `${APP_ROOT}`/`${WORKSPACE}`/`<python_x64_venv>`/`<python_arm64_venv>` from `${APP_ROOT}\data\config\qairt_env.json`. Never hardcode.

---

## Host OS Detection (MANDATORY — run once before Step 1, write result to plan.md)

```bash
python3 -c "
import platform, sys
m = platform.machine()
print('windows-arm64' if sys.platform == 'win32' else
      'linux-aarch64' if m in ('aarch64', 'arm64') else
      'linux-x64'     if m in ('x86_64', 'amd64') else 'unknown')
"
```

| `HOST_OS` | Platform | Step 7 path |
|-----------|----------|-------------|
| `windows-arm64` | Windows Snapdragon ARM64 | **Path A** — `qai_runner.py`/`qai_appbuilder` |
| `linux-aarch64` | Linux aarch64 (HTP on-device) | **Path A** — `qai_runner.py`+`QnnRunner` |
| `linux-x64` | Linux x86_64 (cross-compile, no local HTP) | **Path B** — `adb_runner.py` → aarch64 board |

If `unknown` → **stop and ask user**. Write `HOST_OS = <value>` in plan.md Project Config block.

---

## Step 1 — Export source model to ONNX

- Export with `python_x64_venv` (x86_64 3.10); always `model.eval()` first.
- **Disable training-only branches** (`aux_logits`, dropout, `if self.training:`) — ops QAIRT 2.45 can't convert. Fix: `model.aux_logits = False; model.AuxLogits = None`. Details → `references/model_export_validation.md`.
- **Rule 5 — FP32 ONNX only (never FP16).** `qnn-onnx-converter` expects FP32; CPU lacks FP16 `Conv2d`. FP16 via `--precision 16` later.
- **Rule 7 — `opset_version=18` always.** torch 2.x; min opset 18. Also `pip install onnxscript`.

Full rules (1–4, 6, 8), benchmarks, export template → `references/model_export_validation.md`.

## Step 2 — Inspect ONNX I/O and operator compatibility

```
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_inspect_onnxio.py ${WORKSPACE}\<model_name>\<model_name>.onnx
```

> ⚠️ Do NOT gate on dry-run. `--dry_run` warnings are often false positives. Proceed to Step 4; return to Step 3 only if conversion hard-fails.

## Step 3 — Operator Patching (only if conversion hits a hard op error)

Replace unsupported ops (Einsum/GridSample/ScatterND/Mod/Floor…) with QNN-compatible equivalents **in-memory**, then re-run conversion. After **every** patch: validate ONNX (`onnx.checker`) → convert (not dry-run) → cosine ≥ 0.95 vs baseline. Track in `plan.md`.

> Decision tree, per-op Error→Action table, validation gates → `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/operator-patching/SKILL.md`. Non-op errors → `conversion-troubleshooting`.

## Step 4 — Convert float model

> ⚠️ **Wrapper scripts only:** `run_pipeline.py` (default, ONNX→DLC→.bin) or `run_pipeline_legacy.py` (DLL Flow C only). **Avoid** direct `qairt-converter`/`qnn-onnx-converter`/`qnn-model-lib-generator` — wrappers handle `--preserve_io`, layout, `PYTHONPATH`, VS env. Bypassing risks silent BGR/NHWC errors.

**HTP version** (`--htp_version`):

| User says | flag | Notes |
|-----------|------|-------|
| "v73" / default | `v73` | X Elite 8380 |
| "v81" | `v81` | X2 Elite 8480 |

> **Detect HTP** (when unspecified) — registry query (NOT `Get-WmiObject`/`Get-PnpDeviceProperty` — hang):
> ```powershell
> Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
>   Where-Object { $_.PSChildName -like "qcadsp*" } |
>   Get-ItemProperty | Select-Object PSChildName, ImagePath
> ```
> INF filename: `8380`→`v73`; `8480`→`v81`. Table → `references/win_qairt_setup.md § Platform SoC Identification`. Default `v73`.

```bat
REM FP16, HTP v73, DLC (default)
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
  --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
  --output ${WORKSPACE}\<model_name>\output ^
  --precision fp16

REM Variants: `--htp_version v81`; `--precision fp32`;
REM SoC-optimised: `--htp_version v73 --soc_optimized`
```

> `run_pipeline.py` reads `qairt_env.json`, chains `qairt-converter`→`qairt-quantizer`→`qai_dev_gen_contextbin.py`, copies HTP runtime. No VS ARM64 env needed.

**Output:** `{model_stem}_{precision}.bin` (e.g. `inception_v3_fp16.bin`).

Full args (`--precision`/`--act_bw`/`--weight_bw`/`--bias_bw`/`--input_dim`/`--config`/`--htp_version`/`--skip_contextbin`/`--no_simplification`; DLC: `--cle`/`--per_channel`/`--per_row`/`--dump_encoding`/`--calib_method`/`--soc_optimized`/`--strip_quant`/`--io_config`/`--quant_overrides`) → `references/qnn_conversion.md § End-to-End Pipeline`. Legacy → `run_pipeline_legacy.py`.

> ⚠️ **Ubuntu:** `run_pipeline.py` is Windows-only. Use `qai_dev_gen_contextbin_x86.py`. Dirs: `linux-x64`→`$QAIRT_SDK_ROOT/bin/x86_64-linux-clang/`; `linux-aarch64`→`$QAIRT_SDK_ROOT/bin/aarch64-oe-linux-gcc11.2/`. Python: `python3_venv` (3.12).

## Step 5 — Optional: Quantization (INT8/A16W8/A8W8B8)

**Pre-quantization checklist (MANDATORY):**
- Verify `CALIBRATION_DATA` source exists.
- **No calib data → ask user** — 3 options: (1) user provides; (2) Agent auto-prepares (project `samples\images\`→workspace→user path→web→synthetic); (3) synthetic random (low accuracy). ⚠️ Scan listed dirs only, sample-count cap, never whole-disk glob.
- Images → float32 `.raw` matching model input shape. Validate shape.
- Generate `CALIB_LIST` (one `.raw` path per line). 50–200 representative samples.
- ⚠️ Must be REAL multi-class/multi-scene data. Strategies → `references/model_quantization.md § Calibration Data Acquisition`.

```bat
REM W8A8 (swap --precision w8a16 for W8A16)
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\run_pipeline.py ^
  --model ${WORKSPACE}\<model_name>\<model_name>.onnx ^
  --output ${WORKSPACE}\<model_name>\output ^
  --precision w8a8 ^
  --calib_list ${WORKSPACE}\<model_name>\calib\calibration_list.txt
```

Calib list format (**raw paths, one per line — no `input:=` prefix**; legacy prefix not accepted by `qairt-quantizer`):
```
${WORKSPACE}\<model_name>\calib\sample_0001.raw
${WORKSPACE}\<model_name>\calib\sample_0002.raw
```

Details → `references/model_quantization.md`.

## Step 6 — Context binary generation

- **`.bin` vs `.dlc`:** same machine→`.bin` (fastest); cross-device→`.dlc` (JIT on first load); user picks→obey. Decision → `references/inference.md § Format selection`.
- `run_pipeline.py` generates `.bin` automatically (`qai_dev_gen_contextbin.py --model <file>.dlc`). No extra command.
- **Legacy DLL→bin (Flow C):** `run_pipeline_legacy.py` or `qai_dev_gen_contextbin.py --model <file>.dll`. For DLC auto-invokes `--model QnnModelDlc.dll --dlc_path <file>.dlc --soc_model <id>`, copies runtime DLLs, maps v73→60, v81→88. **Do NOT** pass `.dlc` to `--model` (LoadLibrary fails).
- **Without bin:** `QNNContext` loads `.dlc` directly (identical numerics, ~21–27% slower).
- Manual steps, backend config → `references/context_binary.md`.

> ⚠️ **Ubuntu x64:** skip Step 6 for accuracy-only — push `.dlc` via `adb_runner.py` (auto-pushes `libQnnModelDlc.so`; board JIT-compiles). Not for perf.

## Step 7 — Inference + validation

**Router:** `windows-arm64`→Path A; `linux-aarch64`→Path A; `linux-x64`→Path B.

### Path A — Local Execution (`windows-arm64` / `linux-aarch64`)

> ⚠️ **Wrapper only:** `qai_runner.py` or `qai_appbuilder`. **NEVER `qnn-net-run`** — bypasses tensor handling/NCHW/post-processing.

- `python_arm64_venv` (ARM64 3.13).
- **CRITICAL:** `--preserve_io` keeps **NCHW** input. Always check `model.getInputShapes()`. Wrong format → wrong results. Templates → `references/inference.md`.

**MANDATORY — Save inference script** at `${WORKSPACE}\{MODEL_NAME}\infer_{MODEL_NAME}.py` (for `qai_pack_export.py`): self-contained (`qai_appbuilder`/`QNNContext` NPU + `onnxruntime` CPU baseline); Pre/Infer/Post blocks; comments on shapes; based on `scripts/inference/{infer_classify,infer_detect,infer_sr,infer_segment}.py`.

**MANDATORY — Generate `inference_manifest.json`** at `${WORKSPACE}\{MODEL_NAME}\inference_manifest.json` (for `qai_pack_export.py`; without it runner gets wrong dims). Records `model_name`/`precision`/`inference_script`/`context_binary`/`vendor`, `input`(`shape`/`format`/`dtype`/`preprocessing`), `output`, `assets[]`.

> `output.type` decides template: `"classification"`→softmax+Top-K; `"detection"`→NMS+boxes; `"super_resolution"`→upscale+tiling; `"segmentation"`→argmax+colorize; `"text"`/`"audio"`/`"raw"`→passthrough. Allowed: `classification`|`super_resolution`|`detection`|`segmentation`|`text`|`audio`|`raw`.

Full JSON + fields → `references/pack_export.md § 1`.

### Path B — ADB Deploy + On-Device Inference (`linux-x64` only)

Push model + QNN runtime to aarch64 board via `adb_runner.py`.

**Blocking Conditions:** B-ADB-01 `which adb` fails → install `android-tools-adb`; B-ADB-02 no device → check USB/TCP; B-ADB-03 multiple devices, no `ADB_DEVICE_ID` → ask; B-ADB-04 no `.bin`/`.dlc` → complete prior steps; B-ADB-05 input `.raw` missing → prepare.

```bash
python3 ${APP_ROOT}/factory/chat_features/model-builder/scripts/adb_runner.py \
  --model      ${OUTPUT_DIR}/${MODEL_NAME}_${PRECISION}.bin \
  --inputs     ${WORKSPACE}/${MODEL_NAME}/calib/<input_0>.raw \
  --output_dir ${WORKSPACE}/${MODEL_NAME}/output/adb_out \
  --sdk_root   $QAIRT_SDK_ROOT --backend htp \
  --device_os  ${ADB_DEVICE_OS:-android} --dsp_version ${ADB_DSP_VERSION:-v73} \
  [--device_id ${ADB_DEVICE_ID}]
```
For `.dlc` (accuracy-only): pass as `--model`; maps to `--model libQnnModelDlc.so --dlc_path` form.

Auto-pushes `qnn-net-run`, `libQnnHtp.so`/`libQnnSystem.so`/stubs/skel, model+input; generates `input_list.txt`; runs `adb shell`; pulls `Result_*/*.raw` back.

**Do NOT:** run `qnn-net-run` on x64 host; assume device has it (push from SDK); proceed past Blocking Conditions; pass `.dlc` to `qnn-net-run` directly.

Full ADB setup, SoC lib table → `references/adb_execution.md`.

## Step 8 — Validation report (Phase 6 — MANDATORY after successful inference)

- **Batch mode** — do NOT stop after inference succeeds.
- **ONNX baseline comparison (MANDATORY):**
  1. Run ONNX on same input — `onnxruntime` with **`CPUExecutionProvider` only**.
  2. Reuse Step 7 QNN output. Path A: local. Path B: `np.fromfile(path, dtype=np.float32)` from `Result_*/*.raw`.
  3. Cosine similarity:
     ```python
     cosine = np.dot(onnx_out.flatten(), qnn_out.flatten()) / (np.linalg.norm(onnx_out) * np.linalg.norm(qnn_out))
     ```
  4. Threshold: ≥0.99 (FP16/FP32) or ≥0.95 (INT8/A16W8).
  5. **cosine < threshold → B6.** No auto-fix. Zero-cost diagnosis → STOP → present options (calib diversity/CLE/W8A16/keep FP16/accept).

  > B6 diagnosis, fixes, channel-collapse trap → `${APP_ROOT}/factory/chat_features/model-opt/quantization/accuracy/SKILL.md`.

- **Task metrics** (Top-1/mAP/PSNR·SSIM/WER/BLEU/mIoU; cold·p50·p95·throughput·peak-mem; ≥3 inputs) → `references/expected_output_artifacts.md § Validation Report`.

- **Write `REPORT.md`** in project workdir:
  - **Cosine Summary (MANDATORY FORMAT):** one line per variant:
    ```
    Cosine Similarity (ONNX vs <variant>): <value>
    ```
    Example:
    ```markdown
    ## Cosine Similarity Summary

    Cosine Similarity (ONNX vs FP16): 0.999988
    Cosine Similarity (ONNX vs W8A8): 0.934705
    ```
    Rules: (1) literal prefix `Cosine Similarity (ONNX vs `; (2) `<variant>` ∈ `FP16`|`FP32`|`INT8`|`W8A8`|`W8A16`|`W4A16`|`W4A8`|`W8A8B8`|`A16W8`; (3) decimal value; (4) **always include FP16** — `qai_pack_export.py` reads it; (5) table fallback OK but plain-text avoids warnings. Missing → "Model accuracy validation not passed".
  - Pass/fail verdict; top predictions or sample outputs.

- **Model workspace path (MANDATORY):** print `${WORKSPACE}\<model_name>` in final summary every turn.

- **Update `plan.md` (MANDATORY):** each phase → Progress Summary (⬜→✅); Phase 6 end → fill `END_TIME`+`WORK_TIME`, mark Done.

## Expected Output Artifacts

Per-Flow (A/B/C) artifact list → `references/expected_output_artifacts.md`. Checklist: confirm `.onnx`/`.dlc`/`.bin`/`infer_<model>.py`/`REPORT.md` per phase.
