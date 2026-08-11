# OpusMT Zh-En — Chinese to English Translation on Snapdragon NPU

## Overview

**OpusMT Zh-En** translates Chinese text to English using the MarianMT encoder-decoder architecture, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Machine Translation (Chinese → English)
- **Input**: Chinese text (UTF-8)
- **Output**: English translation
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [opus_mt_zh_en](https://aihub.qualcomm.com/compute/models/opus_mt_zh_en)

## Model Architecture

- **Architecture**: MarianMT encoder-decoder (Helsinki-NLP/opus_mt_zh_en)
- **Decoding**: Greedy decoding, max 256 output tokens
- **Tokenizer**: SentencePiece (auto-downloaded)

## Requirements

```
pip install transformers sentencepiece torch numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model opus_mt_zh_en --args "--input-text '人工智能正在改变世界'"
```

Or run directly:
```bash
python models\multimodal\translation\opus_mt_zh_en\python\opus_mt_zh_en.py --input-text "今天天气很好"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input-text` | Built-in demo text | Chinese text to translate |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`. The tokenizer is also auto-downloaded to `models/tokenizer/`.

## Example

```
Input:  人工智能正在改变世界
Output: Artificial intelligence is changing the world.

Input:  今天天气很好
Output: The weather is very good today.
```
