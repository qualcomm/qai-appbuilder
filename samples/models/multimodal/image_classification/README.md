# Image Classification (Multimodal)

Vision-language models for zero-shot image classification using natural language descriptions.

## Models

| Model | Architecture | Task | AI Hub |
|-------|-------------|------|--------|
| [openai_clip](openai_clip/) | CLIP ViT-B/16 (image encoder + text encoder) | Images + text query → similarity scores → most relevant image | [Link](https://aihub.qualcomm.com/compute/models/openai_clip) |

Unlike traditional classifiers, CLIP can classify images into any category described in natural language without retraining.

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model openai_clip --args "--text 'mountain landscape'"
```
