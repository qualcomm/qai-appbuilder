# LaMa Dilated — Image Inpainting on Snapdragon NPU

## Overview

**LaMa Dilated** (Large Mask inpainting) is an image inpainting model that fills in masked regions of an image with realistic content, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It uses dilated convolutions to capture large-scale context for high-quality inpainting.

- **Task**: Image Inpainting (object removal / region filling)
- **Input**: RGB image + binary mask (512×512)
- **Output**: Inpainted image with masked regions filled
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [lama_dilated](https://aihub.qualcomm.com/compute/models/lama_dilated)

## Model Architecture

LaMa Dilated uses a fully convolutional network with dilated convolutions:
- **Input 1** (image): `float32[1, 512, 512, 3]` (NHWC, masked image)
- **Input 2** (mask): `float32[1, 512, 512, 1]` (NHWC, binary mask: 1=region to fill)
- **Output**: `float32[512 × 512 × 3]` (flat, inpainted image)

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Image_Editing\lama_dilated\lama_dilated.py
```

> Note: This script runs directly. Place your input image as `input.png` and mask as `mask.png` in the script directory.

## Model Download

The model is automatically downloaded on first run:
- `lama_dilated.bin` — QNN model binary

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input image** | `input.png` | RGB image, resized to 512×512 |
| **Input mask** | `mask.png` | Binary mask (white=fill, black=keep), resized to 512×512 |
| **Output** | `output.png` | Inpainted image with masked regions filled |

## Preparing Input Files

1. **Image** (`input.png`): The original image you want to edit (any size, will be resized to 512×512)
2. **Mask** (`mask.png`): A binary mask where:
   - **White pixels** (value=1): Regions to be filled/inpainted
   - **Black pixels** (value=0): Regions to keep unchanged

## Pipeline Details

```
input.png + mask.png
    ↓ PIL.Image.open
    ↓ preprocess_inputs:
      - resize + center crop → 512×512
      - image_masked = image × (1 - mask) + mask  (apply mask)
    ↓ np.transpose → NHWC: 1×512×512×3 (image), 1×512×512×1 (mask)
    ↓ LaMa Dilated (NPU)
Output [flat float32]
    ↓ reshape → [512, 512, 3]
    ↓ clip [0, 1] → PIL Image
output.png
```

## Example Use Cases

- **Object removal**: Paint a white mask over an unwanted object
- **Watermark removal**: Mask the watermark area
- **Image restoration**: Fill in damaged or missing regions
- **Background completion**: Remove foreground objects

## Notes

- The mask preprocessing uses the same `preprocess_inputs` function as `aotgan`.
- The input image is center-cropped to 512×512 (not padded).
- Both input and output are displayed and saved.
- For a similar model with different architecture, see [aotgan](../aotgan/).
