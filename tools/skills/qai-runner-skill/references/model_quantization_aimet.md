# AIMET-ONNX PTQ Quantization Guide

## Overview

Quantizing ONNX models using the AIMET-ONNX calibration/scale derivation engine consists of two clean phases:
1. **Calibration Stage (Python Prompt Example)**: Use `aimet_onnx` to fold Batch Normalization, apply CLE, run QuantSim range calibration, and export the pre-calibrated model (`.onnx` + `.encodings`).
2. **Conversion Stage (Tool Script)**: Use the lightweight `aipc_convert_aimet.py` script to compile the pre-calibrated artifacts directly into target-compatible formats (QNN `.so` or SNPE `.dlc`).

---

## PTQ Optimization & Conversion Flow

```
[ Original Float32 ONNX ]
          │
          ▼
   1. RUN PTQ CALIBRATION (Standalone Python)
      ├─► BN Folding & CLE (Data-free)
      └─► QuantSim Calibration (Representative dataset)
          │
          ▼
[ Calibrated ONNX & scale .encodings ]
          │
          ▼
   2. RUN CONVERSION (aipc_convert_aimet.py)
      └─► Handoff via --quantization_overrides
          │
          ▼
[ Final Compiled Target Binary (QNN .so / SNPE .dlc) ]
```

---

## Stage 1: Calibration Stage (Python Prompt Example)

Execute this standalone Python snippet to run CLE, range calibration, and export the pre-calibrated ONNX model and scale encodings.

```python
import numpy as np
import onnx
from aimet_onnx.batch_norm_fold import fold_all_batch_norms_to_weight
from aimet_onnx.cross_layer_equalization import equalize_model
from aimet_onnx.common.defs import QuantScheme
from aimet_onnx.quantsim import QuantizationSimModel

# 1. Load float ONNX model
model = onnx.load("model.onnx")

# 2. Apply BN Folding and Cross-Layer Equalization (CLE - data-free optimization)
fold_all_batch_norms_to_weight(model)
equalize_model(model)

# 3. Create QuantSim Simulation Model
# A16W8 example: activation int16, weight int8
sim = QuantizationSimModel(
    model=model,
    quant_scheme=QuantScheme.post_training_tf_enhanced,
    param_type="int8",
    activation_type="int16"
)

# 4. Load calibration inputs (representative dataset)
# Return an Iterable of dict mapping ONNX input names to numpy float32 arrays
def get_calib_inputs(num_samples=100):
    for _ in range(num_samples):
        yield {"images": np.random.rand(1, 3, 640, 640).astype(np.float32)}

calib_data = list(get_calib_inputs(100))

# 5. Compute quantization scale and offsets
sim.compute_encodings(calib_data)

# 6. Export calibrated model and scale encodings
sim.export(
    path="runs/aimet_artifacts",
    filename_prefix="model_ptq_calibrated",
    export_model=True,
    encoding_version="1.0.0"
)

print("Calibration complete!")
```

### AIMET package compatibility guard

If both `aimet_onnx` and `aimet_torch` are installed, avoid importing from `aimet_common` in ONNX workflows.
Prefer `aimet_onnx.common.*` namespace explicitly.

Quick preflight:

```bash
python - <<'PY'
from aimet_onnx.common.defs import QuantScheme
from aimet_onnx.quantsim import QuantizationSimModel
print("AIMET ONNX import preflight: OK")
PY
```

### Calibration sample sizing guidance

- First-pass recommendation: **50–200** representative samples.
- Increase sample count only when accuracy requires it (to reduce calibration time/memory pressure).

**Generated Artifacts:**  
- `runs/aimet_artifacts/model_ptq_calibrated.onnx` (Pre-calibrated ONNX model)  
- `runs/aimet_artifacts/model_ptq_calibrated.encodings` (Derived scale and offset JSON)

---

## Stage 2: Conversion Stage (Tool Script)

Pass the exported, pre-calibrated ONNX model and encodings to the lightweight `aipc_convert_aimet.py` helper to compile them for target runtimes.

### 1. QNN Flow (Convert to `.cpp`/`.bin` and compile shared library)

```bash
# Convert to QNN model shared library (A16W8: act_bw 16, weight_bw 8)
python skills/aipc-toolkit/scripts/aipc_convert_aimet.py \
  --input_network runs/aimet_artifacts/model_ptq_calibrated.onnx \
  --quantization_overrides runs/aimet_artifacts/model_ptq_calibrated.encodings \
  --output-root ./qairt_output \
  --act_bw 16 --weight_bw 8 \
  --flow QNN \
  --target-arch aarch64-oe-linux-gcc11.2
```

**Outputs:**
- `./qairt_output/test_libs_model_ptq_calibrated_q_aarch64/` (directory containing compiled `.so` model library) ✓

---

### 2. SNPE Flow (Convert directly to SNPE DLC)

```bash
# Convert directly to SNPE DLC (INT8: act_bw 8, weight_bw 8)
python skills/aipc-toolkit/scripts/aipc_convert_aimet.py \
  --input_network runs/aimet_artifacts/model_ptq_calibrated.onnx \
  --quantization_overrides runs/aimet_artifacts/model_ptq_calibrated.encodings \
  --output-root ./qairt_output \
  --act_bw 8 --weight_bw 8 \
  --flow SNPE
```

**Outputs:**
- `./qairt_output/model_ptq_calibrated_aimet_a8_w8.dlc` ✓

---

## Core Script Arguments

| Arg | Description |
|---|---|
| `--input_network` | Path to the pre-calibrated ONNX model exported by AIMET |
| `--quantization_overrides` | Path to the scale `.encodings` file exported by AIMET |
| `--act_bw` | Target activation bitwidth (e.g. `8` or `16`) |
| `--weight_bw` | Target weight bitwidth (e.g. `8` or `16`) |
| `--flow` | `QNN` (default) or `SNPE` target flow |
| `--output-root` | Output root directory for compiled outputs |
| `--target-arch` | Target compilation architecture (e.g. `aarch64-oe-linux-gcc11.2`) |
| `--input-dim` | Shape overrides for dynamic ONNX inputs (e.g. `--input-dim input,1,3,640,640`) |

---

## Manual Toolchain Fallback (Under the Hood)

If the conversion script fails in an edge case, use these direct CLI commands to compile the pre-calibrated AIMET artifacts manually.

### QNN Direct CLI Fallback

Run QNN converter passing the scale encodings file via `--quantization_overrides` instead of running dynamic calibration:

```bash
# 1. Convert ONNX to C++ graph with overrides
qnn-onnx-converter \
  --input_network runs/aimet_artifacts/model_ptq_calibrated.onnx \
  --quantization_overrides runs/aimet_artifacts/model_ptq_calibrated.encodings \
  -d <input_name> <shape_csv> \
  --output_path runs/aimet_artifacts/model_qnn.cpp \
  --act_bw 8 --weight_bw 8 --bias_bw 32 \
  --use_per_channel_quantization

# 2. Compile C++ graph to shared library
qnn-model-lib-generator \
  -c runs/aimet_artifacts/model_qnn.cpp \
  -b runs/aimet_artifacts/model_qnn.bin \
  -t aarch64-oe-linux-gcc11.2 \
  -o runs/aimet_artifacts/model_libs
```

### SNPE Direct CLI Fallback

Convert the ONNX directly to SNPE DLC passing the overrides. **Do not** run post-conversion `snpe-dlc-quantize` since scales are already built in:

```bash
snpe-onnx-to-dlc \
  --input_network runs/aimet_artifacts/model_ptq_calibrated.onnx \
  --quantization_overrides runs/aimet_artifacts/model_ptq_calibrated.encodings \
  -d <input_name> <shape_csv> \
  -o runs/aimet_artifacts/model_quantized.dlc
```

---

## Common Troubleshooting

- **`--input_list` rejected by qnn-onnx-converter**: Ensure you omit `--input_list` from the converter command. Pass scale and offsets using `--quantization_overrides` instead.
- **Accuracy loss**: Ensure representative calibration samples are used (unlabeled dataset matching pre-processing exactly). If accuracy is still low, transition sensitive layers to 16-bit using Mixed Representation.
