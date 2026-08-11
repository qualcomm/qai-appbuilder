# Image Editing

Image inpainting models that fill in masked regions of an image with realistic content (object removal).

## Models

| Model | Architecture | Input Shape | Output | AI Hub |
|-------|-------------|-------------|--------|--------|
| [lama_dilated](lama_dilated/) | LaMa (Large Mask inpainting) with dilated convolutions | NHWC `[1, 512, 512, 3]` + mask `[1, 512, 512, 1]` | Inpainted image | [Link](https://aihub.qualcomm.com/compute/models/lama_dilated) |
| [aotgan](aotgan/) | AOT-GAN (Aggregated Contextual Transformations GAN) | NHWC `[1, 512, 512, 3]` + mask `[1, 512, 512, 1]` | Inpainted image | [Link](https://aihub.qualcomm.com/compute/models/aotgan) |

Both models take an image and a binary mask (white = region to fill) and output the inpainted result.

## Quick Start

```bash
cd qai-appbuilder\samples

# LaMa inpainting
python run_inference.py --model lama_dilated

# AOT-GAN inpainting
python run_inference.py --model aotgan
```

The default input files are `assets/input.png` and `assets/mask.png` in each model directory.
