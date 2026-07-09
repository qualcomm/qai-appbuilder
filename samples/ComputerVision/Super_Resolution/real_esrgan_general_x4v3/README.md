# Real-ESRGAN General x4v3 — Image Super-Resolution on Snapdragon NPU

## Overview

**Real-ESRGAN General x4v3** is a general-purpose image super-resolution model that upscales images by 4×, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It is optimized for general photographic content and produces high-quality upscaled results.

- **Task**: Image Super-Resolution (4× upscale)
- **Input**: Low-resolution RGB image (resized to 512×512 on WoS, 128×128 on Linux)
- **Output**: 4× upscaled high-resolution image (2048×2048 on WoS, 512×512 on Linux)
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [real_esrgan_general_x4v3](https://aihub.qualcomm.com/compute/models/real_esrgan_general_x4v3)

## Model Architecture

Real-ESRGAN General x4v3 uses a compact RRDBNet variant:
- **Input**: `float32[1, H, W, 3]` (HWC format, values in [0, 1])
- **Output**: `float32[4H × 4W × 3]` (flat, values in [0, 1])
- **Input size**: 512×512 on WoS/x86 Windows; 128×128 on Linux

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Super_Resolution\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py
```

> Note: This script runs directly without command-line arguments. Place your input image as `input.jpg` in the script directory.

## Model Download

The model is automatically downloaded on first run:
- `real_esrgan_general_x4v3.bin` — QNN model binary

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | `input.jpg` in script directory | Resized to 512×512 (WoS) or 128×128 (Linux) |
| **Output** | `output.jpg` in script directory | 4× upscaled image |

## Pipeline Details

```
input.jpg
    ↓ PIL.Image.open
    ↓ resize IMAGE_SIZE × CenterCrop → [IMAGE_SIZE, IMAGE_SIZE]
    ↓ PILToTensor → float32 / 255
    ↓ np.transpose → HWC: IMAGE_SIZE×IMAGE_SIZE×3
    ↓ Real-ESRGAN General x4v3 (NPU)
Output [flat float32]
    ↓ reshape → [IMAGE_SIZE×4, IMAGE_SIZE×4, 3]
    ↓ clip [0, 1] → uint8 [0, 255]
output.jpg
```

## Platform-Specific Image Sizes

| Platform | Input Size | Output Size |
| -------- | ---------- | ----------- |
| WoS (ARM64 Windows) | 512×512 | 2048×2048 |
| x86 Windows | 512×512 | 2048×2048 |
| ARM64 Linux | 128×128 | 512×512 |
| x86 Linux | 128×128 | 512×512 |

## Notes

- The input image is center-cropped to a square before resizing.
- The output is displayed and saved as `output.jpg`.
- For more flexible options (DLC/BIN/ONNX, custom paths), see [real_esrgan_x4plus](../real_esrgan_x4plus/).
