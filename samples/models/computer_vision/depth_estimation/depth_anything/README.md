# Depth Anything — Monocular Depth Estimation on Snapdragon NPU

## Overview

**Depth Anything** is a monocular depth estimation model that predicts per-pixel depth from a single RGB image, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Monocular Depth Estimation
- **Input**: RGB image (resized to 518×518)
- **Output**: Depth heatmap (plasma colormap visualization)
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [depth_anything](https://aihub.qualcomm.com/compute/models/depth_anything)

## Model Architecture

- **Architecture**: DepthAnything (Vision Transformer backbone + DPT decoder)
- **Input shape**: NHWC `[1, 518, 518, 3]`
- **Output**: Depth map (normalized, visualized with plasma colormap)

## Requirements

```
pip install torch torchvision Pillow numpy opencv-python
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model depth_anything --args "--image path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\depth_estimation\depth_anything\python\depth_anything.py
python models\computer_vision\depth_estimation\depth_anything\python\depth_anything.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `depth_anything.bin` — QNN model binary
