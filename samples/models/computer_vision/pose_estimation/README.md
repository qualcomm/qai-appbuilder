# Pose Estimation

Pose estimation models that detect human body keypoints and hand landmarks from images or camera input.

## Models

| Model | Task | Input | Output | AI Hub |
|-------|------|-------|--------|--------|
| [openpose](openpose/) | Full-body pose estimation | NHWC `[1, 224, 224, 3]` | 18 body keypoints (PAF + heatmap) | [Link](https://aihub.qualcomm.com/compute/models/openpose) |
| [mediapipe_hand](mediapipe_hand/) | Hand landmark + gesture | Image or live camera | 21 hand landmarks + gesture (Play/Pause/Stop/Seek) | [Link](https://aihub.qualcomm.com/compute/models/mediapipe_hand) |

MediaPipe Hand uses a 2-stage pipeline: BlazePalm detector → landmark detector.

## Quick Start

```bash
cd qai-appbuilder\samples

# Body pose estimation
python run_inference.py --model openpose --args "--image path/to/image.jpg"

# Hand gesture recognition (live camera)
python run_inference.py --model mediapipe_hand
```
