<br>

<div align="center">
  <img src="https://raw.githubusercontent.com/qualcomm/qai-appbuilder/main/docs/images/qai_banner.svg" alt="QAI AppBuilder & QAI ModelBuilder" width="900" height="150">
</div>

<br>

<div align="center">
  <a href="https://github.com/qualcomm/qai-appbuilder"><img src="https://img.shields.io/github/stars/qualcomm/qai-appbuilder" alt="stars"></a>
  <a href="https://github.com/qualcomm/qai-appbuilder/releases/tag/v2.48.40"><img src="https://img.shields.io/badge/Release-v3.0.0-green" alt="Release"></a>
  <a href="https://opensource.org/license/BSD-3-clause"><img src="https://img.shields.io/badge/License-BSD--3--Clause-blue" alt="License: BSD 3-Clause"></a>
  <a href="https://www.python.org/downloads/windows/"><img src="https://img.shields.io/badge/Python-00599C?logo=Python" alt="Python"></a>
  <a href="https://en.cppreference.com/w/cpp/compiler_support"><img src="https://img.shields.io/badge/C++-999999?logo=c%2B%2B" alt="C++"></a>
  <a href="https://www.qualcomm.com/products/technology/processors/ai-engine"><img src="https://img.shields.io/badge/NPU-ccffff" alt="NPU"></a>
  <a href="https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie"><img src="https://img.shields.io/badge/Genie AI-ffff9C" alt="Genie AI"></a>
</div>

<br>

---

## QAI AppBuilder

**QAI AppBuilder** (the "Quick AI Application Builder", this repository) is an open-source,
**on-device AI application platform** built on top of the
[Qualcomm® AI Runtime SDK](https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK).
Just describe the app you want in plain language — the AI Agent turns it into a complete,
runnable application on the Snapdragon NPU, with **no coding required**.

### From an idea to a running app — in one conversation

At the center of QAI AppBuilder is the **App Builder**: tell the Agent what you want to build
("*a screenshot OCR tool*", "*a voice-memo transcriber*", "*a real-time speech translator*")
and it assembles a complete, self-contained local application for you — picking the right
models, wiring up the pipeline, and generating the UI. The default output is a lightweight
local web app; with a custom prompt the Agent can produce a CLI tool, a desktop utility, or a
batch script just as easily.

To do this, the Agent draws on **on-device Model Packs** (Whisper, Zipformer-ZH, MeloTTS,
PP-OCR and more) and, whenever it needs a model that isn't packaged yet, resolves it
automatically — downloading a pre-built model straight from
[Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models) when one exists, or converting
your own PyTorch / ONNX model (export → quantization → context-binary generation → accuracy
validation via QAIRT SDK) when it doesn't. Either way, you never leave the conversation, and
every model ends up running on the same on-device NPU engine (`QNNContext` via
`qai-appbuilder`).

### Why it matters

- **Minutes, not days** — get a working local AI app without hand-writing a UI or wiring up
  model I/O yourself.
- **Truly on-device** — every model runs on the Snapdragon NPU; your data never leaves the
  machine. No internet connection is needed at inference time.
- **Private by design** — a natural fit for privacy-sensitive scenarios (corporate documents,
  medical images, personal recordings), backed by a built-in security module that gates every
  file, command, and tool the Agent touches.
- **Extensible** — the Agent is driven by hot-reloadable **Skills** and **Model Packs**; add a
  new self-contained module and the Agent picks it up automatically.

---

### Get Started with QAI ModelBuilder

> **QAI ModelBuilder** is the natural-language Agent front-end of QAI AppBuilder — the App Builder,
> Model Builder, and AI Hub Model Run capabilities described above all live here. Grab the
> pre-built package below and you'll have a running, on-device AI app builder in **two commands**.

<div align="center">
  <a href="https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/qaimodelbuilder.zip">
    <img src="https://img.shields.io/badge/Download-QAI%20ModelBuilder%20v3.0.0-2ea44f?style=for-the-badge" alt="Download QAI ModelBuilder">
  </a>
  &nbsp;&nbsp;
  <a href="https://github.com/qualcomm/qai-appbuilder/tree/main/tools/qaimodelbuilder">
    <img src="https://img.shields.io/badge/Browse%20Source-tools/qaimodelbuilder-24292e?style=for-the-badge&logo=github" alt="Browse QAI ModelBuilder source code">
  </a>
</div>

<br>

**Just want to use it? Two steps:**

> **Platform:** Windows on Snapdragon (ARM64). No admin rights needed — `Setup.bat` automatically
> downloads Python, Node.js, QAIRT SDK, and model weights for you.

1. **Download & unzip**
   [`qaimodelbuilder.zip`](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/qaimodelbuilder.zip),
   then open `cmd.exe` in the extracted folder.

2. **Run two commands** — the first installs the environment (one-time), the second launches the
   WebUI and opens your browser:

```cmd
Setup.bat
Start.bat
```

Your browser opens the QAI ModelBuilder WebUI — start chatting to build an app, convert a model,
or run one straight from AI Hub.

**Want to read or modify the code?** The complete QAI ModelBuilder project lives at
[`tools/qaimodelbuilder`](https://github.com/qualcomm/qai-appbuilder/tree/main/tools/qaimodelbuilder).
Clone the repo, then run `Setup.bat` → `Build.bat` → `Start.bat` to launch from source.

> 📖 **Docs inside the QAI ModelBuilder project:**
>
> | Document | Description |
> |----------|-------------|
> | [README](tools/qaimodelbuilder/README.md) \| [中文](tools/qaimodelbuilder/README.zh-CN.md) | Full project overview — architecture, features, configuration, skill system, FAQ |
> | [Quick Start](tools/qaimodelbuilder/QUICK-START.md) \| [中文](tools/qaimodelbuilder/QUICK-START.zh-CN.md) | 1-page cheat-sheet — dev mode / desktop app (Tauri) / release build, and which `.bat` to run when |

---

<div align="center">
  <img src="https://raw.githubusercontent.com/qualcomm/qai-appbuilder/main/docs/images/qai_appbuilder_agent.svg" alt="QAI AppBuilder Agent Capabilities" width="1330" height="488">
</div>

### What can QAI AppBuilder do?

| Capability | Description |
|------------|-------------|
| **Run AI Models on NPU / GPU / CPU** | Load QNN context binaries (`.bin`), model libraries (`.dll`), or SNPE DLC (`.dlc`) onto the Snapdragon NPU, GPU, or CPU and perform high-performance inference |
| **C++ & Python APIs** | Full-featured bindings for both C++ and Python, so you can integrate on-device AI into any project regardless of language |
| **Cross-Platform Support** | Runs on **Windows on Snapdragon (WoS)** ARM64 and **Linux** (e.g. QCS8550, QCM6490), with a unified API surface across both platforms |
| **AI-Driven Model Conversion** | Convert PyTorch / ONNX models to QNN format at multiple precisions (FP16 / W8A16 / W8A8 / W4A8) through **natural language chat** — the AI handles export, quantization, context binary generation, and accuracy validation end-to-end |
| **Pre-built Models from AI Hub** | Automatically download and run pre-exported QNN models from [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models) with no conversion step required |
| **On-Device Model Workbench** | Run ready-to-use **Model Packs** (SR / OCR / ASR / TTS / CV…) on the local Snapdragon NPU with one click; supports multi-variant precision switching, benchmarking, and side-by-side comparison |
| **LLM Agent Pipeline** | Orchestrate multiple local NPU models in a single task via natural language — e.g. classify images then upscale matches, all driven by one prompt |
| **Large Language Models (Genie)** | Run Llama 3 / Qwen 3 / Gemma 2 and other quantized LLMs **fully offline** on the NPU via **GenieAPIService** (OpenAI-compatible API); switch between local and cloud models in the same chat UI |
| **Multimodal & Speech & Vision** | Supports multimodal LLMs (e.g. Qwen2.5-VL), ASR (Whisper, Zipformer), TTS (MeloTTS), OCR (PP-OCR), super-resolution, object detection, and more |
| **WebUI Applications** | Bundled WebUI apps (StableDiffusionApp, ImageRepairApp, GenieWebUI) and a streaming chat WebUI — all running on-device, no internet required |
| **Skill Plugin System** | Extend the AI with hot-reloadable Skill plugins; write your own in a single `SKILL.md` file |
| **Native & Float I/O / Multi-Graph / LoRA** | Native (quantized) and float I/O for maximum throughput; multiple model graphs in one session; LoRA adapter support |

> Supports ARM64 Windows, Linux and Ubuntu (e.g. X Elite Windows, QCS8550 Linux, QCM6490 Ubuntu). Use "native" mode I/O to improve data throughput — see [User Guide](https://github.com/qualcomm/qai-appbuilder/blob/main/docs/user_guide.md#native-mode) and [Whisper sample](samples/audio/Speech_Recognition/whisper_base_en/whisper_base_en.py) for reference.

---

## QAI Skills

QAI AppBuilder ships two built-in AI Agent Skills that together cover the full on-device model lifecycle — from sourcing a model all the way to running inference on the Snapdragon NPU.

### Skill 1 — AI Hub Model Run

> **Use this skill when the model you need already exists on [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models).**

The **AI Hub Model Run** skill downloads pre-exported QNN models directly from AI Hub and runs them on the Snapdragon NPU via `qai_appbuilder` (`QNNContext`) — no conversion, no Visual Studio, no QAIRT SDK required.

| What it does | Details |
|-------------|---------|
| **Supported formats** | `QNN_CONTEXT_BINARY` (`.bin`), `QNN_DLC` (`.dlc`), `ONNX` (CPU baseline only), `VOICE_AI`, `TFLITE` |
| **Inference engine** | Always `qai_appbuilder.QNNContext` loading `.bin` / `.dlc` on the NPU/HTP; `onnxruntime` is used only for optional CPU accuracy comparison |
| **Target devices** | Snapdragon X Elite, Snapdragon X2 Elite, Snapdragon X Plus 8-Core |
| **Typical workflow** | Detect chipset → fetch download link from AI Hub → `curl` download → extract ZIP → run inference script |
| **No conversion needed** | Pre-compiled `.bin` / `.dlc` packages load directly — first-run graph compilation takes 5–60 s, subsequent runs are fast |
| **CPU baseline** | Optionally compare NPU output against `onnxruntime` CPU baseline (cosine similarity > 0.999 for float models) |

**Example trigger prompts:**
```
"Download Inception V3 from AI Hub and run inference on my photo"
"Run YOLOv8 object detection on this image using the NPU"
"Use the pre-built Whisper model to transcribe my audio file"
```

---

### Skill 2 — Model Builder

> **Use this skill when you have a custom PyTorch or ONNX model that is NOT available on AI Hub.**

The **Model Builder** skill automates the full QNN conversion pipeline for custom models — from ONNX export all the way to a validated `.bin` context binary running on the Snapdragon NPU.

| What it does | Details |
|-------------|---------|
| **Input formats** | PyTorch (`.pt` / `.pth`) → ONNX export, or existing ONNX (`.onnx`) |
| **Output formats** | QNN context binary (`.bin`), QNN model library (`.dll`), SNPE DLC (`.dlc`) |
| **Supported precisions** | FP16, FP32, W8A16, W8A8, W8A8B8, W4A16, W4A8 |
| **Auto pipeline** | Export → ONNX inspection → operator patching → QNN conversion → context binary generation → inference → accuracy validation vs. ONNX CPU baseline |
| **Operator patching** | Automatically detects and patches unsupported operators (Einsum, GridSample, ScatterND, Mod, Floor…) with QNN-compatible equivalents |
| **Accuracy validation** | Computes cosine similarity between QNN output and ONNX CPU baseline; threshold ≥ 0.99 (FP16/FP32) or ≥ 0.95 (INT8/quantized) |
| **Auto-generated inference code** | Saves a standalone `infer_<model>.py` and `inference_manifest.json` after successful validation — ready for App Builder Pack export |
| **Requires** | QAIRT SDK 2.45+, Visual Studio 2022 Community, Python x64 3.10 (all auto-installed by `Setup.bat`) |
| **Scope** | Best suited for small-to-medium models (recommended < 2 GB); LLM conversion is not yet supported |

**Example trigger prompts:**
```
"Convert my ResNet ONNX model to QNN FP16 and validate the accuracy"
"Convert my custom YOLOv8 model to W8A8 with calibration data and compare against the original"
"Export my PyTorch model to QNN, run inference, and generate an App Builder pack"
```

---

## Quick Start

### Python

```bash
pip install qai-appbuilder
```

### C++

Download the prebuilt binary package from [Releases](https://github.com/qualcomm/qai-appbuilder/releases):

```
QAI_AppBuilder-win_arm64-{Qualcomm® AI Runtime SDK version}-Release.zip
```

Refer to [User Guide](docs/user_guide.md) for full API usage, or follow [tutorial.ipynb](docs/tutorial.ipynb) to set up and run a CV model step by step.

### Environment Setup

Refer to [python.md](docs/python.md) for instructions on setting up the Python (x64) environment to use QAI AppBuilder on Windows on Snapdragon (WoS) platforms.

You can also run the batch file from [QAI AppBuilder Launcher](tools/launcher/) to set up the environment automatically — enabling you to experience the core functionalities within an hour.

---

## Diagram

<div align="center">
  <img src="https://raw.githubusercontent.com/qualcomm/qai-appbuilder/main/docs/images/diagram2.png" alt="QAI AppBuilder Diagram" width="1100" height="482">
</div>

---

## WebUI AI Application

We have developed several [WebUI AI applications](samples/webui/) based on QAI AppBuilder, allowing you to experience them quickly.
All these applications run on a local PC, requiring *no internet connection* and are *completely free*.
You can run WebUI AI applications through the batch file [4.Start_WebUI.bat](tools/launcher/4.Start_WebUI.bat).

> **Note:** Before trying other functions, we suggest that you try these WebUI AI applications first.

| App | Description |
|-----|-------------|
| ImageRepairApp | An image restoration tool designed to repair old or damaged photographs. |
| StableDiffusionApp | A text-to-image generation tool that creates images based on user input. |
| GenieWebUI | A large language model (LLM) interface that enables interactive conversations. |

---

## OpenAI Compatible API Service (GenieAPIService)

Considering that the current mainstream method for invoking LLMs is based on OpenAI-compatible APIs, we have implemented such interfaces in both C++ and Python. This allows application developers to interact with the local large language model running on NPU in a familiar way.

Many third-party applications that support the OpenAI API can seamlessly switch to the local NPU-based model by simply changing the API endpoint.

We have also implemented client sample code for GenieAPIService in both C++ and Python for developer reference.

1. [Python based service](samples/genie/python/README.md): Guide to run OpenAI compatible API services developed with Python.
2. [C++ based service](samples/genie/c++/README.md): Guide to run OpenAI compatible API services developed with C++.

---

## Samples

We have a rich set of samples covering multiple categories. All models are sourced from [AI Hub](https://aihub.qualcomm.com/compute/models) and automatically downloaded on first run.

Use the interactive launcher to run any sample without writing code:

```bash
cd qai-appbuilder\samples
python run_inference.py              # interactive menu
python run_inference.py --list       # list all available models
python run_inference.py --model whisper_base_en --args "--audio_file input.wav"
```

| Category | Description | Link |
|----------|-------------|------|
| **Audio** | TTS (PiperTTS), ASR (Whisper Base/Tiny), Audio Classification (YAMNet) | [audio/](samples/audio/) |
| **Computer Vision** | Image classification, object detection, segmentation, depth estimation, pose estimation, face analysis, super-resolution, inpainting | [computerVision/](samples/computerVision/) |
| **Generative AI** | Stable Diffusion v1.5 / v2.1 / v3.5 (text → image) | [generativeAI/](samples/generativeAI/) |
| **Multimodal** | OCR (EasyOCR), text embedding (NomicEmbed), CLIP, Chinese→English translation (OpusMT), VLM (Qwen-VL) | [multimodal/](samples/multimodal/) |
| **WebUI Apps** | Gradio-based apps: ImageRepairApp, StableDiffusionApp, GenieWebUI | [webui/](samples/webui/) |
| **Apps** | StorySeed (AI story + image → Xiaohongshu), FletUI desktop app | [apps/](samples/apps/) |
| **Genie LLM Service** | OpenAI-compatible LLM API service (Python + C++) for Llama, Qwen, Phi, Granite | [genie/](samples/genie/) |
| **Android** | GenieChat (LLM/VLM) and SuperResolution Android apps | [android/](samples/android/) |
| **C++** | C++ inference samples for Real-ESRGAN, BEiT | [c++/](samples/c++/) |

See [samples/README.md](samples/README.md) for the full guide including environment setup, model download instructions, and run examples.

---

## Tools

### 1. QAI AppBuilder Launcher
[QAI AppBuilder Launcher](tools/launcher/) — enables you to experience the core functionalities of QAI AppBuilder within an hour.

### 2. DLC2BIN
[DLC2BIN](./tools/convert/dlc2bin/) — converts the general DLC model format into the BIN format optimized for a specific platform.

### 3. ONNX2BIN
[ONNX2BIN](./tools/convert/onnx2bin/) — converts the ONNX model format into the BIN format optimized for a specific platform.

### 4. ONNXWRAPPER
[ONNXWRAPPER](./tools/onnxwrapper/) — a wrapper to run ONNX inference code with a QNN model, which switches to the QNN runtime automatically.

### 5. SKILLS
[SKILLS](./tools/skills/) includes 3 skills:
- [genie_api_service](./tools/skills/knowledge-skills/genie_api_service) — GenieAPIService technical documentation retrieval
- [qai_app_builder](./tools/skills/knowledge-skills/qai_app_builder) — QAI AppBuilder technical documentation retrieval
- [qai-runner-skill](./tools/skills/qai-runner-skill) — QAIRT model conversion & inference on Qualcomm devices

---

## Models

### Model Hub

| Hub | Link |
|-----|------|
| AI Hub | [aihub.qualcomm.com](https://aihub.qualcomm.com/compute/models) |
| AI Dev Home | [aidevhome.com](https://www.aidevhome.com/data/models/) |

### LLM Models

| Model | Link |
|-------|------|
| Qwen2 7B SSD | [Download](https://www.aidevhome.com/data/adh2/models/8380/qwen2_7b_ssd_250702.html) |
| DeepSeek-R1-Distill-Qwen-7B | [Download](https://aiot.aidlux.com/zh/models/detail/78) |

---

## Blog & Documentation

### Official Docs

| Guide | Links |
|-------|-------|
| QAI AppBuilder Guide | [English](docs/guide_en.md) \| [中文](docs/guide_zh.md) |
| GenieAPIService (OpenAI Compatible API) | [English](docs/genie_guide_en.md) \| [中文](docs/genie_guide_zh.md) |
| Qwen2.5-VL-3B On-Device Deployment | [English](samples/genie/c%2B%2B/docs/Qwen2.5-VL-3B-Quickly-Start.md) \| [中文](https://blog.csdn.net/csdnsqst0050/article/details/157474571) |
| QAI AppBuilder WoS PDF | [PDF](https://docs.qualcomm.com/bundle/publicresource/80-94755-1_REV_AA_QAI_AppBuilder_-_WoS.pdf) |
| QAI AppBuilder on Linux (QCS6490) | [English](https://docs.radxa.com/en/dragon/q6a/app-dev/npu-dev/qai-appbuilder) |

### Blog Posts

> The following blog posts are in Chinese (中文).

| Title | Link |
|-------|------|
| 3分钟上手，在骁龙AI PC上部署DeepSeek | [中文](https://blog.csdn.net/csdnsqst0050/article/details/149425691) |
| 本地 OpenAI 兼容 API 服务的配置与部署 | [中文](https://blog.csdn.net/csdnsqst0050/article/details/150208814) |
| Qwen2.5-VL-3B 多模态模型端侧部署 | [中文](https://blog.csdn.net/csdnsqst0050/article/details/157474571) |
| BGE-Base-Zh-V1.5 端侧使用教程 | [中文](https://blog.csdn.net/csdnsqst0050/article/details/157651536) |
| Qwen3-Reranker-0.6B 使用指南 | [中文](https://blog.csdn.net/csdnsqst0050/article/details/158846858) |
| Qwen3-embedding-0.6B 使用指南 | [中文](https://blog.csdn.net/csdnsqst0050/article/details/159389533) |
| Qwen3-8B-8K 模型端侧部署指南 | [中文](https://blog.csdn.net/csdnsqst0050/article/details/160557753) |
| 高通平台大语言模型精选 | [中文](https://www.aidevhome.com/?id=51) |
| Qwen2 7B SSD 使用教程 | [中文](https://www.aidevhome.com/?id=29) |
| Qwen2.5 3B 使用教程 | [中文](https://www.aidevhome.com/?id=36) |
| Genie API Service 配置与使用 | [中文](https://www.aidevhome.com/?id=52) |
| GenieChat：Genie API Service 安卓应用开发 | [中文](https://www.aidevhome.com/?id=50) |
| SuperResolutionApp：图片超分 Android 开发示例 | [中文](https://www.aidevhome.com/?id=53) |

---

## Third-Party App List

| App | Description |
|-----|-------------|
| [stable-diffusion-webui Extension](https://github.com/quic/wos-ai-plugins/tree/main/plugins/stable-diffusion-webui/qairt_accelerate) | Stable Diffusion WebUI plugin accelerated by QAIRT |
| [Blender ControlNet Plugin](https://github.com/quic/wos-ai-plugins/tree/main/plugins/blender/SnapdragonImageGeneration) | Blender image generation plugin for Snapdragon |
| [无痕修图软件](https://www.aidevhome.com/?id=30) (Inpainting App) | AI-powered photo inpainting tool |
| [图片超分器](https://www.aidevhome.com/?id=5) (Super-Resolution Tool) | Image super-resolution upscaler |
| [图片超分应用](https://www.aidevhome.com/?id=37) (Super-Resolution App) | Super-resolution application |
| [视频超分应用](https://www.aidevhome.com/?id=44) (Video Super-Resolution App) | Video super-resolution upscaler |
| [图片消除器](https://www.aidevhome.com/?id=4) (Object Removal Tool) | AI-powered object removal from images |
| [图片搜索应用](https://www.aidevhome.com/?id=31) (Image Search App) | AI-powered image search application |

---

## QAI AppBuilder Components

There are two ways to use QAI AppBuilder:

### 1. C++ Library

Download the prebuilt binary package from [Releases](https://github.com/qualcomm/qai-appbuilder/releases) and link the headers and `.lib`/`.so` files into your C++ project:

```
QAI_AppBuilder-win_arm64-{Qualcomm® AI Runtime SDK version}-Release.zip
```

### 2. Python Binding

Install via pip (see [Quick Start](#quick-start)) or download a specific wheel for your Python version from [Releases](https://github.com/qualcomm/qai-appbuilder/releases).

---

## Build

You can use the pre-compiled version directly — download the version you need from [Releases](https://github.com/qualcomm/qai-appbuilder/releases). To build other wheel variants, refer to [BUILD.md](BUILD.md).

---

## License

QAI AppBuilder is licensed under the BSD 3-clause "New" or "Revised" License. Check out the [LICENSE](LICENSE) for more details.

---

## Star History

<a href="https://www.star-history.com/#qualcomm/qai-appbuilder&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=qualcomm/qai-appbuilder&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=qualcomm/qai-appbuilder&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=qualcomm/qai-appbuilder&type=Date" />
 </picture>
</a>

---

## Disclaimer

This software is provided "as is," without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks.

Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.
