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
python3 scripts/aipc_convert_fp.py --input_network model.onnx
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
`calibration_list.txt`:
```
input:=calibration_raw/input_0000.raw
input:=calibration_raw/input_0001.raw
...
```

### Step 3: Run Conversion
#### SNPE
- you need to have fp32 dlc model first,
- runn commands
```bash
snpe-dlc-quant --input_dlc [model.dlc fp32]--output_dlc [model_a<activation_bitwidth>w<weight_bitwidth>.dlc] \
--input_list .\calibration_list.txt --act_bitwidth <activation_bitwidth> --weights_bitwidth <weight_bitwidth>
```
#### QNN
Use  `scripts/aipc_convert_int.py `  as QNN_CONVERT_SCRIPT.
```bash
python3 [QNN_CONVERT_SCRIPT] \
    --input_network model.onnx \
    --input_list calibration_list.txt \
    --act_bw <activation_bitwidth> --weight_bw <weight_bitwidth>
```
act_bw 16/weight_bw 8 is suggested setting for vision model.
show
**Output:** `model_a{act_bw}_w{weight_bw}.*` (e.g., `model_a16_w8.cpp`, `model_a8_w8.cpp`)

## Key Points
- **FP16:** Fast conversion, no calibration needed
- **INT8/INT16:** Better performance, requires calibration data
- **Calibration:** Use diverse training/validation samples
- **Preprocessing:** Must match inference exactly

## Verification
```python
# Check calibration data (adjust shape to match your model input)
data = np.fromfile("calibration_raw/input_0000.raw", dtype=np.float32)
data = data.reshape(<your_model_input_shape>)  # e.g., (3, 640, 640) or (640, 640, 3)
print(f"Shape: {data.shape}, Range: [{data.min():.3f}, {data.max():.3f}]")
```

## Common Issues
- **Calibration error:** Verify float32 format and correct shape
- **Poor accuracy:** Increase calibration sample diversity

