# Image Classification

Image classification models that identify the main subject of an image from the ImageNet-1K dataset (1000 classes).

## Models

| Model | Architecture | Input Shape | AI Hub |
|-------|-------------|-------------|--------|
| [beit](beit/) | BEiT Vision Transformer (ViT-B/16) | NHWC `[1, 224, 224, 3]` | [Link](https://aihub.qualcomm.com/compute/models/beit) |
| [googlenet](googlenet/) | GoogLeNet (Inception v1) | NHWC `[1, 224, 224, 3]` | [Link](https://aihub.qualcomm.com/compute/models/googlenet) |
| [inception_v3](inception_v3/) | Inception V3 | NHWC `[1, 224, 224, 3]` | [Link](https://aihub.qualcomm.com/compute/models/inception_v3) |

All models output top-5 ImageNet-1K class predictions with confidence scores.

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model beit --args "--image path/to/image.jpg"
python run_inference.py --model googlenet --args "--image path/to/image.jpg"
python run_inference.py --model inception_v3 --args "--image path/to/image.jpg"
```
