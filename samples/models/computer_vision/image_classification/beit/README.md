# BEiT — Image Classification on Snapdragon NPU

## Overview

**BEiT** (Bidirectional Encoder representation from Image Transformers) is a Vision Transformer model for image classification, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Image Classification (ImageNet-1K, 1000 classes)
- **Input**: RGB image (resized and center-cropped to 224×224)
- **Output**: Top-5 class predictions with confidence scores
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [beit](https://aihub.qualcomm.com/compute/models/beit)

## Model Architecture

- **Architecture**: BEiT Vision Transformer (ViT-B/16)
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
python run_inference.py --model beit
python run_inference.py --model beit --args "--image path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\image_classification\beit\python\beit.py
python models\computer_vision\image_classification\beit\python\beit.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `beit.bin` — QNN model binary
- `imagenet_labels.json` — ImageNet-1K class names

## Example Output

```
Top 5 predictions for image:

"golden retriever", 0.8234
"Labrador retriever", 0.0921
"kuvasz", 0.0123
"Great Pyrenees", 0.0089
"clumber spaniel", 0.0045
```
