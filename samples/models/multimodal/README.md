# Multimodal Models

This directory contains Python inference samples for multimodal AI models running on the Snapdragon NPU (HTP) via QAI AppBuilder. These models process multiple modalities (text, image, audio) together.

## Models

| Subcategory | Model | Task |
|-------------|-------|------|
| [Image to Text](image_to_text/) | [easy_ocr](image_to_text/easy_ocr/) | Image → detected text (English + Chinese OCR) |
| [Image Classification](image_classification/) | [openai_clip](image_classification/openai_clip/) | Images + text query → similarity scores (zero-shot classification) |
| [Text Embedding](text_embedding/) | [nomic_embed_text](text_embedding/nomic_embed_text/) | Text → 768-dim embedding vector (for RAG/semantic search) |
| [Text Embedding](text_embedding/) | [opus_mt_zh_en](text_embedding/opus_mt_zh_en/) | Chinese text → English translation |
| [Text Embedding](text_embedding/) | [opus_mt_en_zh](text_embedding/opus_mt_en_zh/) | English text → Chinese translation |
| [Vision Language Model](vision_language_model/) | [qwen_vl](vision_language_model/qwen_vl/) *(Linux only)* | Image/video + question → answer |

## Quick Start

```bash
cd qai-appbuilder\samples

# OCR
python run_inference.py --model easy_ocr --args "--Image_Path path/to/image.png"

# CLIP zero-shot classification
python run_inference.py --model openai_clip --args "--text 'mountain landscape'"

# Text embedding
python run_inference.py --model nomic_embed_text --args "--text 'hello world'"

# Chinese to English translation
python run_inference.py --model opus_mt_zh_en --args "--text '人工智能正在改变世界'"

# English to Chinese translation
python run_inference.py --model opus_mt_en_zh --args "--text 'Artificial intelligence is changing the world.'"
```

## Dependencies

```
pip install easyocr openai-clip transformers sentencepiece
```
