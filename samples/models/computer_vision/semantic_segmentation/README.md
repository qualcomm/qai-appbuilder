# Semantic Segmentation

Semantic segmentation models that assign a class label to every pixel in an image.

## Models

| Model | Architecture | Input Shape | Output | AI Hub |
|-------|-------------|-------------|--------|--------|
| [unet_segmentation](unet_segmentation/) | U-Net | NHWC `[1, 640, 1280, 3]` | Binary segmentation mask overlay | [Link](https://aihub.qualcomm.com/compute/models/unet_segmentation) |

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model unet_segmentation --args "--input_image_path input.jpg"
```
