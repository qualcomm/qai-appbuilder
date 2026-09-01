# OpusMT En-Zh — English to Chinese Translation on Snapdragon NPU

## Overview

**OpusMT En-Zh** translates English text to Chinese using the MarianMT encoder-decoder architecture, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Machine Translation (English → Chinese)
- **Input**: English text (UTF-8)
- **Output**: Chinese translation
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [opus_mt_en_zh](https://aihub.qualcomm.com/compute/models/opus_mt_en_zh)

## Model Architecture

- **Architecture**: MarianMT encoder-decoder (Helsinki-NLP/opus-mt-en-zh)
- **Decoding**: Greedy decoding, max 256 output tokens
- **Tokenizer**: SentencePiece (auto-downloaded)

## Requirements

```
pip install transformers sentencepiece torch numpy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model opus_mt_en_zh --args "--text 'Artificial intelligence is changing the world.'"
```

Or run directly:
```bash
python models\multimodal\text_embedding\opus_mt_en_zh\python\opus_mt_en_zh.py --text "The weather is very nice today."
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | Built-in demo sentences | English text to translate |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`. The tokenizer is also auto-downloaded to `models/tokenizer/`.

## Example

```
Input:  Artificial intelligence is changing the world.
Output: 人工智能正在改变世界。

Input:  The weather is very nice today.
Output: 今天天气很好。
```
