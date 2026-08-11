# QuickSRNet Medium — Lightweight Image Super-Resolution on Snapdragon NPU

## Overview

**QuickSRNet Medium** is a lightweight image super-resolution model that upscales images by 4×, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It is faster and more efficient than Real-ESRGAN variants.

- **Task**: Image Super-Resolution (4× upscaling)
- **Input**: Any RGB image
- **Output**: 4× upscaled image
- **Platform**: Windows on Snapdragon (WoS), ARM64 Linux
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [quicksrnetmedium](https://aihub.qualcomm.com/compute/models/quicksrnetmedium)

## Requirements

```
pip install Pillow numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model quicksrnetmedium --args "--input_image_path path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\super_resolution\quicksrnet_medium\python\quicksrnetmedium.py --input_image_path path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--output_image_path` | `python/output.png` | Path to save the output image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `quicksrnetmedium.bin` — QNN model binary
