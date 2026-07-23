# SNPE Conversion Reference

## Scope
Use this reference when `{FLOW}=SNPE` and output target is `.dlc`.

> **Quantization**: this reference covers SNPE/DLC conversion and its DLC quantization path. For the general quantization guide (INT8/W8A8/W8A16, CLE, calibration data), see `references/model_quantization.md`.

## Host toolchain
- Windows x86: `x86_64-windows-msvc`
- windows arm : `x86_64-windows-msvc` ,emulation mode
- Linux x86: `x86_64-linux-clang`

## Method A: wrapper script
```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_convert_snpe.py \
  --input model.onnx \
  --output snpe_output/model.dlc \
  --arch x86_64-linux-clang \
  --bitwidth 16
```

## Method B: direct converter
```bash
python ${QAIRT_SDK_ROOT}/bin/HOST_TOOLCHAIN/qairt-converter \
  --input_network model.onnx \
  --output_path snpe_output/model.dlc \
  --float_bitwidth 16
```

> **Troubleshooting**: If conversion fails with "Unsupported operator" errors, see [In-Memory Operator Patching](operator_patching.md) for patching guidance.

## Dynamic input ONNX (required shape override)
If ONNX has dynamic inputs, pass explicit shapes.

Wrapper:
```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_convert_snpe.py \
  --input model.onnx \
  --output snpe_output/model.dlc \
  --source-model-input-shape images 1,3,640,640
```

Direct converter:
```bash
python ${QAIRT_SDK_ROOT}/bin/HOST_TOOLCHAIN/qairt-converter \
  --input_network model.onnx \
  --output_path snpe_output/model.dlc \
  --float_bitwidth 16 \
  --source_model_input_shape images 1,3,640,640
```

Failure signature:
- `Missing command line inputs for dynamic inputs [...]`

## Dry run and inspection
Dry run:
```bash
${QAIRT_SDK_ROOT}/bin/HOST_TOOLCHAIN/qairt-converter \
  --input_network model.onnx --dry_run
```

Inspect DLC:
```bash
${QAIRT_SDK_ROOT}/bin/HOST_TOOLCHAIN/snpe-dlc-info -i snpe_output/model.dlc
```

## Outputs
- `model.dlc`
- conversion logs
- dry-run report

---

## ONNX → DLC Quantization (W8A8 / W8A16) via qairt-quantizer

`qairt-converter` **does NOT support inline quantization** (no `--input_list`, `--act_bw`, `--weight_bw` parameters).
Quantization requires a separate two-step process:

**Step 1 — Convert ONNX to FP32 DLC:**
```bat
REM %QAIRT_SDK_ROOT% is set by Setup.bat (or read from data\config\qairt_env.json
REM "qairt_sdk_root"). Do NOT hardcode a versioned path here.
set QAIRT=%QAIRT_SDK_ROOT%
set PYEX=<python_x64_venv>\Scripts\python.exe
set PYTHONPATH=%QAIRT%\lib\python

%PYEX% %QAIRT%\bin\x86_64-windows-msvc\qairt-converter ^
  --input_network ${WORKSPACE}\<model>\<model>.onnx ^
  --output_path ${WORKSPACE}\<model>\output_dlc\<model>_fp32.dlc
```

**Step 2 — Quantize FP32 DLC to W8A8 via `qairt-quantizer`:**
```bat
%PYEX% %QAIRT%\bin\x86_64-windows-msvc\qairt-quantizer ^
  --input_dlc ${WORKSPACE}\<model>\output_dlc\<model>_fp32.dlc ^
  --output_dlc ${WORKSPACE}\<model>\output_dlc\<model>_w8a8.dlc ^
  --input_list ${WORKSPACE}\<model>\calib\calib_list_plain.txt ^
  --param_quantizer tf ^
  --act_quantizer tf ^
  --act_bitwidth 8 ^
  --weights_bitwidth 8
```

For W8A16: use `--act_bitwidth 16 --weights_bitwidth 8`.

> ⚠️ **Calibration list format — `qairt-quantizer` ONLY**: plain file paths (one per line), **without** the `input:=` prefix.
> The `input:=` prefix is a *different tool's* format, used by `qnn-onnx-converter` / `qai_convert_int.py` (Flow C) — see `references/model_quantization.md` for that path. These two formats are genuinely different; do not mix them.
>
> ✅ Correct (`qairt-quantizer`):
> ```
> ${WORKSPACE}\model\calib\calib_0000.raw
> ${WORKSPACE}\model\calib\calib_0001.raw
> ```
>
> ❌ Wrong for `qairt-quantizer` (but correct for `qnn-onnx-converter`):
> ```
> input:=${WORKSPACE}\model\calib\calib_0000.raw
> ```

**`qairt-quantizer` key parameters:**

| Parameter | Description |
|-----------|-------------|
| `--input_dlc` | Input FP32 DLC file |
| `--output_dlc` | Output quantized DLC file |
| `--input_list` | Plain-path calibration list (no `input:=` prefix) |
| `--param_quantizer` | Weight quantizer: `tf` (default, min/max) or `enhanced` |
| `--act_quantizer` | Activation quantizer: `tf` (default) or `enhanced` |
| `--act_bitwidth` | Activation bitwidth: `8` or `16` |
| `--weights_bitwidth` | Weight bitwidth: `8` |

**Tool location:** `bin/x86_64-windows-msvc/qairt-quantizer` (runs under x86 emulation on WoS ARM64).
