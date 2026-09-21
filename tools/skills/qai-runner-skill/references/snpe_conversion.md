# SNPE Conversion Reference

## Scope
Use this reference when `{FLOW}=SNPE` and output target is `.dlc`.

> **Quantization**: this reference covers SNPE/DLC conversion and its DLC quantization path. For the general quantization guide (INT8/W8A8/W8A16, CLE, calibration data), see `references/model_quantization.md`.

## Precision note
- `--float_bitwidth 16` should be treated as the converter's 16-bit floating-point mode, not as a guarantee that a BF16 ONNX model is accepted end-to-end.
- If the source ONNX was exported in BF16, validate that the SNPE/QAIRT converter accepts that graph and produces correct results.
- For maximum compatibility, prefer exporting ONNX in FP32 and let the converter handle the target float precision.

## Host toolchain
`--arch` selects the QAIRT converter host toolchain. It must match the system where
`qairt-converter` is executed, not the target device architecture:

| Host system | Host architecture | `--arch` value |
|---|---|---|
| Windows x86_64 | x86_64 | `x86_64-windows-msvc` |
| Windows ARM64 | ARM64 / ARM64X SDK | `arm64x-windows-msvc` |
| Linux x86_64 | x86_64 | `x86_64-linux-clang` |
| Linux ARM64 | aarch64 | `aarch64-oe-linux-gcc11.2` |

The wrapper auto-detects these values when `--arch` is omitted. Use an explicit value
only when the active Python process or SDK layout requires it. macOS is not a supported
host platform for this wrapper; do not use `x86_64-linux-clang` merely as a macOS fallback.


## Tool resolution
Use the wrapper when possible; it auto-detects the correct SDK tool path.
For direct SDK commands, first check the current system and QAIRT config, put the
matching SDK `bin` directory on `PATH`, then verify the tool resolves. Do not
hardcode a platform-specific SDK subdirectory in this reference.


## Method A: wrapper script
```bash
python scripts/qai_convert_qairt.py \
  --input model.onnx \
  --output snpe_output/model.dlc \
  --bitwidth 16
```

For an explicit Linux x86_64 host, add `--arch x86_64-linux-clang`. For Windows or
Linux ARM64, use the corresponding value from the table above.

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
python scripts/qai_convert_qairt.py \
  --input model.onnx \
  --output snpe_output/model.dlc \
  --source-model-input-shape images 1,3,640,640
```

Direct converter:
```bash
qairt-converter \
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
qairt-converter \
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

## ONNX -> DLC Quantization (W8A8 / W8A16) via qairt-quantizer

`qairt-converter` **does NOT support inline quantization** (no `--input_list`, `--act_bw`, `--weight_bw` parameters).
Quantization requires a separate two-step process:

**Step 1 — Convert ONNX to FP32 DLC:**
```bash
qairt-converter \
  --input_network ${WORKSPACE}/<model>/<model>.onnx \
  --output_path ${WORKSPACE}/<model>/output_dlc/<model>_fp32.dlc
```

**Step 2 — Quantize FP32 DLC to W8A8 via `qairt-quantizer`:**
```bash
qairt-quantizer \
  --input_dlc "${WORKSPACE}/<model>/output_dlc/<model>_fp32.dlc" \
  --output_dlc "${WORKSPACE}/<model>/output_dlc/<model>_w8a8.dlc" \
  --input_list "${WORKSPACE}/<model>/calib/calib_list_plain.txt" \
  --param_quantizer tf \
  --act_quantizer tf \
  --act_bitwidth 8 \
  --weights_bitwidth 8
```

For W8A16: use `--act_bitwidth 16 --weights_bitwidth 8`.

### Calibration list format (single-input vs multi-input)

**Single-input model** — plain paths, one per line (simplest, recommended):
```
${WORKSPACE}\model\calib\calib_0000.raw
${WORKSPACE}\model\calib\calib_0001.raw
```

**Multi-input model** — one line = one calibration sample, all of that sample's
inputs on the SAME line. Two forms, both accepted by `qairt-quantizer`:

| Form | Syntax | When to use |
|---|---|---|
| Positional | `path_a path_b path_c` (space-separated) | ≤3 inputs and you are certain of graph input order |
| **Named (preferred)** | `name_a:=path_a name_b:=path_b` | **Always for >3 inputs** — binding is explicit, immune to I/O reordering |

> 📖 SDK authority: `docs/QAIRT-Docs/QNN/general/tools.html` § `qairt-quantizer --input_list`
> ("Multiple files per line separated by spaces indicate multiple inputs") and
> § `qnn-net-run --input_list` (canonical format definition, with a worked
> `Input_1:=... Input_2:=...` multi-input example). `SNPE/general/qairt_quantizer.html`
> repeats the `<name>:=<path>[<space><name>:=<path>]` grammar.

> ⚠️ **`:=` is valid for `qairt-quantizer`, not just Flow C.** `run_pipeline.py` prints a
> `[WARN]` when it sees `:=`; for a multi-input model that warning is a false positive
> (non-blocking — conversion proceeds). Do not "fix" the list by stripping the names.

> ⚠️ **Never use positional form for a many-input graph.** QNN reorders I/O, so
> "column N = input N" is not guaranteed. A mis-ordered list does not error — it
> silently binds calibration data to the wrong tensor, and the accuracy loss is then
> misattributed to quantization itself. Get names from `qai_inspect_onnxio.py` and
> cross-check with `--dump_encoding`.

**`qairt-quantizer` key parameters:**

| Parameter | Description |
|-----------|-------------|
| `--input_dlc` | Input FP32 DLC file |
| `--output_dlc` | Output quantized DLC file |
| `--input_list` | Calibration list; multi-input → all inputs of one sample on one line (see above) |
| `--param_quantizer` | Weight quantizer: `tf` (default, min/max) or `enhanced` |
| `--act_quantizer` | Activation quantizer: `tf` (default) or `enhanced` |
| `--act_bitwidth` | Activation bitwidth: `8` or `16` |
| `--weights_bitwidth` | Weight bitwidth: `8` |

**Tool location:** `bin/x86_64-windows-msvc/qairt-quantizer` — native on `windows-x64`; runs under Prism x86 emulation on `windows-arm64`.

### Legacy fallback: snpe-dlc-quant

`snpe-dlc-quant` is the older SNPE DLC quantization tool. Do not select it for
new QAIRT workflows when `qairt-quantizer` is available. Use it only for an
older SDK that lacks `qairt-quantizer`, and confirm the supported flags with
that SDK's `snpe-dlc-quant --help`.

```bash
snpe-dlc-quant \
  --input_dlc model_fp32.dlc \
  --output_dlc model_quantized.dlc \
  --input_list calibration_list.txt \
  --enable_htp
```
