# Text Embedding

Text embedding models that convert text into dense vector representations for semantic search and RAG (Retrieval-Augmented Generation).

## Models

| Model | Architecture | Output | AI Hub |
|-------|-------------|--------|--------|
| [nomic_embed_text](nomic_embed_text/) | BERT-based (NomicBERT) | 768-dimensional embedding vector, saved as `embeddings.npy` | [Link](https://aihub.qualcomm.com/compute/models/nomic_embed_text) |

Use cases: semantic search, document retrieval, RAG pipelines, text similarity.

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model nomic_embed_text --args "--text 'hello world'"
```
