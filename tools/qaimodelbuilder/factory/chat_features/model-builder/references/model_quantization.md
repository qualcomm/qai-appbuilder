# SNPE/QNN Quantization Guide

## Overview
Convert ONNX models to quantized SNPE/QNN format (FP16/INT8/INT16) for Qualcomm AI accelerators.

## Prerequisites
- ONNX model file
- QAIRT SDK environment
- Calibration data (for INT model only)

## FP16 Conversion (No Calibration)

#### SNPE
Use `qairt-converter` to convert ONNX models to SNPE DLC format. This is a unified converter that supports both QNN and SNPE formats.


**Usage:**
Choose host toolchain from system.
For Windows, always use x86_64-windows-msvc.
For x86 Linux, use x86_64-linux-clang. ARM Linux cross-compilation is not supported.

```bash
python3 ${QAIRT_SDK_ROOT}/bin/HOST_TOOLCHAIN/qairt-converter \
    --input_network model.onnx \
    --output_path model.dlc \
    --float_bitwidth 16
```


#### QNN
```bash
python3 scripts/qai_convert_fp.py --input_network model.onnx
```

**Output:** `model.cpp`, `model.bin`, `test_libs_model_fp16_aarch64/`

## INT Quantization (INT8/INT16, Requires Calibration)

### Step 1: Prepare Calibration Data
Create `.raw` files (float32) using your model's preprocessing.

**Example preprocessing (adapt to your model):**
```python
import numpy as np
from PIL import Image

# TODO: Replace with your model's actual preprocessing
img = Image.open(path).convert('RGB').resize((640, 640))
arr = np.array(img).astype(np.float32) / 255.0

# Transpose if model expects CHW format (check model input layout)
# arr = np.transpose(arr, (2, 0, 1))  # HWC->CHW if needed

arr.tofile(f"calibration_raw/input_{i:04d}.raw")
```

### Step 2: Create Input List

> ⚠️ **Two calibration-list formats exist — they are a real tool difference, not interchangeable:**
> - **`qai_convert_int.py` (QNN, Flow C)** → **`input:=` prefix** (shown below).
> - **`qairt-quantizer` (SNPE / DLC, Flow A)** → **plain paths, one per line, no prefix** (see [snpe_conversion.md § ONNX → DLC Quantization](snpe_conversion.md#onnx--dlc-quantization-w8a8--w8a16-via-qairt-quantizer)).

`calibration_list.txt` for **`qai_convert_int.py` (Flow C, `input:=` prefix):**
```
input:=calibration_raw/input_0000.raw
input:=calibration_raw/input_0001.raw
...
```

### Step 3: Run Conversion
#### SNPE / DLC Quantization (two-step process)

`qairt-converter` does **not** support inline quantization. The flow is **Step 1**
ONNX → FP32 DLC (via `qairt-converter`), **Step 2** FP32 DLC → quantized DLC (via
`qairt-quantizer`, with a plain-path calibration list — no `input:=` prefix). Full
commands, parameter table, and calibration-list format → [snpe_conversion.md § ONNX → DLC Quantization](snpe_conversion.md#onnx--dlc-quantization-w8a8--w8a16-via-qairt-quantizer).

#### QNN
Use  `scripts/qai_convert_int.py `  as QNN_CONVERT_SCRIPT.
```bash
python3 [QNN_CONVERT_SCRIPT] \
    --input_network model.onnx \
    --input_list calibration_list.txt \
    --act_bw <activation_bitwidth> --weight_bw <weight_bitwidth>
```
act_bw 16/weight_bw 8 is suggested setting for vision model.
show
**Output:** `model_a{act_bw}_w{weight_bw}.*` (e.g., `model_a16_w8.cpp`, `model_a8_w8.cpp`)

## Tool Parameter Mapping (quantization args across the three converters)

Same quantization intent, three different flag spellings. Use this to translate between tools:

| Intent | `run_pipeline.py` (Flow A, default) | `qairt-quantizer` (Flow A backend) | `qai_convert_int.py` (Flow C) |
|--------|-------------------------------------|------------------------------------|-------------------------------|
| Precision (combined) | `--precision w8a8` \| `w8a16` \| `w8a8b8` \| `w4a8` \| `w4a16` \| `w16a16` \| `fp16` \| `bf16` \| `fp32` | — (set act/weight bitwidths individually) | — (set act/weight bitwidths individually) |
| Activation bitwidth | `--act_bw <4\|8\|16>` (with `--weight_bw`) | `--act_bitwidth <8\|16>` | `--act_bw <8\|16>` |
| Weight bitwidth | `--weight_bw <4\|8\|16>` (with `--act_bw`) | `--weights_bitwidth <4\|8>` | `--weight_bw <8>` |
| Bias bitwidth | `--bias_bw <8\|32>` | `--bias_bitwidth <8\|32>` | `--bias_bw <8>` |
| Calibration list | `--calib_list <file>` (**plain paths, no prefix**) | `--input_list <file>` (**plain paths, no prefix**) | `--input_list <file>` (**`input:=` prefix**) |
| Per-channel weights | `--per_channel` | `--use_per_channel_quantization` | — |
| CLE | `--cle` | `--algorithms cle` | — |
| Quantizer scheme | (internal `tf`) | `--param_quantizer tf --act_quantizer tf` | (internal `tf`) |

> ⚠️ **Two distinct spellings, same meaning:** `run_pipeline.py`/`qai_convert_int.py` use `--act_bw`/`--weight_bw`/`--bias_bw`; `qairt-quantizer` uses `--act_bitwidth`/`--weights_bitwidth`/`--bias_bitwidth` (note the `s` in `weights`). And the calibration-list format differs by tool (see § Step 2). Full `run_pipeline.py` flag reference → [qnn_conversion.md § run_pipeline.py — Full Argument Reference](qnn_conversion.md); `qairt-quantizer` full command → [snpe_conversion.md § ONNX → DLC Quantization](snpe_conversion.md#onnx--dlc-quantization-w8a8--w8a16-via-qairt-quantizer).

## Key Points
- **FP16:** Fast conversion, no calibration needed
- **INT8/INT16:** Better performance, requires calibration data
- **Calibration:** Use diverse training/validation samples
- **Preprocessing:** Must match inference exactly

## qairt-quantizer vs qnn-onnx-converter: Quantization Quality Comparison

> Verified on Inception-V3 (299x299, 20 calibration samples, WoS ARM64, QAIRT 2.45)

| Tool | Path | W8A8 Cosine vs ONNX | Notes |
|------|------|--------------------:|-------|
| `qnn-onnx-converter` | ONNX -> cpp/dll -> bin (**legacy Flow C**) | 0.9297 | Original QNN flow (`run_pipeline_legacy.py`) |
| `qairt-quantizer` | ONNX -> FP32 DLC -> W8A8 DLC -> bin (**default Flow A**) | **0.9351** | Slightly better; used by `run_pipeline.py` |

- Both tools use the same calibration data and `tf` quantizer scheme
- `qairt-quantizer` produces marginally higher cosine similarity in this test
- Difference is small (~0.005); both are within acceptable range for W8A8
- If cosine is below threshold, do NOT silently switch to CLE — first check calibration diversity (single-image augmentations are NOT diverse), then STOP and let the **user** choose a fix. CLE is one option among several (see SKILL.md B6). *CLE principle: equalizes per-channel weight ranges across adjacent layers to reduce quant error.*

## Improving W8A8 Accuracy: --algorithms cle

If W8A8 cosine vs ONNX is below 0.95, add `--algorithms cle` to `qairt-quantizer`:

```bat
python %QAIRT%\bin\x86_64-windows-msvc\qairt-quantizer ^
  --input_dlc output_dlc\model_fp32.dlc ^
  --output_dlc output_dlc\model_w8a8_cle.dlc ^
  --input_list calib\calib_list_plain.txt ^
  --param_quantizer tf ^
  --act_quantizer tf ^
  --act_bitwidth 8 ^
  --weights_bitwidth 8 ^
  --algorithms cle
```

`cle` (Cross Layer Equalization) rescales weights across adjacent layers to reduce quantization error.
It is especially effective for models with large weight variance (e.g. MobileNet, EfficientNet).

> 💡 **CLE is now built into `run_pipeline.py` — just add `--cle`.** The default Flow A pipeline (`ONNX → DLC → .bin`) invokes `qairt-quantizer --algorithms cle` internally when you pass `--cle`. Example (all-in-one):
>
> ```bat
> <python_x64_venv>\Scripts\python.exe scripts\run_pipeline.py ^
>   --model model.onnx --output output ^
>   --precision w8a8 --calib_list calib\calib_list_plain.txt ^
>   --cle --per_channel --dump_encoding
> ```
>
> The commands shown just above (running `qairt-quantizer` and `qai_dev_gen_contextbin.py` manually) are kept for debugging / per-step visibility. For normal use, `run_pipeline.py --cle` is preferred.

## FP32 DLC vs FP16 bin: Precision Note

`qairt-converter` always outputs **FP32 DLC** by default (even with `--float_bitwidth 16`,
the DLC stores weights in FP32 internally). The FP16 precision is applied at context binary
generation time by `qnn-context-binary-generator` (HTP backend automatically converts to FP16
during graph optimization). This means:

- `inception_v3_fp32.dlc` (48 MB DLC) -> `inception_v3_dlc_fp16.bin` (48 MB bin, FP16 weights in HTP)
- `inception_v3_w8a8.dlc` (quantized DLC) -> `inception_v3_dlc_w8a8.bin` (24 MB bin, INT8 weights)

You do NOT need to specify `--float_bitwidth 16` in `qairt-converter` when targeting FP16 bin output.

## Verification
```python
# Check calibration data (adjust shape to match your model input)
data = np.fromfile("calibration_raw/input_0000.raw", dtype=np.float32)
data = data.reshape(<your_model_input_shape>)  # e.g., (3, 640, 640) or (640, 640, 3)
print(f"Shape: {data.shape}, Range: [{data.min():.3f}, {data.max():.3f}]")
```

## Large-Dynamic-Range Output Trap (YOLO etc.) — cosine can lie

**Symptom:** After W8A8, certain output channels are constantly all-zero (e.g. the class-score channels of a detection model), yet the overall cosine is still >0.999 — it looks fine but is actually broken.
**Root cause:** QNN uses **one shared scale** for the entire output tensor. When the same tensor mixes large-magnitude channels (bbox coordinates 0~640) and small-magnitude channels (class score 0~1), the scale is dominated by the large values; the small values fall below the quantization resolution and are all truncated to 0. The overall cosine is dominated by the L2 norm of the large-value channels, which masks the failure of the small-value channels.
**Diagnosis:** Do NOT look at the overall cosine alone — **you MUST verify the numerical distribution per channel / per sub-task** (compute min/max + cosine separately for the bbox channels and the class channels).
**Solutions (in priority order):** ① Switch to W8A16 (16-bit activations, leaving precision for small values); ② Add more multi-class calibration samples (≥50, covering all classes); ③ Apply per-channel quantization to the output.

## Common Issues
- **Calibration error:** Verify float32 format and correct shape
- **Poor accuracy:** Increase calibration sample **diversity** — use real images of different classes/scenes. ⚠️ Augmentations (crop/flip/brightness) of ONE image are NOT diverse and barely help. How to obtain diverse real data → § Calibration Data Acquisition below.

## Calibration Data Acquisition

When `CALIBRATION_DATA` is absent, ask the user via the `question` tool before acting —
do NOT silently download or fabricate data.

**Ask 3 options:**
1. **User provides** — user uploads/specifies their own real dataset (preferred; highest quality).
2. **Agent auto-prepares** — Agent gathers real images by the priority below.
3. **Synthetic / random** — run the pipeline with generated data (no real samples). Pipeline
   runs, but accuracy is usually insufficient; if cosine < 0.95 (B6), re-quantize with real data.

**Auto-prepare priority (option 2) — offline first:**

| # | Source | Net | Note |
|---|--------|-----|------|
| 1 | Project `samples\images\` | No | Few images; smoke-test only |
| 2 | Workspace `${WORKSPACE}\` existing images | No | Cap the count; do not recurse whole disk |
| 3 | User-given path | No | Use the dir the user names; cap the count |
| 4 | Web download (tested hosts: `cloudcache.tencent-cloud.com`, `images.pexels.com`, `images.unsplash.com`; or torchvision datasets) | Yes | Only if 1–3 yield too few; may need network/proxy |
| 5 | Synthetic (same as option 3) | No | Last resort |

> 🔒 **Scan boundary (MANDATORY):** image scanning is allowed ONLY in the three explicit dirs
> above (project `samples\images\`, `${WORKSPACE}\`, user-given path), with a sample-count cap
> and early stop. NEVER use recursive `**` / whole-disk / home-dir globs — that has caused
> 30+ min hangs (see SKILL.md GATE note).

**Synthetic calibration** (option 3 / priority 5 — pipeline-bring-up only, not for final
accuracy): generate N float32 `.raw` files matching the model input shape, scaling
`np.random.normal(0,1,...)` per channel by ImageNet mean/std (`x[c] = x[c]*std[c] + mean[c]`,
`mean=[0.485,0.456,0.406]`, `std=[0.229,0.224,0.225]`) so values approximate the
post-preprocess range (better than pure noise).

> ⚠️ Synthetic data only lets the pipeline run; it does not give representative activation
> ranges. For real accuracy, use real multi-class samples (options 1/2). This is the same
> principle as B6 option 1.

