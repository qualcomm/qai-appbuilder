# OpenPose — Human Pose Estimation on Snapdragon NPU

## Overview

**OpenPose** detects 18 human body keypoints from an image, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Human Pose Estimation
- **Input**: RGB image (resized to 224×224)
- **Output**: 18 body keypoints with confidence heatmaps
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [openpose](https://aihub.qualcomm.com/compute/models/openpose)

## Model Architecture

- **Architecture**: OpenPose (VGG-based backbone + PAF + heatmap heads)
- **Input shape**: NHWC `[1, 224, 224, 3]`
- **Output**: Part Affinity Fields (PAF) + confidence heatmaps for 18 keypoints
- **Keypoints**: Nose, Neck, RShoulder, RElbow, RWrist, LShoulder, LElbow, LWrist, RHip, RKnee, RAnkle, LHip, LKnee, LAnkle, REye, LEye, REar, LEar

## Requirements

```
pip install torch torchvision Pillow numpy opencv-python
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model openpose --args "--image path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\pose_estimation\openpose\python\openpose.py
python models\computer_vision\pose_estimation\openpose\python\openpose.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/input.png` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `openpose.bin` — QNN model binary
