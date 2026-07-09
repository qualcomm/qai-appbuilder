# BEiT — Vision Transformer Image Classification on Snapdragon NPU

## Overview

**BEiT** (Bidirectional Encoder representation from Image Transformers) is a Vision Transformer model for image classification, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It classifies images into 1000 ImageNet categories.

- **Task**: Image Classification
- **Dataset**: ImageNet-1K (1000 classes)
- **Input**: RGB image (any size, resized to 224×224)
- **Output**: Top-5 predicted class names with probabilities
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [beit](https://aihub.qualcomm.com/compute/models/beit)

## Model Architecture

BEiT is a Vision Transformer (ViT) pre-trained with masked image modeling:
- **Input**: `float32[1, 224, 224, 3]` (NHWC format)
- **Output**: `float32[1, 1000]` (logits for 1000 ImageNet classes)
- **Preprocessing**: Resize to 224×224, normalize to [0, 1]

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Image_Classification\beit\beit.py
```

With a custom image:
```bash
python ComputerVision\Image_Classification\beit\beit.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and ImageNet class labels are automatically downloaded on first run:
- `beit.bin` — QNN model binary
- `imagenet_labels.json` — 1000 ImageNet class names
- `input.jpg` — sample dog image for testing

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized to 224×224 |
| **Output** | String | Top-5 class names with probability scores |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open → resize 224×224 → ToTensor
Image tensor [float32, NCHW: 1×3×224×224]
    ↓ np.transpose → NHWC: 1×224×224×3
    ↓ BEiT (NPU)
Logits [float32, shape: (1, 1000)]
    ↓ torch.softmax
    ↓ torch.topk(5)
Top-5 class names + probabilities
```

## Example Output

```
SOC_ID: None
[INFO] Detected platform: wos
Top 5 predictions for image:

"golden retriever", 0.8234
"Labrador retriever", 0.0912
"kuvasz", 0.0123
"Great Pyrenees", 0.0089
"clumber spaniel", 0.0045
```

## Notes

- The model supports both WoS (ARM64 Windows) and x86 Windows platforms.
- On WoS, a specific hub model ID (`HUB_ID_H`) is used for download.
- The model uses NHWC input format (batch, height, width, channel).
