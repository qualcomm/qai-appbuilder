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
| 1. Text Encoder | `encoder.bin` | Converts phoneme IDs to hidden representations (m_p, logs_p, x_encoded, x_mask) |
| 2. Stochastic Duration Predictor (SDP) | `sdp.bin` | Predicts phoneme durations and generates attention alignment |
| 3. Flow | `flow.bin` | Transforms the attention matrix and encoder output into latent features z |
| 4. HiFi-GAN Decoder | `decoder.bin` | Converts latent features z into audio waveform samples |

**G2P (Grapheme-to-Phoneme)**: Uses `gruut` (pure Python, no espeak-ng required) to convert English text to IPA phonemes, then maps them to Piper phoneme IDs.

## Requirements

```
pip install gruut
```

Additional dependencies (already included in the main requirements):
```
pip install numpy torch
```

## Quick Start

```bash
cd qai-appbuilder\samples
python audio\Audio_Generation\pipertts_en\pipertts_en.py
```

With custom text:
```bash
python audio\Audio_Generation\pipertts_en\pipertts_en.py --text "Hello, this is a test of PiperTTS on Snapdragon."
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | Built-in demo text | Input English text to synthesize |
| `--models_dir` | `<script_dir>/models/` | Directory containing model `.bin` files |
| `--out` | `<script_dir>/output.wav` | Output WAV file path |
| `--noise_scale` | `0.667` | Noise scale (controls voice variation) |
| `--noise_scale_w` | `0.8` | Duration noise scale |
| `--length_scale` | `1.0` | Speech rate (>1 is slower, <1 is faster) |
| `--verbose` | `False` | Print detailed G2P and inference info |
| `--skip_download` | `False` | Skip model download (use when models are already prepared) |

## Model Download

Models are automatically downloaded on first run. The script detects your device model (Snapdragon X Elite or X2 Elite) and downloads the corresponding model zip from:
```
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/pipertts_en/releases/v0.57.1/
```

Required model files (extracted to `models/`):
- `encoder.bin`
- `sdp.bin`
- `flow.bin`
- `decoder.bin`

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

## Example Output

```
PiperTTS-EN HTP inference  (v10 - auto model download)
  models_dir    : C:\...\models
  out           : C:\...\output.wav
  noise_scale   : 0.667
  noise_scale_w : 0.8
  length_scale  : 1.0

[TTS] 'This is the demo of pipertts_en on WoS.'
  [1/4] G2P        0.012s  -> 42 piper ids
  [2/4] Encoder    0.089s  -> m_p[1, 192, 512]  x_mask_nonzero=42
  [3/4] SDP        0.045s  -> y_lengths=126
  [4/4] Flow+Dec   0.312s  -> 32256 samples (1.46s)

  Total inference: 0.458s  RTF: 0.314  (faster than real-time 3.2x)

[OK]  WAV: C:\...\output.wav
      22050 Hz  1.46s  32256 samples  RMS=0.1234
```

## Notes

- The G2P step uses `gruut` which requires `pip install gruut`. This is the only extra dependency beyond the standard requirements.
- The HiFi-GAN decoder uses overlap-chunking to handle long sequences efficiently.
- For very long texts, the model processes up to 1536 mel frames (≈ 17.5 seconds of audio) per inference call.
