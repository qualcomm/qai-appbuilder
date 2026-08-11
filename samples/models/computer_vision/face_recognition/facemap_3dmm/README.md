# FaceMap 3DMM — 3D Face Modeling on Snapdragon NPU

## Overview

**FaceMap 3DMM** predicts 264 3D Morphable Model (3DMM) parameters from a face image and renders 68 facial landmarks, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: 3D Face Modeling + Facial Landmark Detection
- **Input**: Face image
- **Output**: 264 3DMM parameters → 68 facial landmarks rendered on image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU) or ONNX float model
- **AI Hub Model**: [facemap_3dmm](https://aihub.qualcomm.com/compute/models/facemap_3dmm)

## Model Architecture

- **Architecture**: CNN-based 3DMM parameter regression
- **Output**: 264 parameters (shape, expression, pose, illumination)
- **Post-processing**: 3DMM rendering → 68 2D facial landmarks

## Requirements

```
pip install torch torchvision Pillow numpy opencv-python
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model facemap_3dmm --args "--image path\to\face.jpg"
```

Or run directly:
```bash
python models\computer_vision\face_recognition\facemap_3dmm\python\facemap_3dmm.py
python models\computer_vision\face_recognition\facemap_3dmm\python\facemap_3dmm.py --image path\to\face.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/input.jpg` (auto-downloaded) | Path to the input face image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `facemap_3dmm.bin` — QNN model binary

## Notes

- Supports ONNX float model for better accuracy (auto-downloaded if needed).
- The 3DMM rendering uses pre-computed mean face and shape basis matrices.
