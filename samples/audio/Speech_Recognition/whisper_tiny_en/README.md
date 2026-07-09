# Whisper Tiny EN — English Speech Recognition on Snapdragon NPU

## Overview

**Whisper Tiny EN** is OpenAI's Whisper Tiny model (English-only) for automatic speech recognition (ASR), running on the Snapdragon NPU (HTP) via QAI AppBuilder. It is the smallest and fastest Whisper variant, ideal for real-time or resource-constrained applications.

- **Task**: Automatic Speech Recognition (ASR)
- **Language**: English
- **Input**: WAV audio file (any sample rate, auto-resampled to 16 kHz)
- **Output**: Transcribed text string
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU) with native data types (float16)
- **AI Hub Model**: [whisper_tiny_en](https://aihub.qualcomm.com/compute/models/whisper_tiny_en)

## Model Architecture

Whisper Tiny EN uses an encoder-decoder architecture split into two QNN context binaries:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Encoder | `whisper_tiny_en-whisperencoder-snapdragon_x_elite.bin` | Processes log mel spectrogram → cross-attention KV cache (4 layers, 6 heads) |
| Decoder | `whisper_tiny_en-whisperdecoder-snapdragon_x_elite.bin` | Autoregressive token generation with self-attention and cross-attention KV cache |

**Encoder output shape**: k_cache_cross `[4, 6, 64, 1500]`, v_cache_cross `[4, 6, 1500, 64]`  
**Decoder output shape**: logits `[1, 1, 51864]`, k_cache_self `[6, 8, 64, 224]`, v_cache_self `[6, 8, 224, 64]`

> **Comparison with Whisper Base EN**: Tiny has 4 encoder layers (vs 6 in Base) and 6 attention heads (vs 8 in Base), making it ~2× faster but slightly less accurate.

## Requirements

```
pip install audio2numpy scipy openai-whisper
```

## Quick Start

```bash
cd qai-appbuilder\samples
python audio\Speech_Recognition\whisper_tiny_en\whisper_tiny_en.py
```

With a custom audio file:
```bash
python audio\Speech_Recognition\whisper_tiny_en\whisper_tiny_en.py --audio_file path\to\audio.wav
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--audio_file` | `jfk.wav` (auto-downloaded) | Path to the input WAV audio file |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run from Qualcomm AI Hub. The following assets are also downloaded:
- `jfk.wav` — sample JFK speech audio for testing
- `mel_filters.npz` — mel filter bank coefficients

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | WAV file | Any sample rate (auto-resampled to 16 kHz), any duration (chunked at 30s) |
| **Output** | String | Transcribed English text |

## Pipeline Details

```
Audio file (WAV)
    ↓ Load + resample to 16 kHz
    ↓ Chunk into 30-second segments
    ↓ log_mel_spectrogram (80 mel bins, 3000 frames)
Mel spectrogram [float32, shape: (1, 80, 3000)]
    ↓ Encoder (NPU)
Cross-attention KV cache [float16]
    ↓ Decoder (NPU, autoregressive)
Token IDs [int32]
    ↓ Whisper tokenizer
Transcribed text (string)
```

## Example Output

```
SOC_ID: None
Transcription:  And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country.
```

## Notes

- Audio longer than 30 seconds is automatically split into 30-second chunks and transcribed sequentially.
- The model uses `DataType.NATIVE` for both input and output to preserve float16 precision.
- For higher accuracy, see [whisper_base_en](../whisper_base_en/).
