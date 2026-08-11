# Generative AI Models

This directory contains Python inference samples for generative AI models running on the Snapdragon NPU (HTP) via QAI AppBuilder.

## Models

| Subcategory | Model | Task | Output Size |
|-------------|-------|------|-------------|
| [Image Generation](image_generation/) | [stable_diffusion_v1_5](image_generation/stable_diffusion_v1_5/) | Text → image (SD v1.5) | 512×512 |
| [Image Generation](image_generation/) | [stable_diffusion_v2_1](image_generation/stable_diffusion_v2_1/) | Text → image (SD v2.1) | 512×512 |
| [Image Generation](image_generation/) | [stable_diffusion_v3_5](image_generation/stable_diffusion_v3_5/) | Text → image (SD v3.5 Medium) | 1024×1024 |

## Quick Start

```bash
cd qai-appbuilder\samples

python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'a beautiful sunset over the ocean'"
python run_inference.py --model stable_diffusion_v3_5 --args "--prompt 'a cat holding a sign' --steps 8"
```

## Dependencies

```
pip install torch transformers diffusers
```

> **Note:** SD v1.5 and v2.1 models must be downloaded manually from [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1). SD v3.5 is auto-downloaded.
