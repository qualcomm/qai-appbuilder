# OpenAI CLIP — Image-Text Similarity on Snapdragon NPU

## Overview

**OpenAI CLIP** (Contrastive Language-Image Pre-Training) is a multimodal model that computes similarity scores between images and text descriptions, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It can be used for zero-shot image classification, image search by text query, and cross-modal retrieval.

- **Task**: Image-Text Similarity / Zero-Shot Image Classification
- **Input**: Multiple images + text query
- **Output**: Similarity scores between each image and the text query
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **Pretrained Weights**: ViT-B/16
- **AI Hub Model**: [openai_clip](https://aihub.qualcomm.com/compute/models/openai_clip)

## Model Architecture

OpenAI CLIP uses a joint image-text encoder:
- **Input 1** (image): `float32[1, 224, 224, 3]` (NHWC format)
- **Input 2** (text): `int32[1, 77]` (tokenized text, CLIP tokenizer)
- **Output**: `float32[1]` — cosine similarity score between image and text

## Requirements

```
pip install openai-clip torch
```

## Quick Start

```bash
cd qai-appbuilder\samples
python Multimodal\Image_Classification\openai_clip\openai_clip.py --text "mountain"
```

With custom images directory:
```bash
python Multimodal\Image_Classification\openai_clip\openai_clip.py --images_dir path\to\images --text "a dog playing in the park"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--images_dir` | `images/` (auto-downloaded) | Directory containing input images |
| `--text` | `"camping under the stars"` | Text query for image search |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and sample images are automatically downloaded on first run:
- `openai_clip.bin` — QNN model binary
- `images/image1.jpg`, `images/image2.jpg`, `images/image3.jpg` — sample images

The CLIP ViT-B/16 weights are downloaded from OpenAI on first run (for preprocessing).

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input images** | Directory of JPG/PNG files | Any size, resized to 224×224 |
| **Input text** | Text string | Query text (max 77 tokens) |
| **Output** | Similarity scores | Cosine similarity score for each image |

## Pipeline Details

```
Images directory + text query
    ↓ Load all images from directory
    ↓ For each image:
      ↓ CLIP image preprocessor → [1, 3, 224, 224]
      ↓ np.transpose → NHWC: [1, 224, 224, 3]
    ↓ CLIP tokenizer → [1, 77]
    ↓ For each image:
      ↓ OpenAI CLIP (NPU): image + text → similarity score
Similarity scores [float32]
    ↓ argmax → most relevant image
Display most relevant image
```

## Example Output

```
SOC_ID: None
Searching images by prompt: mountain
    Image with name: image1.jpg has a similarity score=[0.2345]
    Image with name: image2.jpg has a similarity score=[0.8912]
    Image with name: image3.jpg has a similarity score=[0.1234]
Displaying the most relevant image
```

## Use Cases

- **Image search**: Find the most relevant image for a text query
- **Zero-shot classification**: Classify images without training data
- **Content moderation**: Check if image content matches a description
- **Visual question answering**: Score image-question pairs

## Notes

- The model computes similarity for each image independently (not batch).
- The CLIP ViT-B/16 preprocessor is used for image normalization (from `openai-clip` package).
- The most relevant image (highest similarity score) is displayed automatically.
- For text-only embeddings, see [nomic_embed_text](../nomic_embed_text/).
