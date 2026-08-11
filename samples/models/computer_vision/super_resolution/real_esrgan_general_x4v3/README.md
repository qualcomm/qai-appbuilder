# Real-ESRGAN General x4v3 — Image Super-Resolution on Snapdragon NPU

## Overview

**Real-ESRGAN General x4v3** upscales images by 4× using a general-purpose super-resolution model, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It is optimized for general photographic content.

- **Task**: Image Super-Resolution (4× upscaling)
- **Input**: Any RGB image
- **Output**: 4× upscaled image
- **Platform**: Windows on Snapdragon (WoS), ARM64 Linux
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [real_esrgan_general_x4v3](https://aihub.qualcomm.com/compute/models/real_esrgan_general_x4v3)

## Model Architecture

- **Architecture**: Real-ESRGAN General v3 (optimized for general content)
- **Input size**: 512×512 (WoS) or 128×128 (Linux)
- **Output**: 4× upscaled image

## Requirements

```
pip install Pillow numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model real_esrgan_general_x4v3 --args "--input_image_path path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\super_resolution\real_esrgan_general_x4v3\python\real_esrgan_general_x4v3.py --input_image_path path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--output_image_path` | `python/output.jpg` | Path to save the output image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `real_esrgan_general_x4v3.bin` — QNN model binary
