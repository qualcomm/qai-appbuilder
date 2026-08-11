# OpenAI CLIP — Zero-Shot Image Classification on Snapdragon NPU

## Overview

**OpenAI CLIP** (Contrastive Language-Image Pre-Training) performs zero-shot image classification by computing similarity between images and text descriptions, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Zero-Shot Image Classification
- **Input**: Multiple images + text query
- **Output**: Similarity scores → most relevant image
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [openai_clip](https://aihub.qualcomm.com/compute/models/openai_clip)

## Model Architecture

- **Architecture**: CLIP ViT-B/16 (image encoder + text encoder)
- **Image encoder input**: `[1, 3, 224, 224]`
- **Text encoder input**: Tokenized text (max 77 tokens)
- **Output**: Cosine similarity scores between image and text embeddings

## Requirements

```
pip install openai-clip torch Pillow numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model openai_clip --args "--text 'mountain landscape'"
```

Or run directly:
```bash
python models\multimodal\image_classification\openai_clip\python\openai_clip.py --text "mountain landscape"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | `"camping under the stars"` | Text query for zero-shot classification |
| `--images_dir` | `assets/images/` (auto-downloaded) | Directory containing images to classify |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `openai_clip.bin` — QNN model binary

## Notes

- CLIP can classify images into any category described in natural language without retraining.
- Sample images are auto-downloaded to `assets/images/`.
