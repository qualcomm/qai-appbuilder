# MediaPipe Hand — Hand Landmark Detection on Snapdragon NPU

## Overview

**MediaPipe Hand** detects 21 hand landmarks and recognizes gestures from images or live camera input, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Hand Landmark Detection + Gesture Recognition
- **Input**: Image or live camera feed
- **Output**: 21 hand landmarks + gesture (Play/Pause/Stop/Seek)
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [mediapipe_hand](https://aihub.qualcomm.com/compute/models/mediapipe_hand)

## Model Architecture

2-stage pipeline:
1. **BlazePalm Detector** (`handdetector.bin`) — detects hand bounding boxes
2. **Hand Landmark Detector** (`landmarkdetector.bin`) — predicts 21 3D landmarks

## Requirements

```
pip install torch torchvision Pillow numpy opencv-python pygame
```

## Quick Start

```bash
cd qai-appbuilder\samples

# Live camera mode
python run_inference.py --model mediapipe_hand

# With image file
python run_inference.py --model mediapipe_hand --args "--image path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\pose_estimation\mediapipe_hand\python\mediapipe_hand.py
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | Live camera | Path to input image (omit for live camera) |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run to `models/`:
- `mediapipe_hand-handdetector-snapdragon_x_elite.bin` — hand detector
- `mediapipe_hand-landmarkdetector-snapdragon_x_elite.bin` — landmark detector

## Gesture Recognition

| Gesture | Action |
|---------|--------|
| Open hand (5 fingers) | Play |
| Closed fist | Pause |
| Pointing up (1 finger) | Stop |
| Two fingers | Seek |
