# Depth Estimation

Monocular depth estimation models that predict per-pixel depth from a single RGB image.

## Models

| Model | Architecture | Input Shape | Output | AI Hub |
|-------|-------------|-------------|--------|--------|
| [depth_anything](depth_anything/) | DepthAnything (ViT + DPT) | NHWC `[1, 518, 518, 3]` | Depth heatmap (plasma colormap) | [Link](https://aihub.qualcomm.com/compute/models/depth_anything) |

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model depth_anything --args "--image path/to/image.jpg"
```
