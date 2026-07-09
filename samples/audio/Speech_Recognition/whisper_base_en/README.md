# Whisper Base EN — English Speech Recognition on Snapdragon NPU

## Overview

**Whisper Base EN** is OpenAI's Whisper Base model (English-only) for automatic speech recognition (ASR), running on the Snapdragon NPU (HTP) via QAI AppBuilder. It transcribes English audio files to text.

- **Task**: Automatic Speech Recognition (ASR)
- **Language**: English
- **Input**: WAV audio file (any sample rate, auto-resampled to 16 kHz)
- **Output**: Transcribed text string
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU) with native data types (float16)
- **AI Hub Model**: [whisper_base_en](https://aihub.qualcomm.com/compute/models/whisper_base_en)

## Model Architecture

Whisper Base EN uses an encoder-decoder architecture split into two QNN context binaries:

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Encoder | `whisper_base_en-whisperencoder-snapdragon_x_elite.bin` | Processes log mel spectrogram → cross-attention KV cache (6 layers, 8 heads) |
| Decoder | `whisper_base_en-whisperdecoder-snapdragon_x_elite.bin` | Autoregressive token generation with self-attention and cross-attention KV cache |

**Encoder output shape**: k_cache_cross `[6, 8, 64, 1500]`, v_cache_cross `[6, 8, 1500, 64]`  
**Decoder output shape**: logits `[1, 1, 51864]`, k_cache_self `[6, 8, 64, 224]`, v_cache_self `[6, 8, 224, 64]`

## Requirements

```
pip install audio2numpy scipy openai-whisper
```

## Quick Start

```bash
cd qai-appbuilder\samples
python audio\Speech_Recognition\whisper_base_en\whisper_base_en.py
```

With a custom audio file:
```bash
python audio\Speech_Recognition\whisper_base_en\whisper_base_en.py --audio_file path\to\audio.wav
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--audio_file` | `jfk.wav` (auto-downloaded) | Path to the input WAV audio file |
| `--chipset` | Auto-detected | SoC ID for model download (e.g., `43` for Snapdragon X Elite) |

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

## Performance

On Snapdragon X Elite:
- Encoder inference: ~50–100 ms
- Decoder inference per token: ~20–50 ms
- Typical 30-second audio: ~2–5 seconds total

## Example Output

```
SOC_ID: None
[INFO] Detected platform: wos
Transcription:  And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country.
```

## Notes

- Audio longer than 30 seconds is automatically split into 30-second chunks and transcribed sequentially.
- The model uses `DataType.NATIVE` for both input and output to preserve float16 precision.
- For a smaller/faster model, see [whisper_tiny_en](../whisper_tiny_en/).
- For a more accurate model, consider using a larger Whisper variant.
