# ResNet-3D — Video Action Classification on Snapdragon NPU

## Overview

**ResNet-3D** classifies human actions from video clips using 3D spatiotemporal convolutions, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Video Action Classification (Kinetics-400, 400 classes)
- **Input**: MP4 video clip
- **Output**: Top-5 predicted action classes
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [resnet_3d](https://aihub.qualcomm.com/compute/models/resnet_3d)

## Model Architecture

- **Architecture**: 3D ResNet with spatiotemporal convolutions
- **Input shape**: NHWC-T `[1, T, 112, 112, 3]` (T = number of frames)
- **Output**: `[1, 400]` (class logits for 400 Kinetics-400 actions)
- **Classes**: 400 human action categories (surfing, cooking, dancing, etc.)

## Requirements

```
pip install torch torchvision Pillow numpy av
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model resnet_3d --args "--video path\to\video.mp4"
```

Or run directly:
```bash
python models\computer_vision\video_classification\resnet_3d\python\resnet_3d.py
python models\computer_vision\video_classification\resnet_3d\python\resnet_3d.py --video path\to\video.mp4
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--video` | `assets/input.mp4` (auto-downloaded) | Path to the input video file |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `resnet_3d.bin` — QNN model binary
