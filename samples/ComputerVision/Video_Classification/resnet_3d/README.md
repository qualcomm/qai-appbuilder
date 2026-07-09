# ResNet-3D — Video Action Recognition on Snapdragon NPU

## Overview

**ResNet-3D** is a 3D convolutional neural network for video action recognition, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It classifies video clips into 400 action categories from the Kinetics-400 dataset.

- **Task**: Video Action Recognition
- **Dataset**: Kinetics-400 (400 action classes)
- **Input**: MP4 video file
- **Output**: Top-5 predicted action class names
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [resnet_3d](https://aihub.qualcomm.com/compute/models/resnet_3d)

## Model Architecture

ResNet-3D extends 2D ResNet with 3D convolutions to capture temporal information:
- **Input**: `float32[1, 112, 112, T, 3]` (NHWC with temporal dimension, NHWC-T format)
- **Output**: `float32[1, 400]` (logits for 400 Kinetics-400 action classes)
- **Preprocessing**: Resize to 128×171, center crop to 112×112, normalize to [0, 1]

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Video_Classification\resnet_3d\resnet_3d.py
```

With a custom video:
```bash
python ComputerVision\Video_Classification\resnet_3d\resnet_3d.py --video path\to\video.mp4
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--video` | `input.mp4` (auto-downloaded) | Path to the input video file |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and sample video are automatically downloaded on first run:
- `resnet_3d.bin` — QNN model binary
- `surfing_cutback.mp4` — sample surfing video for testing

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | MP4 video file | Any resolution, any frame rate |
| **Output** | String | Top-5 action class names (comma-separated) |

## Pipeline Details

```
Video file (MP4)
    ↓ torchvision.io.read_video → [T, H, W, C] uint8
    ↓ normalize: [T,H,W,C] uint8 → [C,T,H,W] float32 / 255
    ↓ resize: [C,T,H,W] → [C,T,128,171]
    ↓ center crop: [C,T,128,171] → [C,T,112,112]
    ↓ unsqueeze + transpose → [1,T,112,112,C] NHWC
    ↓ ResNet-3D (NPU)
Logits [float32, shape: (1, 400)]
    ↓ torch.topk(5)
Top-5 action class names
```

## Action Categories (Examples)

ResNet-3D can recognize 400 Kinetics-400 action categories including:
- **Sports**: surfing, skiing, basketball, tennis, swimming
- **Daily activities**: cooking, eating, drinking, reading
- **Social**: dancing, singing, clapping, hugging
- **Work**: typing, writing, using computer

## Example Output

```
SOC_ID: None
Top 5 predictions:
surfing water, windsurfing, kitesurfing, water skiing, wakeboarding
```

## Notes

- The model requires `torchvision` for video reading. If unavailable, `torchcodec` is used as a fallback.
- The input tensor format is `[1, T, H, W, C]` (NHWC with temporal dimension).
- The number of frames T depends on the video length and frame rate.
