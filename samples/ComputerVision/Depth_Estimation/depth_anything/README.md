# Depth Anything — Monocular Depth Estimation on Snapdragon NPU

## Overview

**Depth Anything** is a foundation model for monocular depth estimation, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It estimates the depth of every pixel in a single RGB image without any additional sensors.

- **Task**: Monocular Depth Estimation
- **Input**: RGB image (any size, resized to 518×518)
- **Output**: Depth heatmap image (plasma colormap)
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [depth_anything](https://aihub.qualcomm.com/compute/models/depth_anything)

## Model Architecture

Depth Anything uses a Vision Transformer (ViT) encoder with a DPT decoder:
- **Input**: `float32[1, 518, 518, 3]` (NHWC format)
- **Output**: `float32[1, 518, 518]` (depth map, flat)
- **Preprocessing**: Resize + pad to 518×518 (preserves aspect ratio)

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Depth_Estimation\depth_anything\depth_anything.py
```

With a custom image:
```bash
python ComputerVision\Depth_Estimation\depth_anything\depth_anything.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and sample image are automatically downloaded on first run:
- `depth_anything.bin` — QNN model binary
- `test_input_image.jpg` — sample image for testing

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized+padded to 518×518 |
| **Output** | Depth heatmap (JPG) | Plasma colormap visualization, saved as `output.jpg` |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open
    ↓ pil_resize_pad → 518×518 (preserves aspect ratio)
    ↓ transforms.ToTensor → float32 NCHW
    ↓ np.transpose → NHWC: 1×518×518×3
    ↓ Depth Anything (NPU)
Depth map [float32, flat]
    ↓ reshape → [1, 1, 518, 518]
    ↓ undo_resize_pad → original size
    ↓ normalize → [0, 1]
    ↓ plt.cm.plasma colormap
Depth heatmap (JPG)
```

## Example Output

The output is a colorized depth map where:
- **Warm colors** (yellow/red) = closer objects
- **Cool colors** (blue/purple) = farther objects

## Notes

- The depth values are relative (not metric depth in meters).
- The output is displayed and saved as `output.jpg` in the script directory.
- Uses `matplotlib.cm.plasma` colormap for visualization.
