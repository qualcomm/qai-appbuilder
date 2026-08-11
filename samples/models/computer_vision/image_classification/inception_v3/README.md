# Inception V3 — Image Classification on Snapdragon NPU

## Overview

**Inception V3** is a deep convolutional neural network for image classification, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Image Classification (ImageNet-1K, 1000 classes)
- **Input**: RGB image (resized and center-cropped to 224×224)
- **Output**: Top-5 class predictions with confidence scores
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [inception_v3](https://aihub.qualcomm.com/compute/models/inception_v3)

## Model Architecture

- **Architecture**: Inception V3 with factorized convolutions
- **Input shape**: NHWC `[1, 224, 224, 3]`
- **Output shape**: `[1, 1000]` (class logits)
- **Preprocessing**: Resize → CenterCrop(224) → ToTensor

## Requirements

```
pip install torch torchvision Pillow
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model inception_v3
python run_inference.py --model inception_v3 --args "--image path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\image_classification\inception_v3\python\inception_v3.py
python models\computer_vision\image_classification\inception_v3\python\inception_v3.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `inception_v3.bin` — QNN model binary
- `imagenet_labels.json` — ImageNet-1K class names
