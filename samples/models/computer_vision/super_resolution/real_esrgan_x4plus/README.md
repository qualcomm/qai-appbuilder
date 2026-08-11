# Real-ESRGAN x4plus — Image Super-Resolution on Snapdragon NPU

## Overview

**Real-ESRGAN x4plus** upscales images by 4× using a deep residual network, running on the Snapdragon NPU (HTP) via QAI AppBuilder. Supports multiple model formats: `.bin` (precompiled HTP binary), `.dlc` (float DLC), and `.onnx` (FP16, auto-generated).

- **Task**: Image Super-Resolution (4× upscaling)
- **Input**: Any RGB image
- **Output**: 4× upscaled image
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux
- **Runtime**: HTP (NPU), CPU, or GPU
- **AI Hub Model**: [real_esrgan_x4plus](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus)

## Model Architecture

- **Architecture**: RRDBNet (Residual-in-Residual Dense Block Network), 23 RRDB blocks
- **Input**: Tiled image patches (default 512×512 or 128×128)
- **Output**: 4× upscaled patches, stitched together

## Requirements

```
pip install Pillow numpy
```

For ONNX mode:
```
pip install torch onnx onnxconverter-common
```

## Quick Start

```bash
cd qai-appbuilder\samples

# Default (float DLC, auto-downloaded)
python run_inference.py --model real_esrgan_x4plus --args "--input_image_path path\to\image.jpg"

# Precompiled HTP binary
python run_inference.py --model real_esrgan_x4plus --args "--bin --input_image_path path\to\image.jpg"

# FP16 ONNX (auto-generated from PyTorch weights)
python run_inference.py --model real_esrgan_x4plus --args "--onnx --input_image_path path\to\image.jpg"

# w8a8 quantized DLC
python run_inference.py --model real_esrgan_x4plus --args "--w8a8 --input_image_path path\to\image.jpg"
```

Or run directly:
```bash
python models\computer_vision\super_resolution\real_esrgan_x4plus\python\real_esrgan_x4plus.py --input_image_path path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--output_image_path` | `python/output.png` | Path to save the output image |
| `--bin` | False | Use precompiled HTP context binary |
| `--dlc` | True (default) | Use float DLC model |
| `--onnx` | False | Use FP16 ONNX via OnnxRuntimeContext |
| `--w8a8` | False | Use w8a8 quantized DLC |
| `--cpu` | False | Use CPU runtime |
| `--gpu` | False | Use GPU runtime |
| `--no_show` | False | Don't open image viewer after inference |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run to `models/`:
- `real_esrgan_x4plus.dlc` — float DLC (default)
- `real_esrgan_x4plus.bin` — precompiled HTP binary (with `--bin`)
- `real_esrgan_x4plus.onnx` — FP16 ONNX (auto-generated with `--onnx`)
