# QuickSRNet Medium — Lightweight Image Super-Resolution on Snapdragon NPU

## Overview

**QuickSRNet Medium** is a lightweight and fast image super-resolution model that upscales images by 4×, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It is designed for real-time applications where speed is prioritized.

- **Task**: Image Super-Resolution (4× upscale)
- **Input**: Low-resolution RGB image
- **Output**: 4× upscaled high-resolution image
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [quicksrnetmedium](https://aihub.qualcomm.com/compute/models/quicksrnetmedium)

## Model Architecture

QuickSRNet Medium uses a compact super-resolution network:
- **Input**: `float32[1, H, W, 3]` (NHWC format, values in [0, 1])
- **Output**: `float32[4H × 4W × 3]` (flat, values in [0, 1])
- **Input size**: 512×512 on WoS/x86 Windows; 128×128 on Linux

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Super_Resolution\quicksrnetmedium\quicksrnetmedium.py
```

With a custom image:
```bash
python ComputerVision\Super_Resolution\quicksrnetmedium\quicksrnetmedium.py --image path\to\image.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.png` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and sample image are automatically downloaded on first run:
- `quicksrnetmedium.bin` — QNN model binary
- `super_resolution_input.jpg` — sample low-resolution image for testing

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized+padded to 512×512 (WoS) or 128×128 (Linux) |
| **Output** | Upscaled image (`output.png`) | 4× the input resolution |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open
    ↓ pil_resize_pad → IMAGE_SIZE×IMAGE_SIZE (preserves aspect ratio)
    ↓ np.array → normalize [0, 255] → [0.0, 1.0]
    ↓ QuickSRNet Medium (NPU)
Output [flat float32]
    ↓ reshape → [IMAGE_SIZE×4, IMAGE_SIZE×4, 3]
    ↓ clip [0, 1] → uint8 [0, 255]
    ↓ pil_undo_resize_pad → 4× original size
output.png
```

## Platform-Specific Image Sizes

| Platform | Input Size | Output Size |
| -------- | ---------- | ----------- |
| WoS (ARM64 Windows) | 512×512 | 2048×2048 |
| x86 Windows | 512×512 | 2048×2048 |
| ARM64 Linux | 128×128 | 512×512 |
| x86 Linux | 128×128 | 512×512 |

## Notes

- The model uses `pil_resize_pad` to preserve aspect ratio during preprocessing.
- The output is restored to the original aspect ratio using `pil_undo_resize_pad`.
- On WoS, a specific hub model ID (`HUB_ID_H`) is used for download.
- For higher quality super-resolution, see [real_esrgan_x4plus](../real_esrgan_x4plus/) or [real_esrgan_general_x4v3](../real_esrgan_general_x4v3/).
