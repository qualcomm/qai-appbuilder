# Face Recognition

Face analysis models for attribute detection and 3D face modeling.

## Models

| Model | Task | Input Shape | Output | AI Hub |
|-------|------|-------------|--------|--------|
| [face_attrib_net](face_attrib_net/) | Face attribute detection | NHWC `[1, 128, 128, 3]` | 6 attributes: identity, liveness, eye closeness, glasses, mask, sunglasses | [Link](https://aihub.qualcomm.com/compute/models/face_attrib_net) |
| [facemap_3dmm](facemap_3dmm/) | 3D face modeling | Face image | 264 3D morphable model params → 68 facial landmarks | [Link](https://aihub.qualcomm.com/compute/models/facemap_3dmm) |

## Quick Start

```bash
cd qai-appbuilder\samples

# Face attribute detection
python run_inference.py --model face_attrib_net --args "--image path/to/face.jpg"

# 3D face modeling
python run_inference.py --model facemap_3dmm --args "--image path/to/face.jpg"
```
