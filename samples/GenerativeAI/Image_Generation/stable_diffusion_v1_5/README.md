# Stable Diffusion v1.5 — Text-to-Image Generation on Snapdragon NPU

## Overview

**Stable Diffusion v1.5** is a latent diffusion model for text-to-image generation, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It generates 512×512 images from text prompts using a 3-component pipeline (TextEncoder + UNet + VAE Decoder).

- **Task**: Text-to-Image Generation
- **Output Resolution**: 512×512 pixels
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (Hexagon NPU)
- **Quantization**: w8a16 (weights int8, activations int16)
- **AI Hub Model**: [stable_diffusion_v1_5](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5)

## Model Architecture

Stable Diffusion v1.5 uses a 3-component pipeline:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Text Encoder | `stable_diffusion_v1_5_w8a16_quantized-textencoderquantizable-qualcomm_snapdragon_x_elite.bin` | CLIP text encoder: tokenized text → text embeddings [1, 77, 768] |
| UNet | `stable_diffusion_v1_5_w8a16_quantized-unetquantizable-qualcomm_snapdragon_x_elite.bin` | Denoising UNet: latent + timestep + text embedding → denoised latent [1, 64, 64, 4] |
| VAE Decoder | `stable_diffusion_v1_5_w8a16_quantized-vaedecoderquantizable-qualcomm_snapdragon_x_elite.bin` | VAE decoder: latent → image [512, 512, 3] |

**Scheduler**: DPMSolverMultistepScheduler  
**Tokenizer**: CLIP ViT-L/14 (`openai/clip-vit-large-patch14`)

## Requirements

```
pip install transformers diffusers torch
```

## Quick Start

```bash
cd qai-appbuilder\samples
python GenerativeAI\Image_Generation\stable_diffusion_v1_5\stable_diffusion_v1_5.py --prompt "spectacular view of northern lights from Alaska"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--prompt` | `"spectacular view of northern lights from Alaska"` | Text prompt for image generation |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run from Qualcomm AI Hub. Three model files are required:
- `*-textencoderquantizable-*.bin`
- `*-unetquantizable-*.bin`
- `*-vaedecoderquantizable-*.bin`

**Manual download**: If automatic download fails, download from [AI Hub](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5) and save to `models/` directory.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Text string | English text prompt (any length, truncated to 77 tokens) |
| **Output** | JPEG image | 512×512 generated image, saved to `images/` directory |

## Pipeline Details

```
Text prompt
    ↓ CLIPTokenizer (max_length=77)
Token IDs [float32, shape: (77,)]
    ↓ Text Encoder (NPU)
Text embeddings [float32, shape: (1, 77, 768)]
    ↓ Initialize random latent [1, 64, 64, 4] (from seed)
    ↓ DPMSolverMultistepScheduler (20 steps)
    ↓ For each step:
      ↓ UNet (NPU) × 2 (unconditional + conditional)
      ↓ Classifier-free guidance: noise = uncond + 7.5 × (cond - uncond)
      ↓ Scheduler step → updated latent
    ↓ VAE Decoder (NPU)
Image [float32, shape: (512, 512, 3)]
    ↓ clip × 255 → uint8
Generated image (JPEG)
```

## Parameters

| Parameter | Default | Range | Description |
| --------- | ------- | ----- | ----------- |
| Steps | 20 | 20, 30, 50 | Number of denoising steps (more = better quality, slower) |
| Text guidance | 7.5 | 5.0–15.0 | Classifier-free guidance scale (higher = more prompt-adherent) |
| Seed | Random | Any int64 | Random seed for reproducibility |

## Example Output

```
Step 0 Running...
Step 1 Running...
...
Step 19 Running...
Image saved to 'images\2024_01_01_12_00_00_1234567890_512.jpg'
time consumes for inference 45.2(s)
```

## Notes

- The tokenizer (`openai/clip-vit-large-patch14`) is downloaded from HuggingFace on first run.
- Set `HF_ENDPOINT=https://hf-api.gitee.com` if HuggingFace is not accessible.
- Generated images are saved to `images/` with timestamp + seed + size in filename.
- For higher resolution (1024×1024), see [stable_diffusion_v3_5](../stable_diffusion_v3_5/).
