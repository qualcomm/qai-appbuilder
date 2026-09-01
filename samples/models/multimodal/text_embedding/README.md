# Text Embedding

Text embedding and neural machine translation models. These models turn text into dense vector representations for semantic search / RAG (Retrieval-Augmented Generation), or translate text between languages — all running on the Snapdragon NPU (HTP) via QAI AppBuilder.

## Models

| Model | Architecture | Task | AI Hub |
|-------|-------------|------|--------|
| [nomic_embed_text](nomic_embed_text/) | BERT-based (NomicBERT) | Text → 768-dimensional embedding vector, saved as `embeddings.npy` | [Link](https://aihub.qualcomm.com/compute/models/nomic_embed_text) |
| [opus_mt_zh_en](opus_mt_zh_en/) | MarianMT encoder-decoder | Chinese → English translation (greedy decoding, max 256 tokens) | [Link](https://aihub.qualcomm.com/compute/models/opus_mt_zh_en) |
| [opus_mt_en_zh](opus_mt_en_zh/) | MarianMT encoder-decoder | English → Chinese translation (greedy decoding, max 256 tokens) | [Link](https://aihub.qualcomm.com/compute/models/opus_mt_en_zh) |

Use cases: semantic search, document retrieval, RAG pipelines, text similarity, machine translation.

## Quick Start

```bash
cd qai-appbuilder\samples

# Text embedding
python run_inference.py --model nomic_embed_text --args "--text 'hello world'"

# Chinese to English translation
python run_inference.py --model opus_mt_zh_en --args "--text '人工智能正在改变世界'"

# English to Chinese translation
python run_inference.py --model opus_mt_en_zh --args "--text 'Artificial intelligence is changing the world.'"
```
