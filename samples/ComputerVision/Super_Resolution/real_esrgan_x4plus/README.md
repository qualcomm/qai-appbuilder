# Real-ESRGAN x4plus — Image Super-Resolution on Snapdragon NPU

## Overview

**Real-ESRGAN x4plus** is a state-of-the-art image super-resolution model that upscales images by 4× using a deep residual dense network (RRDBNet), running on the Snapdragon NPU (HTP) via QAI AppBuilder. It supports multiple model formats (DLC, BIN, ONNX) and runtimes (HTP, GPU, CPU).

- **Task**: Image Super-Resolution (4× upscale)
- **Input**: Low-resolution RGB image
- **Output**: 4× upscaled high-resolution image
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (default), GPU, CPU
- **Model Formats**: Float DLC (default), w8a8 quantized DLC, precompiled BIN, FP16 ONNX
- **AI Hub Model**: [real_esrgan_x4plus](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus)

## Model Architecture

Real-ESRGAN x4plus uses RRDBNet (Residual-in-Residual Dense Block Network):
- **Input**: `float32[1, H, W, 3]` (NHWC) or `float32[1, 3, H, W]` (NCHW, auto-detected)
- **Output**: `float32[1, 4H, 4W, 3]` or `float32[1, 3, 4H, 4W]` (4× upscaled)
- **Default tile size**: 128×128 (DLC/BIN) or 512×512 (ONNX dynamic)
- **Architecture**: 23 RRDB blocks, 64 feature channels

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Super_Resolution\real_esrgan_x4plus\real_esrgan_x4plus.py
```

With custom input/output:
```bash
python ComputerVision\Super_Resolution\real_esrgan_x4plus\real_esrgan_x4plus.py --input_image_path input.jpg --output_image_path output.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `input.jpg` | Path to the input image |
| `--output_image_path` | `output.png` | Path to save the upscaled output image |
| `--bin` | False | Use precompiled HTP context binary (.bin) |
| `--dlc` | False | Use float DLC model (default behavior) |
| `--onnx` | False | Use FP16 ONNX model via OnnxRuntimeContext (auto-generated if absent) |
| `--w8a8` | False | Use w8a8 quantized DLC (combine with `--dlc`) |
| `--cpu` | False | Use CPU runtime instead of HTP |
| `--gpu` | False | Use GPU runtime instead of HTP |
| `--no_show` | False | Do not open image viewer after inference |
| `--chipset` | Auto-detected | SoC ID for hub-model download (Linux only) |

## Model Download

Models are automatically downloaded on first run:
- **Float DLC** (default): Downloaded from public S3 URL
- **w8a8 DLC**: Downloaded from public S3 URL
- **BIN**: Downloaded from Qualcomm AI Hub
- **ONNX**: Auto-generated from PyTorch weights (requires `torch`, `onnx`, `onnxconverter-common`)

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, tiled at model input size |
| **Output** | Upscaled image (PNG) | 4× the input resolution |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open → convert RGB
    ↓ pil_resize_pad → model input size (128 or 512)
    ↓ normalize [0, 255] → [0.0, 1.0]
    ↓ auto-detect layout (NCHW or NHWC)
    ↓ Real-ESRGAN x4plus (NPU/GPU/CPU)
Output tensor [4H, 4W, 3] or [3, 4H, 4W]
    ↓ clip [0, 1] → uint8 [0, 255]
    ↓ pil_undo_resize_pad → 4× original size
Upscaled image (PNG)
```

## Runtime Comparison

| Mode | Command | Notes |
| ---- | ------- | ----- |
| HTP + float DLC (default) | `python real_esrgan_x4plus.py` | Best for WoS |
| HTP + w8a8 DLC | `python real_esrgan_x4plus.py --dlc --w8a8` | Quantized, faster |
| HTP + BIN | `python real_esrgan_x4plus.py --bin` | Precompiled, fastest |
| HTP + ONNX | `python real_esrgan_x4plus.py --onnx` | FP16, auto-generated |
| CPU + DLC | `python real_esrgan_x4plus.py --cpu` | For debugging |
| GPU + DLC | `python real_esrgan_x4plus.py --gpu` | GPU acceleration |

## Notes

- On **x86 Windows**: Always uses CPU + DLC (HTP/GPU not available).
- The `--bin` and `--dlc` flags are mutually exclusive.
- The `--w8a8` flag implies `--dlc`.
- ONNX generation requires: `pip install torch onnx onnxconverter-common`
- The model auto-detects input layout (NCHW vs NHWC) from the model's input shape.
