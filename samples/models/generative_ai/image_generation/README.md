# Image Generation

Text-to-image generation models using Stable Diffusion, running on the Snapdragon NPU.

## Models

| Model | Architecture | Output | Steps | AI Hub |
|-------|-------------|--------|-------|--------|
| [stable_diffusion_v1_5](stable_diffusion_v1_5/) | TextEncoder (CLIP) + UNet + VAE | 512×512 image | 20 (default) | [Link](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5) |
| [stable_diffusion_v2_1](stable_diffusion_v2_1/) | TextEncoder (OpenCLIP) + UNet + VAE | 512×512 image | 20 (default) | [Link](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1) |
| [stable_diffusion_v3_5](stable_diffusion_v3_5/) | CLIP-L + CLIP-G + MM-DiT Transformer + VAE | 1024×1024 image | 8 (default) | — |

## Quick Start

```bash
cd qai-appbuilder\samples

# SD v2.1 (requires manual model download)
python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'spectacular view of northern lights from Alaska'"

# SD v3.5 (auto-downloaded)
python run_inference.py --model stable_diffusion_v3_5 --args "--prompt 'a cat holding a sign that says hello world' --steps 8"
```

## Manual Model Download (SD v1.5 / v2.1)

1. Go to [AI Hub — Stable Diffusion v2.1](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1)
2. Select **Runtime: Qualcomm® AI Engine Direct**, **Device: Snapdragon® X Elite**
3. Download: `TextEncoderQuantizable`, `UnetQuantizable`, `VaeDecoderQuantizable`
4. Save `.bin` files to `stable-diffusion-v2-1/models/`
