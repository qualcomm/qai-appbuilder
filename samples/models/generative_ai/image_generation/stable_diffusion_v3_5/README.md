# Stable Diffusion v3.5 Medium — Text-to-Image on Snapdragon NPU

## Overview

**Stable Diffusion v3.5 Medium** generates 1024×1024 images from text prompts using the MM-DiT (Multimodal Diffusion Transformer) architecture, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Text-to-Image Generation
- **Output**: 1024×1024 RGB image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)

## Model Architecture

4-component pipeline:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Text Encoder (CLIP-L) | `text_encoder.serialized.bin` | CLIP-L text encoder (77 tokens) |
| Text Encoder (CLIP-G) | `text_encoder_2.serialized.bin` | CLIP-G text encoder (77 tokens) |
| MM-DiT Transformer | `transformer.serialized.bin` | Multimodal Diffusion Transformer (denoising) |
| VAE Decoder | `vae_decoder.serialized.bin` | Decodes latent → 1024×1024 image |

**Scheduler**: FlowMatch Euler Discrete (shift=3.0)

## Requirements

```
pip install torch transformers diffusers
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model stable_diffusion_v3_5 --args "--prompt 'a cat holding a sign that says hello world' --steps 8"
```

Or run directly:
```bash
python models\generative_ai\image_generation\stable_diffusion_v3_5\python\stable_diffusion_v3_5.py --prompt "a cat" --steps 8
```

## Model Download

Models are automatically downloaded on first run to `models/`. The script detects your device (Snapdragon X Elite or X2 Elite) and downloads the corresponding zip from aidevhome.com.

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--prompt` | Built-in demo prompt | Text prompt for image generation |
| `--negative_prompt` | `""` | Negative prompt |
| `--steps` | `8` | Number of diffusion steps (8 recommended) |
| `--cfg` | `3.5` | Classifier-free guidance scale |
| `--seed` | `42` | Random seed |
| `--output` | `python/output.png` | Output image path |

## Notes

- Output is saved to `python/output.png` by default.
- 8 steps is recommended for SD v3.5 (FlowMatch scheduler).
- Only English prompts are supported.
