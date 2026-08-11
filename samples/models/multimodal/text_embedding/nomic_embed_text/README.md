# NomicEmbedText — Text Embedding on Snapdragon NPU

## Overview

**NomicEmbedText** converts text into dense 768-dimensional embedding vectors for semantic search and RAG (Retrieval-Augmented Generation), running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Text Embedding
- **Input**: UTF-8 text string
- **Output**: 768-dimensional embedding vector (saved as `embeddings.npy`)
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [nomic_embed_text](https://aihub.qualcomm.com/compute/models/nomic_embed_text)

## Model Architecture

- **Architecture**: NomicBERT (BERT-based transformer)
- **Input**: Tokenized text (max sequence length)
- **Output**: `[1, 768]` embedding vector

## Requirements

```
pip install transformers torch numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model nomic_embed_text --args "--text 'hello world'"
```

Or run directly:
```bash
python models\multimodal\text_embedding\nomic_embed_text\python\nomic_embed_text.py --text "hello world"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | Built-in demo text | Input text to embed |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `nomic_embed_text.bin` — QNN model binary

## Use Cases

- **Semantic search**: Find similar documents by comparing embeddings
- **RAG pipelines**: Embed documents for retrieval-augmented generation
- **Text similarity**: Compute cosine similarity between text pairs
- **Clustering**: Group similar texts by embedding distance

## Notes

- Output embeddings are saved to `python/embeddings.npy`.
