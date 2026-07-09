# Nomic Embed Text — Text Embedding on Snapdragon NPU

## Overview

**Nomic Embed Text** is a high-performance text embedding model that converts text into dense vector representations (embeddings), running on the Snapdragon NPU (HTP) via QAI AppBuilder. The embeddings can be used for semantic search, text similarity, clustering, and retrieval-augmented generation (RAG).

- **Task**: Text Embedding (Semantic Representation)
- **Embedding Dimension**: 768
- **Max Sequence Length**: 128 tokens
- **Input**: Text string
- **Output**: 768-dimensional embedding vector (saved as `.npy` file)
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [nomic_embed_text](https://aihub.qualcomm.com/compute/models/nomic_embed_text)

## Model Architecture

Nomic Embed Text is based on a BERT-style transformer encoder:
- **Input 1** (input_ids): `int32[1, 128]` — tokenized text (BERT tokenizer)
- **Input 2** (attention_mask): `int32[1, 128]` — attention mask
- **Output**: `float32[1, 768]` — text embedding vector

## Requirements

```
pip install transformers torch
```

## Quick Start

```bash
cd qai-appbuilder\samples
python Multimodal\Text_Generation\nomic_embed_text\nomic_embed_text.py
```

With custom text:
```bash
python Multimodal\Text_Generation\nomic_embed_text\nomic_embed_text.py --text "Hello, world!"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | `"hello!"` | Text to encode into an embedding |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run:
- `nomic_embed_text.bin` — QNN model binary

The BERT tokenizer (`bert-base-uncased`) is downloaded from HuggingFace on first run.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Text string | Any English text (truncated to 128 tokens) |
| **Output** | NumPy array (`embeddings.npy`) | 768-dimensional float32 embedding vector |

## Pipeline Details

```
Text string
    ↓ BERT tokenizer (bert-base-uncased, max_length=128)
input_ids [int32, shape: (1, 128)]
attention_mask [int32, shape: (1, 128)]
    ↓ Nomic Embed Text (NPU)
Embedding [float32, shape: (1, 768)]
    ↓ np.save → embeddings.npy
```

## Use Cases

The 768-dimensional embedding vectors can be used for:
- **Semantic search**: Find similar documents by cosine similarity
- **Text clustering**: Group similar texts together
- **RAG (Retrieval-Augmented Generation)**: Build a vector database for LLM context retrieval
- **Text classification**: Use embeddings as features for downstream classifiers
- **Duplicate detection**: Find near-duplicate texts

## Example Output

```
SOC_ID: None
Embeddings saved to: C:\...\embeddings.npy
Embeddings shape: (1, 768)
Embeddings:
[[ 0.0234  0.0156 -0.0089 ... -0.0123  0.0345  0.0012]]
```

## Notes

- Uses shared utilities from `common/_text_generation.py`.
- The tokenizer uses `bert-base-uncased` vocabulary (30522 tokens).
- Embeddings are L2-normalized for cosine similarity computation.
- For image-text similarity, see [openai_clip](../openai_clip/).
