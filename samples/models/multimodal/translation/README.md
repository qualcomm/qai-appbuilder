# Translation

Neural machine translation models for translating text between languages.

## Models

| Model | Architecture | Task | AI Hub |
|-------|-------------|------|--------|
| [opus_mt_zh_en](opus_mt_zh_en/) | MarianMT encoder-decoder | Chinese → English translation (greedy decoding, max 256 tokens) | [Link](https://aihub.qualcomm.com/compute/models/opus_mt_zh_en) |

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model opus_mt_zh_en --args "--input-text '人工智能正在改变世界'"
```

**Example output:**
```
Input:  人工智能正在改变世界
Output: Artificial intelligence is changing the world.
```
