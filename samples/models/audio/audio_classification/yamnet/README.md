# YAMNet — Audio Event Classification on Snapdragon NPU

## Overview

**YAMNet** (Yet Another Mobile Network) is a deep neural network for audio event classification, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It classifies audio into 521 sound categories from the AudioSet ontology. Supports multiple model formats: `.bin` (precompiled HTP binary), `.dlc` (float DLC), and `.onnx` (float ONNX).

- **Task**: Audio Event Classification
- **Classes**: 521 AudioSet sound categories
- **Input**: WAV audio file (any sample rate, auto-resampled to 16 kHz)
- **Output**: Top-5 predicted sound categories
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux
- **Runtime**: HTP (Hexagon NPU), CPU, or GPU
- **AI Hub Model**: [yamnet](https://aihub.qualcomm.com/compute/models/yamnet)

## Model Architecture

YAMNet uses a MobileNet-based architecture that processes log mel spectrograms:

- **Input**: Batched log mel spectrogram patches `[N, 1, 96, 64]` (0.96-second windows)
- **Output**: Class probability scores `[N, 521]`
- **Preprocessing**: VGGish-style log mel spectrogram (16 kHz, 64 mel bands, 25 ms window, 10 ms hop)

## Requirements

```
pip install soundfile soxr torch
```

## Quick Start

```bash
cd qai-appbuilder\samples

# Default (precompiled HTP binary, auto-downloaded)
python run_inference.py --model yamnet

# Float DLC (auto-downloaded)
python run_inference.py --model yamnet --args "--dlc"

# Float ONNX (auto-downloaded)
python run_inference.py --model yamnet --args "--onnx"

# With a custom audio file
python run_inference.py --model yamnet --args "--input_audio_path path\to\audio.wav"
```

Or run directly:
```bash
# Default: precompiled HTP binary
python models\audio\audio_classification\yamnet\python\yamnet.py

# Float DLC
python models\audio\audio_classification\yamnet\python\yamnet.py --dlc

# Float ONNX via OnnxRuntimeContext (onnxruntime_qnn HTP EP)
python models\audio\audio_classification\yamnet\python\yamnet.py --onnx

# CPU runtime with DLC
python models\audio\audio_classification\yamnet\python\yamnet.py --dlc --cpu

# Custom audio file
python models\audio\audio_classification\yamnet\python\yamnet.py --input_audio_path path\to\audio.wav
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_audio_path` | `assets/input.wav` (auto-downloaded) | Path to the input WAV audio file |
| `--bin` | True (default) | Use precompiled HTP context binary |
| `--dlc` | False | Use float DLC model (auto-downloaded) |
| `--onnx` | False | Use float ONNX via OnnxRuntimeContext (onnxruntime_qnn HTP EP) |
| `--cpu` | False | Use CPU runtime instead of HTP |
| `--gpu` | False | Use GPU runtime instead of HTP |
| `--chipset` | Auto-detected | SoC ID for hub-model download |

> **Note**: `--bin`, `--dlc`, and `--onnx` are mutually exclusive. `--cpu` and `--gpu` are mutually exclusive.
>
> On **x86 Windows**, the runtime is always forced to CPU and the model format is always `.dlc` regardless of flags.

## Platform Support

| Platform | HTP | GPU | CPU | `.bin` | `.dlc` | `.onnx` |
| -------- | --- | --- | --- | ------ | ------ | ------- |
| Windows on Snapdragon (WoS) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| x86 Windows | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ |
| ARM64 Linux | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| x86 Linux | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

## Model Download

Models and supporting files are automatically downloaded on first run to `models/`:

| File | Description | Trigger |
| ---- | ----------- | ------- |
| `yamnet.bin` | Precompiled HTP context binary | Default (no flags) or `--bin` |
| `yamnet.dlc` | Float DLC model | `--dlc` |
| `yamnet.onnx` + `yamnet.data` | Float ONNX model (external data format) | `--onnx` |
| `yamnet_class_map.csv` | 521 AudioSet class names | Always |

A sample audio file is auto-downloaded to `assets/`:
- `input.wav` — sample whistling audio for testing

Model download URLs (v0.59.0):
- DLC: `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/releases/v0.59.0/yamnet-qnn_dlc-float.zip`
- ONNX: `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/releases/v0.59.0/yamnet-onnx-float.zip`

> **Note**: The ONNX model uses ONNX external data format and consists of two files (`yamnet.onnx` and `yamnet.data`). Both files must be present in the same directory.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | WAV file | Any sample rate (auto-resampled to 16 kHz mono) |
| **Output** | String | Top-5 predicted sound categories (pipe-separated) |

## Pipeline Details

```
Audio file (WAV)
    ↓ Load with soundfile (int16 → float32)
    ↓ Convert to mono (average channels if stereo)
    ↓ Resample to 16 kHz (via soxr, if needed)
    ↓ VGGish log mel spectrogram (64 mel bands, 25ms window, 10ms hop)
    ↓ Slice into 0.96-second non-overlapping patches
Patches [float32, shape: (N, 1, 96, 64)]
    ↓ YAMNet inference (HTP / CPU / GPU)
Class scores [float32, shape: (N, 521)]
    ↓ Average over time dimension
    ↓ Top-5 argmax
Top-5 class names (pipe-separated string)
```

## Example Output

```
[INFO] Detected platform: wos
[INFO] Runtime: HTP
[INFO] Using BIN model: ...\models\yamnet.bin
[INFO] Starting inference...
[INFO] Inference completed in 45.2 ms (0.05 seconds)
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
- When using `--onnx` on WoS, the model runs via `onnxruntime_qnn` with the QNN HTP Execution Provider. If the HTP EP is unavailable, it automatically falls back to CPU.
- If the `.bin` hub download fails, the script automatically falls back to downloading the float `.dlc` model.
