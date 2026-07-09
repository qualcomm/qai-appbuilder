# YAMNet — Audio Event Classification on Snapdragon NPU

## Overview

**YAMNet** (Yet Another Mobile Network) is a deep neural network for audio event classification, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It classifies audio into 521 sound categories from the AudioSet ontology.

- **Task**: Audio Event Classification
- **Classes**: 521 AudioSet sound categories
- **Input**: WAV audio file (any sample rate, auto-resampled to 16 kHz)
- **Output**: Top-5 predicted sound categories
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [yamnet](https://aihub.qualcomm.com/compute/models/yamnet)

## Model Architecture

YAMNet uses a MobileNet-based architecture that processes log mel spectrograms:

- **Input**: Batched log mel spectrogram patches `[N, 1, 96, 64]` (0.96-second windows)
- **Output**: Class probability scores `[N, 521]`
- **Preprocessing**: VGGish-style log mel spectrogram (16 kHz, 64 mel bands, 25ms window, 10ms hop)

## Requirements

```
pip install resampy soundfile torchaudio
```

## Quick Start

```bash
cd qai-appbuilder\samples
python audio\Audio_Classification\yamnet\yamnet.py
```

With a custom audio file:
```bash
python audio\Audio_Classification\yamnet\yamnet.py --image path\to\audio.wav
```

> Note: The `--image` argument is used for the audio file path (legacy naming).

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.wav` (auto-downloaded) | Path to the input WAV audio file |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model and class map are automatically downloaded on first run:
- `yamnet.bin` — QNN model binary
- `yamnet_class_map.csv` — 521 AudioSet class names
- `speech_whistling2.wav` — sample audio for testing

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | WAV file | Any sample rate (auto-resampled to 16 kHz) |
| **Output** | String | Top-5 predicted sound categories (pipe-separated) |

## Pipeline Details

```
Audio file (WAV)
    ↓ Load with soundfile (int16 → float32)
    ↓ Resample to 16 kHz (if needed)
    ↓ VGGish log mel spectrogram (64 mel bands)
    ↓ Slice into 0.96-second patches
Patches [float32, shape: (N, 1, 96, 64)]
    ↓ YAMNet (NPU)
Class scores [float32, shape: (N, 521)]
    ↓ Average over time dimension
    ↓ Top-5 argmax
Top-5 class names (string)
```

## Example Output

```
SOC_ID: None
accuracy shape: (1, 1, 521)
Top 5 predictions:
Whistling | Speech | Human voice | Male speech, man speaking | Singing
```

## Sound Categories (Examples)

YAMNet can classify 521 sound categories including:
- **Speech**: Speech, Male speech, Female speech, Child speech
- **Music**: Music, Musical instrument, Guitar, Piano, Singing
- **Nature**: Rain, Thunder, Wind, Bird, Dog
- **Environment**: Traffic, Engine, Alarm, Siren
- **Actions**: Clapping, Laughter, Coughing, Footsteps

## Notes

- The model processes audio in 0.96-second non-overlapping windows.
- For audio shorter than 0.96 seconds, the entire audio is used as a single patch.
- The class map CSV file maps class indices to human-readable names.
