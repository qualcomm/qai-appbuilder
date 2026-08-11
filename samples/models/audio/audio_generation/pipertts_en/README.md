# PiperTTS-EN — English Text-to-Speech on Snapdragon NPU

## Overview

**PiperTTS-EN** is a high-quality English text-to-speech (TTS) model that runs fully on the Snapdragon NPU (HTP) via QAI AppBuilder. It converts English text into a natural-sounding WAV audio file using a 4-stage neural pipeline.

- **Task**: Text-to-Speech (TTS)
- **Language**: English
- **Output**: 22050 Hz mono WAV audio
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [pipertts_en](https://aihub.qualcomm.com/compute/models/pipertts_en)

## Model Architecture

PiperTTS-EN uses a 4-stage pipeline, each stage running as a separate QNN context binary:

| Stage | Model File | Description |
| ----- | ---------- | ----------- |
| 1. Text Encoder | `encoder.bin` | Converts phoneme IDs to hidden representations |
| 2. Stochastic Duration Predictor (SDP) | `sdp.bin` | Predicts phoneme durations |
| 3. Flow | `flow.bin` | Transforms encoder output into latent features z |
| 4. HiFi-GAN Decoder | `decoder.bin` | Converts latent features z into audio waveform |

**G2P (Grapheme-to-Phoneme)**: Uses `gruut` (pure Python) to convert English text to IPA phonemes.

## Requirements

```
pip install gruut numpy torch
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model pipertts_en --args "--text 'Hello, this is a test.'"
```

Or run directly:
```bash
python models\audio\audio_generation\pipertts_en\python\pipertts_en.py --text "Hello world."
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | Built-in demo text | Input English text to synthesize |
| `--models_dir` | `../models/` | Directory containing model `.bin` files |
| `--out` | `python/output.wav` | Output WAV file path |
| `--noise_scale` | `0.667` | Noise scale (controls voice variation) |
| `--noise_scale_w` | `0.8` | Duration noise scale |
| `--length_scale` | `1.0` | Speech rate (>1 is slower, <1 is faster) |
| `--skip_download` | `False` | Skip model download |

## Model Download

Models are automatically downloaded on first run to `models/`. The script detects your device (Snapdragon X Elite or X2 Elite) and downloads the corresponding zip:
- `encoder.bin`, `sdp.bin`, `flow.bin`, `decoder.bin`

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | UTF-8 string | English text (any length) |
| **Output** | WAV file | 22050 Hz, 16-bit PCM, mono |

## Pipeline Details

```
Text (string)
    ↓ G2P (gruut)
Phoneme IDs [int32, shape: (1, 512)]
    ↓ Encoder
m_p, logs_p, x_encoded, x_mask [float32]
    ↓ SDP
y_lengths, w_ceil [float32]
    ↓ generate_path + Flow
z [float32, shape: (1, 192, 1536)]
    ↓ HiFi-GAN Decoder (overlap chunking)
Audio waveform [float32]
    ↓ save_wav
output.wav (22050 Hz, 16-bit PCM)
```

## Performance

On Snapdragon X Elite:
- Typical RTF (Real-Time Factor) < 1.0 (faster than real-time)
- Total inference time for a short sentence: ~0.5–2 seconds

## Notes

- The G2P step uses `gruut` which requires `pip install gruut`.
- The HiFi-GAN decoder uses overlap-chunking to handle long sequences efficiently.
- Output is saved to `python/output.wav` by default.
