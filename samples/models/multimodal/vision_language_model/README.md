# Vision Language Model

Vision-language models (VLMs) that understand both images/videos and natural language, enabling visual question answering and multimodal conversations.

## Models

| Model | Architecture | Task | Platform |
|-------|-------------|------|----------|
| [qwen_vl](qwen_vl/) | Qwen2-VL / Qwen3-VL | Image/video/camera + question → answer (Gradio web UI) | Linux (ARM64) only |

> **Note:** Qwen-VL requires the Linux (aarch64-oe-linux) QNN runtime and is not supported on Windows on Snapdragon (WoS). It requires manual model download and QNN SDK setup.

## Quick Start (Linux only)

```bash
cd qai-appbuilder/samples
python models/multimodal/vision_language_model/qwen_vl/python/qwen_vl.py
```

See [qwen_vl/README.md](qwen_vl/README.md) for detailed setup instructions.
