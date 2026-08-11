# U-Net Segmentation — Semantic Segmentation on Snapdragon NPU

## Overview

**U-Net Segmentation** is a semantic segmentation model that produces a binary segmentation mask overlay, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Semantic Segmentation (binary mask)
- **Input**: RGB image (resized to 640×1280)
- **Output**: Binary segmentation mask overlay on the original image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [unet_segmentation](https://aihub.qualcomm.com/compute/models/unet_segmentation)

## Model Architecture

- **Architecture**: U-Net with encoder-decoder and skip connections
- **Input shape**: NHWC `[1, 640, 1280, 3]`
- **Output**: Binary segmentation mask

## Requirements

```
pip install torch torchvision Pillow numpy opencv-python
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model unet_segmentation --args "--input_image_path path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\semantic_segmentation\unet_segmentation\python\unet_segmentation.py
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `unet_segmentation.bin` — QNN model binary
