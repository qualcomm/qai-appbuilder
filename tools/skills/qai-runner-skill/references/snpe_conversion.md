# SNPE Conversion Reference

## Scope
Use this reference when `{FLOW}=SNPE` and output target is `.dlc`.

## Precision note
- `--float_bitwidth 16` should be treated as the converter's 16-bit floating-point mode, not as a guarantee that a BF16 ONNX model is accepted end-to-end.
- If the source ONNX was exported in BF16, validate that the SNPE/QAIRT converter accepts that graph and produces correct results.
- For maximum compatibility, prefer exporting ONNX in FP32 and let the converter handle the target float precision.

## Host toolchain
- Windows x86: `x86_64-windows-msvc`
- windows arm : `x86_64-windows-msvc` ,emulation mode
- Linux x86: `x86_64-linux-clang`

## Method A: wrapper script
```bash
python skills/aipc-toolkit/scripts/aipc_convert_snpe.py \
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
python skills/aipc-toolkit/scripts/aipc_convert_snpe.py \
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
