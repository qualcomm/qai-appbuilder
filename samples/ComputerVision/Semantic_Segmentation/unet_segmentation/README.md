# U-Net Segmentation — Semantic Segmentation on Snapdragon NPU

## Overview

**U-Net Segmentation** is a semantic segmentation model based on the U-Net architecture, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It generates pixel-level segmentation masks from input images.

- **Task**: Semantic Segmentation
- **Input**: RGB image (resized to 640×1280)
- **Output**: Binary segmentation mask overlaid on the input image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [unet_segmentation](https://aihub.qualcomm.com/compute/models/unet_segmentation)

## Model Architecture

U-Net uses an encoder-decoder architecture with skip connections:
- **Input**: `float32[1, 640, 1280, 3]` (NHWC format)
- **Output**: `float32[1, 2, 640, 1280]` (2-class logits, reshaped from flat output)
- **Preprocessing**: Resize + pad to 640×1280 (preserves aspect ratio)

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Semantic_Segmentation\unet_segmentation\unet_segmentation.py
```

With custom input/output images:
```bash
python ComputerVision\Semantic_Segmentation\unet_segmentation\unet_segmentation.py --image input.jpg --output_image output.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.jpg` | Path to the input image |
| `--output_image` | `output.png` | Path to save the segmentation result |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run:
- `unet_segmentation.bin` — QNN model binary

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized+padded to 640×1280 |
| **Output** | Segmentation image (PNG) | Mask overlay on original image; also saves `output_mask.png` |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open
    ↓ pil_resize_pad → 640×1280 (preserves aspect ratio)
    ↓ preprocess_PIL_image → float32 NCHW
    ↓ np.transpose → NHWC: 1×640×1280×3
    ↓ U-Net (NPU)
Output [float32, flat]
    ↓ reshape → [1, 2, 640, 1280]
    ↓ argmax(dim=1) → binary mask [640, 1280]
    ↓ Image.blend (alpha=0.5) → overlay on input
    ↓ pil_undo_resize_pad → original size
Segmentation image (PNG)
```

## Notes

- Two output files are saved: `output.png` (50% blend overlay) and `output_mask.png` (pure mask).
- The model performs binary segmentation (2 classes: foreground/background).
- Input is padded with "reflect" mode to maintain aspect ratio.
