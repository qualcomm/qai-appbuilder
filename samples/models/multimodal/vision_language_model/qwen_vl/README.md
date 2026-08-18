# Qwen-VL — Vision Language Model on Snapdragon NPU

## Overview

**Qwen-VL** is a vision-language model that understands images, videos, and camera input, enabling visual question answering and multimodal conversations. Supports both Qwen2-VL and Qwen3-VL variants, running on the Snapdragon NPU via QAI AppBuilder.

- **Task**: Visual Question Answering / Multimodal Chat
- **Input**: Image / video / camera + natural language question
- **Output**: Natural language answer
- **Platform**: **Linux (ARM64) only** — requires aarch64-oe-linux QNN runtime
- **Runtime**: HTP (Hexagon NPU)

> **Note:** Qwen-VL is NOT supported on Windows on Snapdragon (WoS). It requires the Linux (aarch64-oe-linux) QNN runtime and manual model download.

## Model Architecture

| Component | Description |
| --------- | ----------- |
| Visual Encoder (VEG) | Processes image/video frames into visual tokens |
| Language Model (LLM) | Generates text responses from visual + text tokens |

Supported variants:
- **Qwen2-VL** (`qwen2_vlm_qnn.py`) — Qwen2 vision-language model
- **Qwen3-VL** (`qwen3_vlm_qnn.py`) — Qwen3 vision-language model (newer)

## Requirements

- Linux (ARM64) with QNN SDK installed
- Manual model download and QNN SDK setup required

## Quick Start (Linux only)

```bash
cd qai-appbuilder/samples
python models/multimodal/vision_language_model/qwen_vl/python/qwen_vl.py
```

With a specific model:
Qwen2：
```bash
python models/multimodal/vision_language_model/qwen_vl/python/qwen_vl.py --model qwen2 --path models/multimodal/vision_language_model/qwen_vl/models/qwen2
```

Qwen3：
```bash
python models/multimodal/vision_language_model/qwen_vl/python/qwen_vl.py --model qwen3 --path models/multimodal/vision_language_model/qwen_vl/models/qwen3
```

## Scripts

| Script | Description |
| ------ | ----------- |
| `python/install_model.py` | Downloads and extracts models |
| `python/qwen_vl.py` | Main entry point with Gradio web UI |
| `python/qwen2_vlm_qnn.py` | Qwen2-VL QNN inference implementation |
| `python/qwen3_vlm_qnn.py` | Qwen3-VL QNN inference implementation |
| `python/vlm_inference.py` | Shared VLM inference utilities |

## Model Download

Models must be downloaded manually. See the Qualcomm AI Hub or aidevhome.com for available Qwen-VL model packages.

Place model files in a directory and pass it with `--path`.

## Notes

- The Gradio web UI launches on `http://localhost:7860` by default.
- Supports image, video, and live camera input.
- Requires QNN SDK setup with the aarch64-oe-linux runtime libraries.
