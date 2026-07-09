# OpusMT Zh-En — Chinese-to-English Neural Machine Translation on Snapdragon NPU

## Overview

**OpusMT Zh-En** is a neural machine translation model that translates Chinese text to English, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It uses the Helsinki-NLP MarianMT architecture with an encoder-decoder design.

- **Task**: Neural Machine Translation (Chinese → English)
- **Model**: Helsinki-NLP/opus-mt-zh-en (MarianMT)
- **Input**: Chinese text string
- **Output**: English translation string
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [opus_mt_zh_en](https://aihub.qualcomm.com/compute/models/opus_mt_zh_en)

## Model Architecture

OpusMT Zh-En uses a MarianMT encoder-decoder architecture:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Encoder | `encoder.bin` | Encodes Chinese input tokens → cross-attention KV cache (6 layers, 8 heads) |
| Decoder | `decoder.bin` | Autoregressive English token generation with self-attention + cross-attention |

**Encoder input**: `input_ids [1, 256]` + `encoder_attention_mask [1, 256]`  
**Decoder input**: `input_ids [1, 1]` + `position [1]` + cross-attention KV cache + self-attention KV cache  
**Decoder output**: `logits [1, 1, 65001]` + updated self-attention KV cache  
**Vocabulary size**: 65001 tokens  
**Max sequence length**: 256 tokens

## Requirements

```
pip install transformers sentencepiece
```

## Quick Start

```bash
cd qai-appbuilder\samples
python Multimodal\Text_Generation\opus_mt_zh_en\opus_mt_zh_en.py
```

With custom text:
```bash
python Multimodal\Text_Generation\opus_mt_zh_en\opus_mt_zh_en.py --input-text "人工智能正在改变世界。"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input-text` | None (runs built-in test sentences) | Chinese text to translate |

## Model Download

The model is automatically downloaded on first run. The script detects your device (Snapdragon X Elite or X2 Elite) and downloads the corresponding model zip:
- **Snapdragon X Elite**: `opus_mt_zh_en-voice_ai-float-qualcomm_snapdragon_x_elite.zip`
- **Snapdragon X2 Elite**: `opus_mt_zh_en-voice_ai-float-qualcomm_snapdragon_x2_elite.zip`

The MarianMT tokenizer (`Helsinki-NLP/opus-mt-zh-en`) is downloaded from HuggingFace on first run.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Chinese text string | Any Chinese text (truncated to 256 tokens) |
| **Output** | English text string | Translated English text |

## Pipeline Details

```
Chinese text
    ↓ MarianTokenizer (max_length=256, padding)
input_ids [int32, shape: (1, 256)]
attention_mask [int32, shape: (1, 256)]
    ↓ Encoder (NPU)
Cross-attention KV cache [6 layers × (key + value)]
    ↓ Initialize: current_token = BOS (65000)
    ↓ Autoregressive decoder loop (max 256 steps):
      ↓ Decoder (NPU): token + position + cross-KV + self-KV
      ↓ logits [1, 1, 65001]
      ↓ greedy decode: next_token = argmax(logits)
      ↓ Update self-attention KV cache
      ↓ Stop if next_token == EOS (0)
Generated token IDs
    ↓ MarianTokenizer.decode
English translation (string)
```

## Example Output

```
[1/4] Checking / downloading model...
[INFO] Detected device: snapdragon_x_elite
[INFO] Model already exists: ...
[2/4] Loading tokenizer...
      Loading tokenizer from: ...models\tokenizer\...
      Vocab=65001, BOS=65000, EOS=0, PAD=65000
[3/4] Loading encoder onto NPU...
      Encoder inputs : ['input_ids', 'encoder_attention_mask']
      Encoder outputs: ['block_0_cross_key_states', ...]
[4/4] Loading decoder onto NPU...
      Decoder inputs : ['input_ids', 'position', ...]

Models loaded. Starting inference...

[Translate] Input : 我爱中国。
[Translate] Output: I love China.
[Timing]    Encoder: 45ms | Decoder: 312ms (5 tokens, 62ms/tok)

[Translate] Input : 人工智能正在改变世界。
[Translate] Output: Artificial intelligence is changing the world.
```

## Notes

- The model uses greedy decoding (temperature=0, no beam search).
- BOS token ID = 65000 (pad_token_id), EOS token ID = 0.
- The self-attention KV cache is updated incrementally for efficient autoregressive generation.
- The tokenizer is stored locally in `models/tokenizer/` after first download.
