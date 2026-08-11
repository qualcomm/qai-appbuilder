# Video Classification

Video classification models that recognize human actions and activities from video clips.

## Models

| Model | Architecture | Input Shape | Output | AI Hub |
|-------|-------------|-------------|--------|--------|
| [resnet_3d](resnet_3d/) | 3D ResNet (spatiotemporal convolutions) | NHWC-T `[1, T, 112, 112, 3]` | Top-5 Kinetics-400 action class predictions | [Link](https://aihub.qualcomm.com/compute/models/resnet_3d) |

The model classifies 400 human action categories from the Kinetics-400 dataset (e.g., surfing, cooking, dancing).

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model resnet_3d --args "--video path/to/video.mp4"
```
