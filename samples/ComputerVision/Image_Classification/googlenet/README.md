# GoogLeNet — Image Classification on Snapdragon NPU

## Overview

**GoogLeNet** (Inception v1) is a classic deep convolutional neural network for image classification, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It classifies images into 1000 ImageNet categories.

- **Task**: Image Classification
- **Dataset**: ImageNet-1K (1000 classes)
- **Input**: RGB image (any size, resized to 224×224)
- **Output**: Top-5 predicted class names with probabilities
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [googlenet](https://aihub.qualcomm.com/compute/models/googlenet)

## Model Architecture

GoogLeNet uses Inception modules with parallel convolutions at different scales:
- **Input**: `float32[1, 224, 224, 3]` (NHWC format)
- **Output**: `float32[1, 1000]` (logits for 1000 ImageNet classes)
- **Preprocessing**: Resize to 224×224, normalize to [0, 1]

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Image_Classification\googlenet\googlenet.py
```

With a custom image:
```bash
python ComputerVision\Image_Classification\googlenet\googlenet.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `dog.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and ImageNet class labels are automatically downloaded on first run:
- `googlenet.bin` — QNN model binary
- `imagenet_labels.json` — 1000 ImageNet class names
- `dog.jpg` — sample dog image for testing

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
    ↓ GoogLeNet (NPU)
Logits [float32, shape: (1, 1000)]
    ↓ torch.softmax
    ↓ torch.topk(5)
Top-5 class names + probabilities
```

## Example Output

```
SOC_ID: None
Top 5 predictions for image:

golden retriever 0.7823
Labrador retriever 0.1012
kuvasz 0.0234
clumber spaniel 0.0089
Sussex spaniel 0.0045
```

## Notes

- The model uses NHWC input format (batch, height, width, channel).
- No ImageNet normalization is applied (raw [0, 1] pixel values).
