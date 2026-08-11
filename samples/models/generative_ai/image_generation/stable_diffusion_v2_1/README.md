# Stable Diffusion v2.1 — Text-to-Image on Snapdragon NPU

## Overview

**Stable Diffusion v2.1** generates 512×512 images from text prompts, running on the Snapdragon NPU (HTP) via QAI AppBuilder. Uses `QNNShareMemory` for efficient NPU memory management.

- **Task**: Text-to-Image Generation
- **Output**: 512×512 RGB image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [stable_diffusion_v2_1](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1)

## Model Architecture

3-component pipeline:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Text Encoder | `stable_diffusion_v2_1_TextEncoderQuantizable.bin` | OpenCLIP text encoder (1024-dim), tokenizer: `stabilityai/stable-diffusion-2-1-base` |
| UNet | `stable_diffusion_v2_1_UnetQuantizable.bin` | Denoising UNet with QNNShareMemory |
| VAE Decoder | `stable_diffusion_v2_1_VaeDecoderQuantizable.bin` | Decodes latent → 512×512 image |

## Requirements

```
pip install torch transformers diffusers
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'spectacular view of northern lights from Alaska'"
```

Or run directly:
```bash
python models\generative_ai\image_generation\stable_diffusion_v2_1\python\stable_diffusion_v2_1.py --prompt "a cat"
```

## Manual Model Download

> **Note:** Models must be downloaded manually from Qualcomm AI Hub.

1. Go to [AI Hub — Stable Diffusion v2.1](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1)
2. Select **Runtime: Qualcomm® AI Engine Direct**, **Device: Snapdragon® X Elite**
3. Download: `TextEncoderQuantizable`, `UnetQuantizable`, `VaeDecoderQuantizable`
4. Save `.bin` files to `models/`

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--prompt` | Built-in demo prompt | Text prompt for image generation |
| `--negative_prompt` | `""` | Negative prompt |
| `--steps` | `20` | Number of diffusion steps |
| `--guidance` | `7.5` | Text guidance scale (5.0–15.0) |
| `--seed` | `-1` (random) | Random seed |

## Notes

- Output images are saved to `python/images/`.
- Only English prompts are supported.
- Each image takes ~20–30 seconds on the NPU.
