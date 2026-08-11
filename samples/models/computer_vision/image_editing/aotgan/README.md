# AOT-GAN — Image Inpainting on Snapdragon NPU

## Overview

**AOT-GAN** (Aggregated Contextual Transformations GAN) is a GAN-based image inpainting model that fills masked regions with realistic content, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Image Inpainting (object removal)
- **Input**: RGB image + binary mask (white = region to fill)
- **Output**: Inpainted image with masked region filled
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [aotgan](https://aihub.qualcomm.com/compute/models/aotgan)

## Model Architecture

- **Architecture**: AOT-GAN with aggregated contextual transformations
- **Input shape**: Image NHWC `[1, 512, 512, 3]` + Mask NHWC `[1, 512, 512, 1]`
- **Output**: Inpainted image `[1, 512, 512, 3]`

## Requirements

```
pip install Pillow numpy scikit-image
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model aotgan
```

Or run directly:
```bash
python models\computer_vision\image_editing\aotgan\python\aotgan.py
```

The default input files are `assets/input.png` and `assets/mask.png`.

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.png` | Path to the input image |
| `--mask_image_path` | `assets/mask.png` | Path to the binary mask |
| `--output_image_path` | `python/output.png` | Path to save the inpainted image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `aotgan.bin` — QNN model binary
