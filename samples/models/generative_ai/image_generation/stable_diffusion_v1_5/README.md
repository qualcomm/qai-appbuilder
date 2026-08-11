# Stable Diffusion v1.5 — Text-to-Image on Snapdragon NPU

## Overview

**Stable Diffusion v1.5** generates 512×512 images from text prompts, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Text-to-Image Generation
- **Output**: 512×512 RGB image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [stable_diffusion_v1_5](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5)

## Model Architecture

3-component pipeline:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Text Encoder | `stable_diffusion_v1_5_TextEncoderQuantizable.bin` | CLIP text encoder (768-dim), tokenizer: `openai/clip-vit-large-patch14` |
| UNet | `stable_diffusion_v1_5_UnetQuantizable.bin` | Denoising UNet (w8a16 quantization) |
| VAE Decoder | `stable_diffusion_v1_5_VaeDecoderQuantizable.bin` | Decodes latent → 512×512 image |

## Requirements

```
pip install torch transformers diffusers
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model stable_diffusion_v1_5 --args "--prompt 'a beautiful sunset over the ocean'"
```

Or run directly:
```bash
python models\generative_ai\image_generation\stable_diffusion_v1_5\python\stable_diffusion_v1_5.py --prompt "a cat"
```

## Manual Model Download

> **Note:** Models must be downloaded manually from Qualcomm AI Hub.

1. Go to [AI Hub — Stable Diffusion v1.5](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5)
2. Select **Runtime: Qualcomm® AI Engine Direct**, **Device: Snapdragon® X Elite**
3. Download: `TextEncoderQuantizable`, `UnetQuantizable`, `VaeDecoderQuantizable`
4. Save `.bin` files to `models/`

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--prompt` | Built-in demo prompt | Text prompt for image generation |
| `--negative_prompt` | `""` | Negative prompt |
| `--steps` | `20` | Number of diffusion steps |
| `--seed` | `-1` (random) | Random seed |

## Notes

- Output images are saved to `python/images/`.
- Only English prompts are supported.
