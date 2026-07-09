# Stable Diffusion v3.5 Medium — Text-to-Image Generation on Snapdragon NPU

## Overview

**Stable Diffusion v3.5 Medium** is a state-of-the-art latent diffusion model for high-resolution text-to-image generation, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It generates 1024×1024 images from text prompts using a 4-component pipeline (CLIP-L + CLIP-G text encoders + Transformer + VAE Decoder).

- **Task**: Text-to-Image Generation
- **Output Resolution**: 1024×1024 pixels
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: Custom download from aidevhome.com

## Model Architecture

Stable Diffusion v3.5 Medium uses a 4-component pipeline:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Text Encoder (CLIP-L) | `text_encoder.serialized.bin` | CLIP-L: text → hidden states [1, 77, 768] + pooled [768] |
| Text Encoder (CLIP-G) | `text_encoder_2.serialized.bin` | CLIP-G: text → hidden states [1, 77, 1280] + pooled [1280] |
| Transformer | `transformer.serialized.bin` | MM-DiT transformer: denoising in latent space [1, 4096, 64] |
| VAE Decoder | `vae_decoder.serialized.bin` | VAE decoder: latent [1, 16, 128, 128] → image [1, 1024, 1024, 3] |

**Scheduler**: FlowMatchEulerDiscreteScheduler (shift=3.0)  
**Tokenizers**: CLIP-L and CLIP-G tokenizers (stored in `models/tokenizer/` and `models/tokenizer_2/`)  
**Time embedding**: `time_text_embed.pt` (CombinedTimestepTextProjEmbeddings)

## Requirements

```
pip install transformers diffusers torch
```

## Quick Start

```bash
cd qai-appbuilder\samples
python GenerativeAI\Image_Generation\stable_diffusion_v3_5\stable_diffusion_v3_5.py --prompt "A cat holding a sign that says hello world"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--prompt` | `"A cat holding a sign that says hello world"` | Text prompt for image generation |
| `--negative_prompt` | `""` | Negative prompt (what to avoid) |
| `--steps` | `8` | Number of denoising steps |
| `--cfg` | `3.5` | Classifier-free guidance scale |
| `--seed` | `42` | Random seed for reproducibility |
| `--output` | `output.png` | Output image path |
| `--chipset` | Auto-detected | SoC ID |

## Model Download

The model is automatically downloaded on first run. The script detects your device (Snapdragon X Elite or X2 Elite) and downloads the corresponding model zip:
- **Snapdragon X Elite**: `sd3.5_qnn_for_windows-8380.zip` (~several GB)
- **Snapdragon X2 Elite**: `sd3.5_qnn_for_windows-8480.zip` (~several GB)

The download uses multi-threaded chunked downloading (8 threads, 16MB chunks) for faster speeds.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Text string | English text prompt |
| **Output** | PNG image | 1024×1024 generated image |

## Pipeline Details

```
Text prompt + negative prompt
    ↓ CLIP-L tokenizer + CLIP-G tokenizer
    ↓ Text Encoder CLIP-L (NPU) → hidden_states [1,77,768] + pooled [768]
    ↓ Text Encoder CLIP-G (NPU) → hidden_states [1,77,1280] + pooled [1280]
    ↓ Concatenate: encoder_hidden_states [1, 160, 4096]
    ↓ Concatenate: pooled_projections [1, 2048]
    ↓ Initialize random latent [1, 16, 128, 128]
    ↓ FlowMatchEulerDiscreteScheduler (8 steps)
    ↓ For each step:
      ↓ get_temb (CombinedTimestepTextProjEmbeddings)
      ↓ Transformer (NPU) × 2 (conditional + unconditional)
      ↓ CFG: noise = uncond + 3.5 × (cond - uncond)
      ↓ unpatchify_noise_pred → [1, 16, 128, 128]
      ↓ Scheduler step → updated latent
    ↓ preprocess_latent_for_vae: scale + shift
    ↓ VAE Decoder (NPU)
Image [1, 1024, 1024, 3]
    ↓ normalize + clip → uint8
output.png (1024×1024)
```

## Latent Space Parameters

| Parameter | Value |
| --------- | ----- |
| Latent H × W | 128 × 128 |
| Latent channels | 16 |
| VAE scale | 1.5305 |
| VAE shift | 0.0609 |
| Patch size | 2 |
| Context dim | 4096 |

## Notes

- The model download is large (several GB). Ensure sufficient disk space.
- Multi-threaded download with resume support (re-run if interrupted).
- The transformer uses patchified latents: 128×128 latent → 4096 patches of size 2×2×16=64.
- Redundant files (whl packages, libs directory) are automatically cleaned up after extraction.
- For lower resolution (512×512), see [stable_diffusion_v1_5](../stable_diffusion_v1_5/) or [stable_diffusion_v2_1](../stable_diffusion_v2_1/).
