# Face Attribute Net — Face Attribute Detection on Snapdragon NPU

## Overview

**Face Attribute Net** is a multi-task face analysis model that detects multiple facial attributes simultaneously, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It outputs identity features, liveness features, and binary attributes (eye closeness, glasses, mask, sunglasses).

- **Task**: Face Attribute Detection
- **Attributes**: Identity feature, liveness feature, eye closeness, glasses, mask, sunglasses
- **Input**: Face image (128×128)
- **Output**: JSON file with attribute values
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [face_attrib_net](https://aihub.qualcomm.com/compute/models/face_attrib_net)

## Model Architecture

FaceAttribNet is a lightweight CNN for multi-task face attribute prediction:
- **Input**: `float32[1, 128, 128, 3]` (NHWC format, normalized to [-1, 1])
- **Outputs**:
  - `id_feature`: Identity embedding vector
  - `liveness_feature`: Anti-spoofing liveness feature
  - `eye_closeness`: Eye open/closed score
  - `glasses`: Glasses presence score
  - `mask`: Face mask presence score
  - `sunglasses`: Sunglasses presence score

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Face_Recognition\face_attrib_net\face_attrib_net.py
```

With a custom face image:
```bash
python ComputerVision\Face_Recognition\face_attrib_net\face_attrib_net.py --image path\to\face.bmp
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.bmp` (auto-downloaded) | Path to the input face image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and sample face image are automatically downloaded on first run:
- `face_attrib_net.bin` — QNN model binary
- `img_sample.bmp` — sample face image for testing

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | BMP/JPG/PNG image | Face image, resized to 128×128 |
| **Output** | JSON file (`output.json`) | Face attribute values |

## Pipeline Details

```
Face image
    ↓ PIL.Image.open → resize 128×128
    ↓ normalize to [-1, 1]
    ↓ np.transpose → NHWC: 1×128×128×3
    ↓ FaceAttribNet (NPU)
[id_feature, liveness_feature, eye_closeness, glasses, mask, sunglasses]
    ↓ save_face_attributes_json
output.json
```

## Output Format

The output JSON file contains:
```json
{
  "id_feature": [...],
  "liveness_feature": [...],
  "eye_closeness": 0.95,
  "glasses": 0.02,
  "mask": 0.01,
  "sunglasses": 0.03
}
```

## Notes

- The model uses normalized input in the range [-1, 1] (not [0, 1]).
- Uses shared utilities from `common/_face_recognition.py`.
- The identity feature can be used for face recognition/verification by computing cosine similarity between embeddings.
