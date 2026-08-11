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
| Decoder | `whisper_tiny_en-whisperdecoder-snapdragon_x_elite.bin` | Autoregressive token generation |

> **Comparison with Whisper Base EN**: Tiny has 4 encoder layers (vs 6 in Base) and 6 attention heads (vs 8 in Base), making it ~2× faster but slightly less accurate.

## Requirements

```
pip install audio2numpy openai-whisper
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model whisper_tiny_en
```

With a custom audio file:
```bash
python run_inference.py --model whisper_tiny_en --args "--audio_file path\to\audio.wav"
```

Or run directly:
```bash
python models\audio\speech_recognition\whisper_tiny_en\python\whisper_tiny_en.py
python models\audio\speech_recognition\whisper_tiny_en\python\whisper_tiny_en.py --audio_file path\to\audio.wav
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--audio_file` | `assets/jfk.wav` (auto-downloaded) | Path to the input WAV audio file |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run to `models/`. Assets downloaded to `assets/`:
- `jfk.wav` — sample JFK speech audio for testing
- `mel_filters.npz` — mel filter bank coefficients

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | WAV file | Any sample rate (auto-resampled to 16 kHz), any duration (chunked at 30s) |
| **Output** | String | Transcribed English text |

## Example Output

```
SOC_ID: None
Transcription:  And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country.
```

## Notes

- Audio longer than 30 seconds is automatically split into 30-second chunks.
- For higher accuracy, see [whisper_base_en](../whisper_base_en/).
