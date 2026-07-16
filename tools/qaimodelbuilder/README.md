# 🤖 QAI ModelBuilder

> **A local-first AI workspace for Snapdragon® X-Series PCs — built on a clean DDD/Clean-Architecture core. Featuring an on-device App Builder workbench (ASR / TTS) running natively on the Snapdragon NPU, an AI-driven QNN Model Conversion & Optimization skill (Model Builder), multi-agent streaming chat, and a built Vue 3 WebUI.**

[![Platform](https://img.shields.io/badge/Platform-Windows_on_ARM-blue?logo=windows)](https://www.microsoft.com/windows)
[![Snapdragon](https://img.shields.io/badge/NPU-Snapdragon_X_Elite%2FPlus-red?logo=qualcomm)](https://www.qualcomm.com/snapdragon)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![Vue3](https://img.shields.io/badge/Frontend-Vue3_%2B_Vite-4FC08D?logo=vue.js)](https://vuejs.org/)
[![中文文档](https://img.shields.io/badge/文档-中文版-red?logo=googletranslate&logoColor=white)](README.zh-CN.md)

---

> ⚡ **In a hurry?** See **[`QUICK-START.md`](QUICK-START.md)** for a 1-page
> cheat-sheet covering the three usage modes — **dev (run from source) / desktop
> app (Tauri) / release artifact** — and which `.bat` to run when.

---

## 📖 Table of Contents

- [Features](#-features)
- [🏛️ Architecture Overview](#-architecture-overview)
- [🧩 App Builder — Run On-Device AI Models](#-app-builder--run-on-device-ai-models)
  - [LLM Multi-Model Agent Pipeline](#llm-multi-model-agent-pipeline)
  - [Multi-Variant Packs](#multi-variant-packs-fp16--int8--w8a16)
  - [Promote Your Own Pack from Model Builder](#promote-your-own-pack-from-model-builder)
- [🌟 Highlight: Model Builder Skill](#-highlight-model-builder-skill)
- [💬 Chat — Multi-Agent Streaming](#-chat--multi-agent-streaming)
  - [Built-in Tools](#built-in-tools)
  - [Try These](#try-these)
- [🚀 Installation](#-installation)
- [▶️ Quick Start](#-quick-start)
- [📁 Project Structure](#-project-structure)
- [⚙️ Configuration](#-configuration)
- [⚡ Skill System](#-skill-system)
- [💻 Requirements](#-requirements)
- [❓ FAQ](#-faq)

---

## ✨ Features

| Module | Description |
|--------|-------------|
| 🧩 **App Builder** | **[HIGHLIGHT]** A built-in workbench that runs ready-to-use **Model Packs** on the local NPU with one click. Self-contained Pack format (`manifest.json` + `runner.py` + optional `SKILL.md`), multi-variant precision switching, a **Sticky Worker** for sub-second repeat inference, history / compare / benchmark / share, and an optional **LLM Agent Pipeline** that lets a cloud LLM orchestrate multiple Packs via `appbuilder_run` tool calls. |
| 🚀 **Model Builder Skill** | **[HIGHLIGHT]** AI-driven model conversion & optimization — convert PyTorch/ONNX models to QNN format at multiple precisions (FP16/FP32/W8A16/W8A8/W4A16/W4A8…), run inference, and validate results on the Snapdragon NPU — all via natural-language chat. **Best for small-to-medium models under ~2 GB; LLM conversion is NOT supported.** |
| 💬 **Multi-Agent Streaming Chat** | Real-time **WebSocket** streaming (with automatic **SSE** fallback), full Markdown + syntax highlighting, plus a **Discussion mode** where multiple named agent personas debate a topic and converge on an implementation plan. |
| 🔥 **Snapdragon NPU Inference** | Run LLMs locally and offline on the Snapdragon X Elite / X Plus NPU via the GenieAPIService daemon (managed start / stop / status / model-load / live logs). |
| 🤖 **Multi-Model Support** | Seamlessly switch between local NPU models and cloud models (OpenAI-compatible providers). |
| ⚡ **Skill Management** | Plugin-based Skill system with hot-reload and automatic system-prompt injection; built-in chat skills plus user-installable skills. |
| 🪝 **Chat Action Hooks** | Bind shell commands to chat lifecycle events from the Settings → Hooks tab. |
| 🔒 **Secure Storage & Permission Gating** | API keys stored via OS keyring (with encrypted fallback) — never written in plaintext. Tool execution is gated by a `PolicyCenter` + Protected Paths + FileBroker software guardrail stack with a permission-approval workflow. |
| 📥 **Download Center** | Browse and download Snapdragon NPU-optimized quantized models from a remote release manifest, powered by aria2c multi-threaded downloading with resume + checksum verification. |
| 🎨 **Dark / Light Theme & i18n** | Switchable dark/light theme, responsive layout, and English / Chinese UI. |

---

## 🏛️ Architecture Overview

QAI ModelBuilder has been **fully rewritten as a Clean Architecture / DDD application**. The codebase is organized into bounded contexts under `src/qai/`, an application entry layer under `apps/`, a thin protocol-adapter layer under `interfaces/`, and out-of-the-box assets under `factory/`.

```
┌──────────────────────────────────────────────────────────────┐
│  apps/        Application entry layer (FastAPI app, CLI)       │
│  interfaces/  Protocol adapters: HTTP / WS / SSE / Webhook     │
├──────────────────────────────────────────────────────────────┤
│  src/qai/     Bounded contexts (domain ⇐ application ⇐ adapters)│
│   chat · app_builder · model_builder ·                       │
│   model_catalog · model_runtime · security ·                  │
│   tools · user_prefs · dependency_approval ·                  │
│   command_policy · service_release · platform (shared kernel) │
├──────────────────────────────────────────────────────────────┤
│  frontend/    Vue 3 + Vite SPA  →  served from frontend/dist/  │
└──────────────────────────────────────────────────────────────┘
```

| Context | Purpose |
|---------|---------|
| `chat` | Multi-tab conversations / messages, multi-agent discussion, OpenAI-compatible entry |
| `app_builder` | On-device Model Pack execution, runs, artifacts, share, benchmarking |
| `model_builder` | Model conversion + Promote-to-App-Builder export |
| `model_catalog` | Downloadable model/skill catalog (remote release manifest + aria2c) |
| `model_runtime` | GenieAPIService daemon control (start/stop/status/load/logs) |
| `security` | Permission-approval workflow, FileGuard / FileBroker, sandbox grants, audit |
| `tools` | Tool execution (FileBroker etc.) |
| `user_prefs` | Durable user preferences (`kv_user_prefs` table) |
| `dependency_approval` / `command_policy` | Dependency-install / command-execution approval queues |
| `service_release` | GenieAPIService download center |
| `platform` | Shared kernel: config, logging, persistence, crypto, edition, skills, uploads… |

---

## 🧩 App Builder — Run On-Device AI Models

> **The fastest way to use AI models on your Snapdragon NPU.** App Builder is a workbench inside the chat UI where each model is a self-contained **Model Pack** under `factory/app_builder/models/<id>/`. Upload an image / record audio / type text, click **Run**, and the result is rendered with the right viewer. Everything runs on-device via QAI AppBuilder + QNN HTP — **nothing is uploaded**.

### Quick Tour

1. **Open App Builder** from the chat input toolbar (next to `Model Builder` and `Coding`). The workbench appears above the chat list — your conversation stays visible.
2. **Pick a category** from the left rail (ASR / TTS / …).
3. **Pick a model** from the top strip. Each card shows runtime · delegate · quantization · typical latency · variant count.
4. **Provide input**: drop an image, pick from `📎 Examples`, record audio with the built-in MediaRecorder (Chrome / Edge desktop), or type text.
5. **Adjust params** if needed (each Pack declares its own param schema; advanced params are folded by default).
6. **Click `[Run]`** — watch live `status` → `progress` → `metrics` → `result` events stream in. The output panel auto-renders the right viewer for the model's output kind.
7. **Iterate**: `Re-run`, `Send to Chat` (push the result into the conversation so the LLM can summarize / extract / translate it), `Add to Compare`, `Download`, or `Share Link`.

> The `🔒 On-Device` badge is shown at all times — input never leaves the machine. NPU runs are serialized with a queue indicator (`Up next` / `{n} ahead`).

### Built-in Model Packs

| Pack | Category | Input → Output | Notes |
|------|----------|----------------|-------|
| **Whisper Base** (`whisper-base`) | ASR | audio → JSON (segments + timestamps) | English; `transcribe` / `translate` |
| **Zipformer ZH** (`zipformer-zh`) | ASR | audio → JSON (text + timestamps) | Chinese, INT8, faster than Whisper Base |
| **MeloTTS ZH** (`melotts-zh`) | TTS | text → wav 24 kHz | Chinese; voice + speed control, full-NPU W8A16 |

> The built-in Packs currently cover **ASR / TTS**. The Pack taxonomy also supports SR / CV and more — convert your own models with [Model Builder](#-highlight-model-builder-skill) and one-click **Promote to App Builder** to add new Packs (e.g. image classification or super-resolution). Demo models such as `inception-v3` / `real-esrgan` ship as part of the `aihub-model-run` user skill, not as built-in App Builder Packs.
>
> **Adding a new Pack is a directory copy:** clone the `_template`, edit `manifest.json` + `runner.py`, drop weights in. The backend auto-scans `factory/app_builder/models/*/manifest.json` at startup.

### Key Capabilities

- **Multi-variant precision** — a single Pack can ship multiple `variants[]` (e.g. FP16 + INT8 + W8A16); the workbench shows a `VariantSwitcher` and live-refreshes inputs / params / metrics. See [Multi-Variant Packs](#multi-variant-packs-fp16--int8--w8a16) below.
- **Sticky Worker** — the inference subprocess stays alive between runs (`src/qai/app_builder/infrastructure/sticky_worker/`). First run pays the model-load cost (~3 s NPU contexts + dependency warmup); subsequent runs on the same model reuse the loaded `QnnContext` and typically complete **< 1 s**. After ~10 minutes idle the NPU is auto-released (graceful shutdown with a 5 s force-kill timeout); worker crashes fall back transparently to a one-shot subprocess.
- **LLM Agent Pipeline** — in App Builder mode with a cloud model, the LLM can orchestrate your Packs via `appbuilder_run` / `appbuilder_batch_run`. See [below](#llm-multi-model-agent-pipeline).

#### History · Compare · Benchmark · Share

Every run is persisted, unlocking these features:

| Feature | What it does |
|---------|--------------|
| **History panel** | Per-model list of recent runs with a snapshot view that re-renders the input + output as it was. |
| **Compare tray** | Several runs side by side — Cards (full output renderers), Table (model / runtime / latency / memory / size / confidence / user score), and a Radar chart. |
| **Benchmark** | Runs N iterations (default 1, up to 100) and reports p50 / p90 / p99 / min / max / mean / std as a live stream. |
| **Batch** | Accepts a dataset directory and runs all entries serially (with stop-on-error). |
| **Export** | `GET /api/app-builder/runs/{id}/export.md` generates a multi-section Markdown report (model card, params, metrics, output, logs, environment, reproduction command). |
| **Share Link** | Creates a secret token + TTL so a read-only result can be opened via a share URL. |
| **Result cache** | A management-side LRU cache keyed by a single SHA-256 over `(model, variant, inputs, params)`; the Run button always executes a fresh inference (it does not replay from cache). |
| **User feedback** | 👍 / 👎 buttons feed a quality score back into history and refresh the LLM agent's catalog for smarter selection next turn. |
| **Keyboard** | `Ctrl/Cmd+Enter` to Run · `Esc` to close the history panel / drawers. |

> Runs are persisted to the unified `data/db/qai.db` (`app_builder_run` + `app_builder_artifact` tables). App Builder HTTP endpoints live under the `/api/app-builder` prefix.

> **Privacy:** the `🔒 On-Device` badge is always shown — input never leaves the machine. A cloud LLM (when used) only sees the summarized result text you choose to send, never the raw input file.

### LLM Multi-Model Agent Pipeline

> **Let a cloud LLM orchestrate your local AI models.** In **App Builder mode** with a cloud model, the system prompt is augmented with a catalog of every installed Pack (id / category / I/O kind / params / typical latency / historical user rating), and the LLM gets two function-calling tools: `appbuilder_run` (single inference) and `appbuilder_batch_run` (batch, NPU-serialized).

**Example** — *"Find images containing flowers in `C:\test\images` and super-resolve them."* The LLM plans the chain autonomously:

1. `glob` — list the directory
2. `appbuilder_run(modelId="inception-v3", inputs={"image": "…"})` × N — classify each image
3. filter results whose top-1 label is flower-like
4. `appbuilder_run(modelId="real-esrgan-x4plus", inputs={"image": "…"})` × M — upscale the matches
5. summarize: how many were flowers, where the upscaled outputs were saved

> **Prerequisite:** `inception-v3` / `real-esrgan-x4plus` are **not** pre-installed — convert them with [Model Builder](#-highlight-model-builder-skill) and promote them to App Builder first.

- **Smart selection** — when several Packs cover the same category, the LLM sees each one's aggregated latency and historical user rating (the 👍/👎 buttons feed back into the catalog) and picks the best one.
- **Path security** — external paths go through the same `PolicyCenter` approval flow as `read` / `glob` / `grep`: first access pops an authorization dialog; granted paths are reused for the session. **No need to copy files into an uploads folder.**
- **Variant locking** — `appbuilder_run` accepts an optional `variantId` to lock to a specific precision (e.g. `int8` for benchmarks, `fp16` for accuracy comparisons).

### Multi-Variant Packs (FP16 / INT8 / W8A16…)

A single Pack can ship multiple precision variants of the same logical model. Each variant declares its own `runtime` (quantization, model size, context bins, supported devices), `assets`, and `metrics`:

```jsonc
"variants": [
  { "id": "fp16", "label": "FP16", "default": true,
    "runtime": { "backend": "qnn", "delegate": "htp", "quantization": "fp16", "modelSizeMB": 23 },
    "metrics": { "latencyMs": 1500, "memoryMB": 200 } }
]
```

In the workbench, a Pack with **≥ 2 variants** automatically gets a `VariantSwitcher` next to the model picker; inputs / params / metrics live-refresh when you switch precision. Single-variant Packs render exactly as before.

### Promote Your Own Pack from Model Builder

Once you've converted a model with [`model-builder`](#-highlight-model-builder-skill), one action promotes the result into App Builder:

1. Finish a Model Builder run (the converted `.bin` files land under the workspace `output/` folder).
2. In the Model Builder UI a **Promote to App Builder** card scans the output, detects each precision (`fp16` / `fp32` / `int8`→`w8a8` / `w8a16` / `w4a16` / `int4`→`w4a8` / `w8a8b8`), and lets you check the variants to ship and pick a default.
3. The importer (`FileSystemAppImportAdapter`, `src/qai/app_builder/infrastructure/app_import_adapter.py`) runs a `scan-bins → dry-run → commit → rollback`-safe cycle — `dry-run` validates the runner, weight checksums, and schema and smoke-tests the default variant on the NPU; `commit` atomically copies the Pack into `factory/app_builder/models/<id>/` and exposes it in App Builder.

---

## 🌟 Highlight: Model Builder Skill

> **The most powerful feature of QAI ModelBuilder** — a built-in chat skill (`factory/chat_features/model-builder/`) that lets you convert, quantize, and validate AI models for the Qualcomm Snapdragon NPU through natural-language conversation, powered by cloud LLMs.

> ### ⚠️ Scope & Limitations
>
> **Model Builder is optimized for small-to-medium models (recommended below ~2 GB)** — typical CV / small NLP models such as image classification, object detection, semantic segmentation, super-resolution, etc.
>
> **❌ Large Language Models (LLMs) are NOT supported.** To run LLMs (Llama, Qwen, Gemma, …) on the Snapdragon NPU, download a pre-converted NPU quantized model from the [📥 Download Center](#-configuration) and run it through [GenieAPIService](#genieapiservice-setup).

### What is Model Builder?

By chatting with the AI you can:

- **Convert** PyTorch or ONNX models to QNN BIN / SNPE DLC format
- **Quantize** models to FP16, FP32, W8A16, W8A8, W4A16, W4A8 (and INT8)
- **Generate** context binaries optimized for the Snapdragon HTP (Hexagon Tensor Processor)
- **Run inference** and validate results on Windows-on-Snapdragon (WoS) ARM64 devices
- **Patch unsupported operators** automatically when conversion fails
- **Analyze & compare** model outputs across precisions

### Three Conversion Flows

| Flow | Output | When to use |
|------|--------|-------------|
| **Flow A — QNN** (`ONNX → C++/BIN → DLL → .bin`) | QNN context binary | Default, most robust — fully automated by `run_pipeline.py` |
| **Flow B — SNPE** | `.dlc` | Android / DSP targets |
| **Flow C — DLC → bin** (`qairt-converter + qairt-quantizer + qnn-context-binary-generator`) | `.bin` | CLE accuracy fallback / cross-device |

The skill scripts (`run_pipeline.py`, `qai_convert_fp.py`, `qai_convert_int.py`, `qai_inspect_onnxio.py`, `qai_dev_gen_contextbin.py`, …) live under `factory/chat_features/model-builder/scripts/`. They read `data/config/qairt_env.json` (auto-generated by `Setup.bat`), initialize the VS ARM64 environment internally, and handle HTP file copy + context-binary generation automatically.

### Supported Conversion Targets

| Format | Description |
|--------|-------------|
| **QNN FP16** | Half-precision float — best balance of speed and accuracy on HTP |
| **QNN FP32** | Full-precision float — maximum accuracy |
| **QNN W8A16** | 8-bit weights, 16-bit activations — good accuracy with reduced size |
| **QNN W8A8** | 8-bit weights and activations — maximum compression |
| **QNN W4A16** | 4-bit weights, 16-bit activations — ultra-compact |
| **QNN W4A8** | 4-bit weights, 8-bit activations |
| **SNPE DLC** | Snapdragon Neural Processing Engine DLC format |

> **Tested platform:** Windows on Snapdragon (WoS) X Elite — convert + run inference on the same device. HTP v73 (8380) / v81 (8480).

### How to Use Model Builder

1. **Activate** — in the chat input toolbar, switch to the **Model Builder** mode (you can upload a model file and pick a target precision).
2. **Describe your task** in natural language — the AI runs the whole pipeline.

#### Example Prompts (real-world tested)

**Image classification — Inception V3:**

```
Help me download the original inception_v3 model, convert it to QNN models
with FP16 and W8A8 precision, run inference, and compare the differences
between the two. Use C:\test\images\dog.jpg as the test image.
Automatically execute all steps until the task is completed.
```

**Super-resolution — Real-ESRGAN x4plus:**

```
Help me download the original real_esrgan_x4plus model, convert it to QNN
with FP16 and W8A8 precision respectively, run inference, and compare the
accuracy of the two. Use C:\test\images\flower.jpg as the test image.
```

**Object detection — YOLOv8:**

```
Download the original YOLOv8 model, convert it to QNN FP16 and W8A8,
run inference, and compare detection accuracy between the two precisions.
Use C:\test\images\yolo.jpg as the test image.
```

**Quick single-precision conversion:**

```
Convert my model.onnx to QNN FP16 and run inference on C:\test\images\sample.jpg
```

The AI downloads the source model, exports to ONNX, converts to the requested precisions, runs inference, and produces a side-by-side accuracy comparison report — in one go. For a structured project workflow, ask it to initialize a workspace (default root `C:\WoS_AI\<model_name>\`), edit `qai_plan.md`, then say *"Do all project work."*

#### Improving Quantized Model Accuracy

If a quantized model (W8A8 / W8A16 / W4A8 / W4A16) gives poor accuracy, just describe the problem to the AI — it will guide you through the fix:

| # | Approach | How to apply |
|---|----------|-------------|
| 1 | **Use real calibration data** | Provide actual images from your target domain instead of synthetic data — better represents the model's input distribution. |
| 2 | **Increase calibration sample count** | Use 20–200 representative images instead of the default small set (more samples improve accuracy, but conversion time scales up). |
| 3 | **Switch to higher precision** | Try W8A16 instead of W8A8, or fall back to FP16 for sensitive layers. |
| 4 | **Ask the AI to diagnose & fix** | Describe the accuracy problem in chat — the AI analyzes the output, identifies problematic layers, applies per-layer overrides, or patches operators. |
| 5 | **Per-layer mixed precision** | Keep specific sensitive layers (e.g. first/last conv) in FP16 while quantizing the rest. |
| 6 | **Compare FP16 vs quantized** | Run both on the same image and ask the AI to compute cosine similarity / PSNR to judge whether the gap is acceptable. |

> **No AI expertise required.** Just describe what you observe and the AI proposes and executes the appropriate fix.

### Prerequisites

`Setup.bat` installs everything automatically: the QAIRT SDK (~2 GB), Visual Studio 2022 C++ ARM64 build tools, an x86_64 Python 3.10 venv for the QNN converters, and the ARM64 Python 3.13 runtime. Pass `--no-builder` to `Setup.bat` to skip the conversion toolchain if you only need chat / App Builder.

---

## 💬 Chat — Multi-Agent Streaming

- **Streaming transport:** the primary link is **WebSocket** (`/api/chat/ws`); if WS retries are exhausted it falls back automatically to **SSE** (`/api/chat/conversations/{id}/stream`). Both consume the same stream-frame protocol.
- **Discussion mode:** multiple named agent personas take turns debating a topic (each frame carries a `sender_id`, with optional pinned speaker), then converge into an implementation plan. Discussion mode streams over SSE.
- **Role templates / personas:** define reusable **Agent Templates** (a single role: display name + model + persona), **Roster Templates** (a reusable group of roles), and **Mode Templates** (discussion presets).
- **Markdown + syntax highlighting**, multi-tab conversations, and an OpenAI-compatible API surface (`/v1/chat/completions`, `/v1/models`).
- **Native Mermaid rendering:** fenced ` ```mermaid ` blocks in a reply are rendered as live diagrams (flowchart / sequence / state / class / ER / gantt / pie / mindmap …), following the app's light/dark theme.

### Built-in Tools

During a chat turn the assistant can call these built-in tools (it decides when; you just ask in natural language):

| Category | Tool | What it does |
|----------|------|--------------|
| **Files** | `read` | Read a text file (with line offset/limit for large files). |
| | `list` | List the entries of a single directory. |
| | `write` | Create or overwrite a whole file. |
| | `edit` | Make targeted text replacements in one file. |
| | `glob` | Find files by glob pattern (e.g. `src/**/*.ts`). |
| | `grep` | Search file contents by regex, returning `path:line`. |
| | `apply_patch` | Apply a multi-file patch atomically. |
| **Shell** | `exec` | Run a command on Windows (cmd / PowerShell / sh auto-detected, gated by the permission-approval workflow). |
| **Web** | `webfetch` | Fetch a URL and extract its readable content (markdown/text). |
| **Sub-agents** | `agent` | Spawn a sub-agent to autonomously handle a self-contained task. It supports two modes via `subagent_type`: **`explore`** (fast, **read-only** codebase exploration — search/read only, cannot modify) and **`general`** (full-capability research + multi-step work). |
| | `list_subagents` | List the sub-agents already spawned in this conversation. |
| **Skills** | `skill` | Load a matching skill's full instructions on demand by its id (see [Skill System](#-skill-system)). |
| **Planning** | `todowrite` | Maintain a structured to-do list for multi-step tasks. |
| **Interaction** | `question` | Ask you a clarifying question and wait for your answer. |

> The exact set offered in a given turn depends on the mode and provider; cloud and local models receive the same tool set through different transports.

### Try These

Copy any of these into the chat box to see the tools in action:

**Draw a diagram (Mermaid)**
- `Explain this project's architecture with a Mermaid flowchart.`
- `Draw a Mermaid sequenceDiagram of how the main agent calls a sub-agent.`
- `Draw a Mermaid stateDiagram-v2 of the chat tab states: idle, streaming, aborting, error.`

**Explore the codebase (explore sub-agent)**
- `Use an explore sub-agent to find where the chat streaming logic lives, with path:line references.`
- `Dispatch an explore sub-agent to list the contexts under src/qai/chat and 2 key files.`

**Load a skill**
- `What skills do you have?`
- `Load the data-analyst skill and use it to analyze X.`

**Research / multi-step (general sub-agent)**
- `Use a general sub-agent to investigate how model conversion is wired and summarize it.`

---

## 🚀 Installation

> **Platform:** Windows on Snapdragon (ARM64). `Setup.bat` automatically downloads `uv`, Python 3.13 ARM64, PortableGit, and Node.js into `%LOCALAPPDATA%\QAIModelBuilder\` — **no administrator rights and no manual Python install required**.

There are three usage modes (full cheat-sheet in [`QUICK-START.md`](QUICK-START.md)):

| Mode | For | Script chain |
|------|-----|--------------|
| **Dev — run from source** | Contributors, debugging | `Setup.bat` → `Build.bat` → `Start.bat` |
| **Desktop app (Tauri)** | A standalone `.exe` / `.msi` | `Setup.bat --desktop` → `Build.bat --desktop` |
| **Release artifact** | Packaging for end users | `Release.bat [version]` → ship `dist\release\` → end user runs `Setup.bat` → `Start.bat` |

### Launcher scripts (repo root)

| Script | Purpose |
|--------|---------|
| `Setup.bat` | **The single install entry point.** Downloads `uv`, installs Python 3.13 ARM64, creates the venv at `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313`, installs runtime deps (`uv pip install -e .`), initializes the `data/` directory (via `python -m scripts.init.install`), and installs PortableGit / Node+pnpm / QAIRT SDK / VS 2022 / TTS data / WebView2. Flags: `--no-builder` (skip conversion toolchain), `--dev`, `--desktop`, `--no-pause`. |
| `Start.bat` | Starts the server (supervised). The port is **not hard-coded** — the supervisor probes a fallback list and writes the real URL to `data/runtime/server.endpoint.json`, then opens your browser. `Start.bat --reload` enables hot-reload. |
| `Build.bat` | Builds the Vue 3 SPA into `frontend/dist/` (pnpm). `--full` (typecheck+lint+test), `--install`, `--clean`, `--desktop` (Tauri bundle). |
| `Release.bat` | Builds a clean-cutover release artifact ready to ship to end users. |
| `Console.bat` | Opens an interactive shell with the ARM64 venv activated (install extra packages, run Python commands). |
| `Uninstall.bat` | Uninstaller — rolls back what `Setup.bat` installed outside the project dir; **does not delete `data/`**. |

> The legacy `Install.bat` / `Launch.bat` flow has been removed — installation is `Setup.bat`, startup is `Start.bat`.

---

## ▶️ Quick Start

After installation, double-click **`Start.bat`** (or launch the desktop app). The server picks an available port (default **8989**, falling back to `8088 / 7799 / 12989 / 18989 / 28989` if occupied) and opens your browser automatically. The actual URL is written to `data/runtime/server.endpoint.json`.

> Changed the **backend**? Just restart `Start.bat` (Python is interpreted — no build step). Changed the **frontend**? Run `Build.bat`, then `Start.bat`.

---

## 📁 Project Structure

```
QAIModelBuilder/
├── apps/                 # Application entry layer
│   ├── api/              #   FastAPI app factory (create_app), DI container, lifespan
│   └── cli/              #   qai CLI + qai-serve supervisor
├── interfaces/           # Protocol adapters: HTTP / WS / SSE / Webhook routes
├── src/qai/              # Bounded contexts (Clean Architecture / DDD)
│   ├── chat/  app_builder/  model_builder/
│   ├── model_catalog/  model_runtime/  security/
│   ├── tools/  user_prefs/  dependency_approval/  command_policy/  service_release/
│   └── platform/         #   Shared kernel (config, persistence, crypto, edition, skills…)
├── frontend/             # Vue 3 + Vite SPA
│   ├── src/              #   views / components / composables / stores (Pinia) / router
│   └── dist/             #   built output (served by FastAPI at runtime)
├── factory/              # Out-of-the-box assets
│   ├── _source/          #   factory source material (compiler input — never read at runtime)
│   ├── app_builder/      #   built-in Model Packs (models/<id>/manifest.json + runner.py)
│   ├── chat_features/    #   built-in chat skills (code-assist / model-builder / ppt-gen)
│   ├── db_staging/       #   compiled DB seed (*.jsonl)
│   └── config/           #   compiled config seed
├── skills/               # User-installable skills (each with a SKILL.md)
├── scripts/              # build / ci / dev / init / release / setup scripts
├── tools/                # Importable tooling (factory_compiler, install pipeline, openapi)
├── models/               # Pre-placed on-device model weights
├── data/                 # Runtime data (generated by install; git-ignored)
├── vendor/               # Offline wheels (ARM64) + bundled binaries + g2p/nltk data
├── Setup.bat  Start.bat  Build.bat  Release.bat  Console.bat  Uninstall.bat  qai.bat
└── pyproject.toml        # Python dependencies & console scripts (single source of truth)
```

---

## ⚙️ Configuration

> All configuration is done through the WebUI — no manual file editing required.

### Configuration directories

| Directory | Role |
|-----------|------|
| `factory\_source\` | Factory source material — compiler input, **never read at runtime** |
| `factory\` | Compiled, sanitized seed (config + DB staging) — the source for build/install |
| `data\config\` | **Runtime user config + daemon working dir** (`forge_config.json`, `qairt_env.json`) — generated by install from the factory seed |
| `data\bin\` | Downloaded binaries and their co-located config (aria2c, GenieAPIService `service_config.json`) |
| `bin\` | Dev-time toolchain (`uv`, `aria2c`, `7zr`) downloaded by `Setup.bat` |

> The repo root no longer has a `config/` directory — runtime configuration lives under `data\config\`.

### GenieAPIService Setup

GenieAPIService is the local LLM inference daemon that runs models on the Snapdragon NPU. Skip this if you only use cloud models. It is controlled from the dedicated **Service** view (sidebar → Service, route `/service`) — not a Settings tab.

1. Open the WebUI → **Download Center** → the software/versions view, download GenieAPIService and let it install. (Until the binary is installed, the Service page shows a guided "not installed" card.)
2. Open the **Service** view, pick a model from the dropdown, optionally set the port / log level, and click **▶ Start Service**. The status indicator turns green when ready (with PID / uptime shown).
3. Enable **Auto-start on relaunch** to start it automatically next time.

> The install location (`data\bin\`) and the models root (`data\models\`) are now **fixed defaults** — there is no manual path field. Just install the binary and download models through the WebUI.

### Downloadable NPU Models

The **Download Center** browses a remote release manifest of Snapdragon NPU-optimized quantized models (Llama / Qwen / Gemma families and more). Downloads use aria2c multi-threaded acceleration with resume support and sha256 verification. The exact model list is provided dynamically by the manifest — it is not hard-coded in the repo.

### Cloud Model Configuration

Open the WebUI → **Settings → Cloud Models**, add an OpenAI-compatible provider with its Base URL, API key, and model name. API keys are stored via the OS keyring (encrypted fallback) and never written in plaintext. Changes take effect immediately.

### Network Proxy

Configure an outbound proxy in **Settings → App Config** (network section); it applies immediately to all outbound requests.

### Settings tabs

The Settings page has four tabs: **App Config** (service / network / security), **Cloud Models**, **Coding Modes** (coding persona prompts), and **Hooks** (chat action hooks). Theme and language are switched from the sidebar footer.

---

## ⚡ Skill System

Skills are the plugin-extension mechanism. There are two kinds, kept separate by design:

- **Built-in chat skills** (`factory/chat_features/`): `code-assist`, `model-builder`, `ppt-gen`.
- **User-installable skills** (`skills/`): each is a directory with a `SKILL.md`. Current bundled set:

| Skill | Description |
|-------|-------------|
| `aihub-model-run` | Run pre-compiled Qualcomm AI Hub model packages |
| `data-analyst` | Data analysis & visualization |
| `file-manager` | File system operations |
| `weather` | China weather queries |
| `read-arxiv-paper` | Read & summarize arXiv papers |
| `summarize` | Summarize articles / news |
| `stooq-market` | Stock market data |
| `email-163-com` | 163 Mail send/receive |
| `outlook` | Outlook mail |
| `invoice-summary` | Invoice summarization |

### How to Use

Open the WebUI → **Skills**, toggle individual skills on/off. Enabled skills are auto-injected into the system prompt. Click **Reload** to hot-load a new skill without restarting.

### Custom Skills

Create a subdirectory under `skills/` with a `SKILL.md`:

```markdown
---
name: My Custom Skill
description: What this skill does
use_for: Describe when to use this skill
---

# Skill Content
Describe how the AI should use this skill...
```

---

## 💻 Requirements

| Item | Requirement |
|------|-------------|
| **OS** | Windows 11 (ARM64) recommended; Windows 10/11 (x64) for cloud-only use |
| **CPU** (NPU inference) | Qualcomm Snapdragon X Elite or X Plus (Windows on ARM) |
| **Python** | 3.12+ (3.13 ARM64 installed automatically by `Setup.bat`); x64 3.10 for Model Builder conversion |
| **RAM** | 16 GB+ recommended for NPU inference; 8 GB for cloud-only |
| **Storage** | NPU model files are typically 2–8 GB; 20 GB+ free recommended |
| **Network** | Required during installation and for cloud model APIs |
| **QAIRT SDK** | 2.45+ for the Model Builder skill (installed by `Setup.bat`) |
| **Visual Studio** | 2022 Community C++ ARM64 build tools for Model Builder (installed by `Setup.bat`) |

> **Runtime dependencies** are declared solely in `pyproject.toml` (`[project].dependencies`) — `requirements.txt` no longer exists.

### Key Python Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` + `starlette` | Web framework and ASGI server |
| `websockets` | Chat WebSocket transport |
| `httpx` | Async HTTP client (cloud providers, downloads) |
| `pydantic` + `pydantic-settings` | Data validation & settings |
| `sqlalchemy` + `aiosqlite` + `alembic` | Persistence & schema migrations |
| `cryptography` + `keyring` | Secure secret storage |
| `structlog` | Structured logging |
| `qai_appbuilder` | Qualcomm AI Engine inference library (App Builder / Model Builder, ARM64 wheel) |
| `onnx` + `onnxruntime` | ONNX inspection & validation (Model Builder) |

---

## ❓ FAQ

**Q: I can't open the WebUI after starting.**

The server auto-selects a port (default 8989). Check the actual URL printed in the terminal or in `data/runtime/server.endpoint.json`, make sure no firewall is blocking it, and review the terminal output for errors.

**Q: How do I use local NPU models?**

See [GenieAPIService Setup](#genieapiservice-setup) — download, configure, and start it entirely through the WebUI.

**Q: How do I convert my PyTorch/ONNX model to QNN format?**

Run `Setup.bat` (installs the full conversion toolchain), switch the chat to **Model Builder** mode, and describe your task — e.g. *"Convert my model.onnx to QNN FP16 and run inference on C:\test\images\sample.jpg."* The AI runs the whole pipeline.

**Q: What model formats does Model Builder support?**

Input: PyTorch (`.pt` / `.pth`) and ONNX (`.onnx`). Output: QNN context binary (`.bin`), QNN model library (`.dll`), SNPE DLC (`.dlc`). Precisions: FP16, FP32, W8A16, W8A8, W4A16, W4A8 (and INT8).

**Q: Can Model Builder convert LLMs (Llama / Qwen / Gemma)?**

Not yet — it targets small-to-medium models under ~2 GB. To run LLMs on the NPU, download a pre-converted quantized model from the Download Center and run it via GenieAPIService.

**Q: How do I add my own model to App Builder without writing code?**

Convert it with Model Builder, then use **Promote to App Builder** — it scans the output, detects each precision, lets you pick variants and a default, and generates a complete Pack (with a dry-run + smoke-test) exposed in App Builder.

**Q: Why is the second inference on the same model so much faster?**

The **Sticky Worker** keeps the inference subprocess alive between runs on the same model, reusing the loaded `QnnContext` — subsequent runs typically complete in **< 1 s**. Idle 10 minutes auto-releases the NPU; crashes fall back transparently.

**Q: Does App Builder run models in the cloud?**

**Always on-device.** Every Pack runs through QAI AppBuilder + QNN HTP on the local NPU. Inputs never leave your machine; a cloud LLM (when used) only sees summarized result text you choose to send.

---

## 📄 License

QAI ModelBuilder is licensed under the BSD 3-Clause "New" or "Revised" License. See [LICENSE](LICENSE) for details.

---

*Built on a Clean Architecture / DDD core — FastAPI backend + Vue 3 (Vite) frontend.*
