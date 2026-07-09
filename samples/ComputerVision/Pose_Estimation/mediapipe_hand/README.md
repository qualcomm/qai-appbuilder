# MediaPipe Hand — Hand Landmark Detection & Gesture Recognition on Snapdragon NPU

## Overview

**MediaPipe Hand** is a two-stage hand tracking pipeline that detects hand bounding boxes and 21 hand landmarks, with gesture recognition, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It supports both static image inference and real-time camera input.

- **Task**: Hand Landmark Detection + Gesture Recognition
- **Landmarks**: 21 hand keypoints (wrist, finger joints, fingertips)
- **Gestures**: Play, Pause, Stop, fast forward 10 seconds, back 10 seconds
- **Input**: RGB image or live camera feed
- **Output**: Annotated image with hand landmarks and gesture label
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Models**: [handdetector](https://aihub.qualcomm.com/compute/models/handdetector), [landmarkdetector](https://aihub.qualcomm.com/compute/models/landmarkdetector)

## Model Architecture

MediaPipe Hand uses a two-stage pipeline:

| Stage | Model File | Description |
| ----- | ---------- | ----------- |
| 1. Hand Detector | `mediapipe_hand-handdetector.bin` | BlazePalm detector: detects hand bounding boxes and 7 keypoints from 256×256 input |
| 2. Landmark Detector | `mediapipe_hand-landmarkdetector.bin` | Detects 21 hand landmarks from a cropped 256×256 ROI |

**Hand Detector output**: box scores `[2944]`, box coordinates `[2944×18]`  
**Landmark Detector output**: landmark score `[1]`, handedness `[1]`, landmarks `[63]` (21×3 xyz)

## Requirements

```
pip install opencv-python
```

## Quick Start

**Camera mode** (real-time):
```bash
cd qai-appbuilder\samples
python ComputerVision\Pose_Estimation\mediapipe_hand\mediapipe_hand.py
```

**Image mode**:
```bash
python ComputerVision\Pose_Estimation\mediapipe_hand\mediapipe_hand.py --imagefile path\to\image.jpg
```

**With audio control**:
```bash
python ComputerVision\Pose_Estimation\mediapipe_hand\mediapipe_hand.py --audiofile path\to\music.wav
```

Press **ESC** to exit camera mode.

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--imagefile` | None (camera mode) | Path to input image; if not set, uses camera |
| `--audiofile` | None | Path to audio file for gesture-controlled playback |
| `--displayPredict` | `True` | Whether to display the prediction result |
| `--DemoMode` | `True` | `True` = draw all landmarks/boxes; `False` = only show gesture label |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run:
- `mediapipe_hand-handdetector.bin` — hand detector QNN binary
- `mediapipe_hand-landmarkdetector.bin` — landmark detector QNN binary

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file or camera frame | Any size |
| **Output** | Annotated image | 21 hand landmarks + gesture label |

## Pipeline Details

```
Image / Camera frame
    ↓ resize_pad → 256×256
    ↓ Hand Detector (NPU)
Box scores [2944] + Box coords [2944×18]
    ↓ decode_preds_from_anchors (BlazePalm anchors)
    ↓ batched_nms (iou=0.3, score=0.95)
Selected boxes + keypoints
    ↓ compute_object_roi (rotation-aware ROI)
    ↓ apply_affine_transform → 256×256 crop
    ↓ Landmark Detector (NPU)
Landmarks [21×3] (x, y, confidence)
    ↓ apply_inverse_affine → original coordinates
    ↓ recognize_gesture
Gesture label (Play/Pause/Stop/...)
    ↓ draw_landmarks + draw_gesture_text
Annotated image
```

## Gesture Recognition

| Gesture | Hand Shape | Action |
| ------- | ---------- | ------ |
| **Stop** | Fist (thumb between fingers) | Stop audio |
| **Pause** | Open hand (all fingers extended, thumb open) | Pause audio |
| **Play** | OK sign (thumb + index touching) | Play audio |
| **fast forward 10 seconds** | Fist with thumb pointing right | Seek +10s |
| **back 10 seconds** | Fist with thumb pointing left | Seek -10s |

## Notes

- In **Demo Mode** (`--DemoMode True`): draws 21 keypoints, connections, ROI box, and palm rectangle.
- In **End-User Mode** (`--DemoMode False`): only shows the gesture label text.
- Camera mode processes every 30th frame for gesture recognition (to reduce CPU load).
- The landmark detector requires confidence ≥ 0.95 to accept a detection.
- Uses BlazePalm anchor-based detection with 2944 anchors.
