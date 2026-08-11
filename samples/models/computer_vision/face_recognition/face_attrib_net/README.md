# FaceAttribNet — Face Attribute Detection on Snapdragon NPU

## Overview

**FaceAttribNet** detects 6 face attributes from a face image, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Face Attribute Detection
- **Input**: Face image (128×128)
- **Output**: 6 attributes as JSON (identity, liveness, eye closeness, glasses, mask, sunglasses)
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [face_attrib_net](https://aihub.qualcomm.com/compute/models/face_attrib_net)

## Model Architecture

- **Architecture**: Lightweight CNN for multi-task face attribute classification
- **Input shape**: NHWC `[1, 128, 128, 3]`
- **Output**: 6 binary/continuous attribute scores

## Requirements

```
pip install torch torchvision Pillow numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model face_attrib_net --args "--image path\to\face.jpg"
```

Or run directly:
```bash
python models\computer_vision\face_recognition\face_attrib_net\python\face_attrib_net.py
python models\computer_vision\face_recognition\face_attrib_net\python\face_attrib_net.py --image path\to\face.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/input.bmp` (auto-downloaded) | Path to the input face image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `face_attrib_net.bin` — QNN model binary

## Output Attributes

| Attribute | Description |
|-----------|-------------|
| Identity | Face identity embedding |
| Liveness | Real face vs. spoof detection |
| Eye Closeness | Open/closed eye detection |
| Glasses | Glasses presence |
| Mask | Face mask presence |
| Sunglasses | Sunglasses presence |
