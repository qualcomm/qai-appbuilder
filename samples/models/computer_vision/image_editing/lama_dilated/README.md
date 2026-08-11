# LaMa Dilated — Image Inpainting on Snapdragon NPU

## Overview

**LaMa Dilated** is an image inpainting model that fills masked regions with realistic content (object removal), running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Image Inpainting (object removal)
- **Input**: RGB image + binary mask (white = region to fill)
- **Output**: Inpainted image with masked region filled
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [lama_dilated](https://aihub.qualcomm.com/compute/models/lama_dilated)

## Model Architecture

- **Architecture**: LaMa (Large Mask inpainting) with dilated convolutions and Fourier convolution layers
- **Input shape**: Image NHWC `[1, 512, 512, 3]` + Mask NHWC `[1, 512, 512, 1]`
- **Output**: Inpainted image `[1, 512, 512, 3]`

## Requirements

```
pip install Pillow numpy scikit-image
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model lama_dilated
```

Or run directly:
```bash
python models\computer_vision\image_editing\lama_dilated\python\lama_dilated.py
```

The default input files are `assets/input.png` and `assets/mask.png`.

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.png` | Path to the input image |
| `--mask_image_path` | `assets/mask.png` | Path to the binary mask (white = fill region) |
| `--output_image_path` | `python/output.png` | Path to save the inpainted image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `lama_dilated.bin` — QNN model binary

## Notes

- The mask should be a binary image where white (255) indicates the region to fill.
- Input images are automatically resized to 512×512 for inference.
