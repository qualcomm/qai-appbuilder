# 🤖 QAI ModelBuilder

> **专为 Snapdragon® X-Series PC 打造的本地优先 AI 工作站，构建于干净的 DDD / Clean Architecture 内核之上。核心亮点包括：在骁龙 NPU 上原生运行的端侧 App Builder 工作台（ASR / TTS）、AI 驱动的 QNN 模型转换与优化技能（Model Builder）、多 Agent 流式聊天，以及构建型 Vue 3 WebUI。**

[![平台](https://img.shields.io/badge/平台-Windows_on_ARM-blue?logo=windows)](https://www.microsoft.com/windows)
[![骁龙](https://img.shields.io/badge/NPU-Snapdragon_X_Elite%2FPlus-red?logo=qualcomm)](https://www.qualcomm.com/snapdragon)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/后端-FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![Vue3](https://img.shields.io/badge/前端-Vue3_%2B_Vite-4FC08D?logo=vue.js)](https://vuejs.org/)
[![English Docs](https://img.shields.io/badge/Docs-English-blue?logo=googletranslate&logoColor=white)](README.md)

---

> ⚡ **想快速上手？** 查看 **[`QUICK-START.zh-CN.md`](QUICK-START.zh-CN.md)** ——
> 一页速查表覆盖三种使用模式（**开发模式 / 桌面 App（Tauri）/ 发布包**），告诉你什么场景该跑哪个 `.bat`。

---

## 📖 目录

- [功能概览](#-功能概览)
- [🏛️ 架构总览](#-架构总览)
- [🧩 App Builder —— 一键运行端侧 AI 模型](#-app-builder--一键运行端侧-ai-模型)
  - [LLM 多模型 Agent Pipeline](#llm-多模型-agent-pipeline)
  - [多精度变体](#多精度变体fp16--int8--w8a16)
  - [从 Model Builder 一键导入自有 Pack](#从-model-builder-一键导入自有-pack)
- [🌟 核心亮点：Model Builder 技能](#-核心亮点model-builder-技能)
- [💬 聊天 —— 多 Agent 流式对话](#-聊天--多-agent-流式对话)
  - [内置工具](#内置工具)
  - [试试这些](#试试这些)
- [🚀 安装](#-安装)
- [▶️ 快速启动](#-快速启动)
- [📁 项目结构](#-项目结构)
- [⚙️ 配置说明](#-配置说明)
- [⚡ Skill 系统](#-skill-系统)
- [系统要求](#-系统要求)
- [常见问题](#-常见问题)

---

## ✨ 功能概览

| 功能模块 | 说明 |
|----------|------|
| 🧩 **App Builder（应用构建器）** | **【核心亮点】** 内置工作台，一键运行打包好的 **Model Pack** 在本机 NPU 上推理。Pack 自包含格式（`manifest.json` + `runner.py` + 可选 `SKILL.md`），支持多精度变体切换、**Sticky Worker** 二次推理亚秒级响应、历史 / 对比 / 基准 / 分享，并提供可选 **LLM Agent Pipeline**——让云端 LLM 通过 `appbuilder_run` 工具自主编排多个 Pack 完成复合任务。 |
| 🚀 **Model Builder（模型构建器）技能** | **【核心亮点】** AI 驱动的模型转换与优化——通过自然语言对话，将 PyTorch/ONNX 模型转换为多种精度的 QNN 格式（FP16/FP32/W8A16/W8A8/W4A16/W4A8 等），在骁龙 NPU 上运行推理并验证。**适用于 ~2 GB 以下的中小型模型；不支持大语言模型（LLM）转换。** |
| 💬 **多 Agent 流式聊天** | 实时 **WebSocket** 流式输出（并自动 **SSE** 回退），完整 Markdown + 代码语法高亮，并提供 **讨论模式**——多个具名 agent 角色围绕话题轮流辩论并收敛成实施计划。 |
| 🔥 **骁龙 NPU 本地推理** | 通过 GenieAPIService daemon（受控的 启动/停止/状态/加载模型/实时日志）在骁龙 X Elite / X Plus NPU 上离线运行 LLM。 |
| 🤖 **多模型支持** | 本地 NPU 模型与云端模型（OpenAI 兼容 provider）无缝切换。 |
| ⚡ **Skill 管理** | 插件式 Skill 系统，支持热加载与系统提示词自动注入；含出厂内置聊天技能 + 用户可安装技能。 |
| 🪝 **聊天动作 Hooks** | 在 设置 → Hooks 标签页把 shell 命令绑定到聊天生命周期事件。 |
| 🔒 **安全存储与权限审批** | API Key 经 OS keyring（含加密兜底）存储，绝不明文落盘。工具执行受 `PolicyCenter` + Protected Paths + FileBroker 三层软件护栏与权限审批流把关。 |
| 📥 **下载中心** | 从远程 release manifest 浏览并下载骁龙 NPU 优化的量化模型，基于 aria2c 多线程下载，支持断点续传 + sha256 校验。 |
| 🎨 **深色/浅色主题与多语言** | 支持深/浅色主题切换、响应式布局、中英文 UI。 |

---

## 🏛️ 架构总览

QAI ModelBuilder 已**全面重写为 Clean Architecture / DDD 应用**。代码按限界上下文组织于 `src/qai/`，应用入口层在 `apps/`，薄协议适配层在 `interfaces/`，出厂资产在 `factory/`。

```
┌──────────────────────────────────────────────────────────────┐
│  apps/        应用入口层（FastAPI app、CLI）                    │
│  interfaces/  协议适配层：HTTP / WS / SSE / Webhook            │
├──────────────────────────────────────────────────────────────┤
│  src/qai/     限界上下文（domain ⇐ application ⇐ adapters）     │
│   chat · app_builder · model_builder ·                       │
│   model_catalog · model_runtime · security ·                  │
│   tools · user_prefs · dep_broker · exec_broker ·             │
│   service_release · platform（共享内核）                       │
├──────────────────────────────────────────────────────────────┤
│  frontend/    Vue 3 + Vite SPA  →  运行时由 frontend/dist/ 提供 │
└──────────────────────────────────────────────────────────────┘
```

| 上下文 | 职责 |
|--------|------|
| `chat` | 多标签对话/消息、多 Agent 讨论、OpenAI 兼容入口 |
| `app_builder` | 端侧 Model Pack 执行、运行记录、产物、分享、基准 |
| `model_builder` | 模型转换 + 一键导入到 App Builder |
| `model_catalog` | 可下载模型/技能目录（远程 release manifest + aria2c） |
| `model_runtime` | GenieAPIService daemon 控制（启动/停止/状态/加载/日志） |
| `security` | 权限审批流、FileGuard / FileBroker、沙箱授权、审计 |
| `tools` | 工具执行（FileBroker 等） |
| `user_prefs` | 持久用户偏好（`kv_user_prefs` 表） |
| `dep_broker` / `exec_broker` | 依赖安装 / 命令执行审批队列 |
| `service_release` | GenieAPIService 下载中心 |
| `platform` | 共享内核：config、logging、persistence、crypto、edition、skills、uploads… |

---

## 🧩 App Builder —— 一键运行端侧 AI 模型

> **在骁龙 NPU 上使用 AI 模型最快的方式。** App Builder 在聊天界面中嵌入了一个工作台，每个模型都是位于 `factory/app_builder/models/<id>/` 的自包含 **Model Pack**。上传图片 / 录音 / 输入文字，点击 **Run** 即可拿到结果，并由对应查看器渲染。所有推理通过 QAI AppBuilder + QNN HTP 在本机完成——**输入数据不上传**。

### 快速上手

1. **打开 App Builder**（聊天输入框工具栏，与 `Model Builder`、`Coding` 平级）。工作台浮现于聊天列表上方——对话历史保持可见。
2. **从左侧选择类别**（ASR / TTS / …）。
3. **从顶部模型条选择具体模型**。每张卡片显示：runtime · delegate · 量化精度 · 典型延迟 · 变体数量。
4. **提供输入**：拖入图片、从 `📎 Examples` 选择内置示例、用内置 MediaRecorder 录音（Chrome / Edge 桌面端），或直接输入文本。
5. **调整参数**（每个 Pack 自带参数 Schema；高级参数默认折叠）。
6. **点击 `[Run]`** —— 实时观察 `status` → `progress` → `metrics` → `result` 事件流。Output 面板按模型输出类型自动选择渲染器。
7. **后续操作**：`Re-run` 重跑、`Send to Chat` 把结果送入对话让 LLM 解读 / 摘要 / 翻译、`Add to Compare` 加入对比、`Download` 下载、`Share Link` 生成只读分享链接。

> **隐私徽章 `🔒 On-Device`** 永久显示——输入永不离开本机。NPU 推理由后端串行队列调度，前端实时显示队列位置（`Up next` / `{n} ahead`）。

### 内置 Model Pack

| Pack | 类别 | 输入 → 输出 | 说明 |
|------|------|-------------|------|
| **Whisper Base** (`whisper-base`) | ASR | audio → JSON（segments + 时间戳）| 英文；`transcribe` / `translate` |
| **Zipformer ZH** (`zipformer-zh`) | ASR | audio → JSON（text + 时间戳）| 中文，INT8，比 Whisper Base 更快 |
| **MeloTTS ZH** (`melotts-zh`) | TTS | text → wav 24 kHz | 中文语音合成；声音 + 语速可调，全 NPU W8A16 |

> 当前内置 Pack 覆盖 **ASR / TTS**。Pack 的分类体系也支持 SR / CV 等更多类别——可用 [Model Builder](#-核心亮点model-builder-技能) 转换自有模型，再一键 **导入到 App Builder** 新增 Pack（如图像分类或超分辨率）。`inception-v3` / `real-esrgan` 等演示模型随 `aihub-model-run` 用户技能提供，并非 App Builder 内置 Pack。
>
> **新增 Pack 只需复制目录：** 拷贝 `_template` 模板，修改 `manifest.json` + `runner.py`，放入权重即可。后端启动时自动扫描 `factory/app_builder/models/*/manifest.json`。

### 核心能力

- **多精度变体** —— 同一个 Pack 可打包多个 `variants[]`（如 FP16 + INT8 + W8A16）；工作台显示 `VariantSwitcher` 并响应式刷新输入 / 参数 / 指标。详见下文 [多精度变体](#多精度变体fp16--int8--w8a16)。
- **Sticky Worker** —— 推理子进程在多次 Run 之间保持存活（`src/qai/app_builder/infrastructure/sticky_worker/`）。首次 Run 支付模型加载开销（约 3 秒 NPU contexts + 依赖预热）；同一模型的后续 Run 复用已加载的 `QnnContext`，端到端典型 **< 1 秒**。空闲约 10 分钟自动释放 NPU（优雅 shutdown，5 秒后强制 kill 兜底）；worker 崩溃自动透明退化为一次性子进程。
- **LLM Agent Pipeline** —— 在 App Builder 模式 + 云端模型下，LLM 可通过 `appbuilder_run` / `appbuilder_batch_run` 自主编排你的 Pack。详见 [下文](#llm-多模型-agent-pipeline)。

#### 历史 · 对比 · 基准 · 分享

每次 Run 都持久化，由此解锁以下能力：

| 功能 | 说明 |
|------|------|
| **历史面板** | 按模型显示最近运行列表，支持 snapshot 快照视图（按当时输入 + 输出原样回放）。 |
| **对比托盘** | 多项 Run 并排对比——Cards（完整输出渲染器）、Table（模型 / runtime / 延迟 / 内存 / 体积 / 置信度 / 用户评分）、Radar 雷达图。 |
| **Benchmark** | 执行 N 次（默认 1，最多 100），以实时流返回 p50 / p90 / p99 / min / max / mean / std。 |
| **Batch** | 接受数据集目录，串行执行所有条目（支持 stop-on-error）。 |
| **导出** | `GET /api/app-builder/runs/{id}/export.md` 生成多章节 Markdown 报告（模型卡、参数、指标、输出、日志、环境、复现命令）。 |
| **分享链接** | 生成 token + TTL，只读结果可通过分享 URL 打开。 |
| **结果缓存** | 管理性 LRU 缓存，键 = 对 `(模型, 变体, 输入, 参数)` 做单次 SHA-256；Run 按钮始终真跑推理（不从缓存回放）。 |
| **用户反馈** | 👍 / 👎 把质量分写入历史，并刷新 LLM agent 的 catalog，下一轮智能选型自动跟进。 |
| **快捷键** | `Ctrl/Cmd+Enter` 运行 · `Esc` 关闭历史面板 / 抽屉。 |

> Run 记录持久化到统一的 `data/db/qai.db`（`app_builder_run` + `app_builder_artifact` 两表）。App Builder 的 HTTP 端点统一在 `/api/app-builder` 前缀下。

> **隐私：** `🔒 On-Device` 徽章永久显示——输入永不离开本机。云端 LLM（如使用）只能看到你主动发送的结果摘要文本，永不接触原始输入文件。

### LLM 多模型 Agent Pipeline

> **让云端 LLM 自主编排你的本地 AI 模型。** 在 **App Builder 模式** 下与云端模型对话时，系统提示词会注入所有已安装 Pack 的能力清单（id / 类别 / I/O 类型 / 参数 / 典型延迟 / 历史用户评分），并向 LLM 暴露两个 function-calling 工具：`appbuilder_run`（单次推理）、`appbuilder_batch_run`（批量，NPU 串行）。

**典型用例** —— *"帮我从 `C:\test\images` 目录中找出含有花的图片，然后对这些图片执行超分辨率修复。"* 云端 LLM 自主规划执行链路：

1. `glob` —— 列出目录中的图片
2. `appbuilder_run(modelId="inception-v3", inputs={"image": "…"})` × N —— 逐张分类
3. 筛选 top-1 标签为花卉的图片
4. `appbuilder_run(modelId="real-esrgan-x4plus", inputs={"image": "…"})` × M —— 对命中图执行超分
5. 汇总报告：含花的图片数量、超分输出文件位置

> **前置条件：** `inception-v3` / `real-esrgan-x4plus` **不在预装清单中**——请先用 [Model Builder](#-核心亮点model-builder-技能) 转换并导入到 App Builder。

- **智能选型** —— 当多个 Pack 覆盖同一类别时，LLM 会看到每个 Pack 的聚合延迟与历史用户评分（👍/👎 反馈到 catalog），选出最优模型。
- **路径安全** —— 外部路径走与 `read` / `glob` / `grep` 相同的 `PolicyCenter` 审批流：首次访问弹出授权对话框，授权后会话内复用。**无需把文件复制到上传目录。**
- **变体锁定** —— `appbuilder_run` 可传入可选的 `variantId` 锁定特定精度（如 `int8` 用于基准、`fp16` 用于精度对比）。

### 多精度变体（FP16 / INT8 / W8A16…）

同一个 Pack 可打包同一模型的多个精度变体。每个变体自带 `runtime`（量化、模型体积、context bins、支持设备）、`assets`、`metrics`：

```jsonc
"variants": [
  { "id": "fp16", "label": "FP16", "default": true,
    "runtime": { "backend": "qnn", "delegate": "htp", "quantization": "fp16", "modelSizeMB": 23 },
    "metrics": { "latencyMs": 1500, "memoryMB": 200 } }
]
```

工作台中，**含 ≥ 2 个变体的 Pack** 会自动在模型选择器旁显示 `VariantSwitcher`；切换精度时输入 / 参数 / 指标响应式刷新。单变体 Pack 渲染零变化。

### 从 Model Builder 一键导入自有 Pack

用 [`model-builder`](#-核心亮点model-builder-技能) 转换完模型后，一键即可推送到 App Builder：

1. 完成一次 Model Builder 运行（转换好的 `.bin` 位于工作区 `output/` 目录）。
2. Model Builder UI 出现 **Promote to App Builder** 卡片，扫描输出、识别每种精度（`fp16` / `fp32` / `int8`→`w8a8` / `w8a16` / `w4a16` / `int4`→`w4a8` / `w8a8b8`），让你勾选要打包的变体并选默认精度。
3. Importer（`FileSystemAppImportAdapter`，`src/qai/app_builder/infrastructure/app_import_adapter.py`）执行 `scan-bins → dry-run → commit → rollback` 安全流程——`dry-run` 校验 runner、权重 checksum、Schema 并对默认变体做 NPU 冒烟测试；`commit` 原子复制 Pack 到 `factory/app_builder/models/<id>/` 并暴露到 App Builder。

---

## 🌟 核心亮点：Model Builder 技能

> **QAI ModelBuilder 最强大的功能** —— 一个内置聊天技能（`factory/chat_features/model-builder/`），让你借助云端 LLM，通过自然语言对话完成 AI 模型到高通骁龙 NPU 的转换、量化与验证全流程。

> ### ⚠️ 适用范围说明
>
> **Model Builder 主要适用于中小型模型（建议 ~2 GB 以下）** —— 如图像分类、目标检测、语义分割、超分辨率等典型 CV / 小型 NLP 模型。
>
> **❌ 暂不支持大语言模型（LLM）转换。** 如需在骁龙 NPU 上运行 LLM（Llama、Qwen、Gemma 等），请从 [📥 下载中心](#-配置说明) 下载已预转换的 NPU 量化模型，并通过 [GenieAPIService](#genieapiservice-安装与启动) 运行。

### 什么是 Model Builder？

只需与 AI 对话即可：

- **转换** PyTorch 或 ONNX 模型为 QNN BIN / SNPE DLC 格式
- **量化** 模型为 FP16、FP32、W8A16、W8A8、W4A16、W4A8（及 INT8）
- **生成** 针对骁龙 HTP（Hexagon 张量处理器）优化的 Context Binary
- **运行推理** 并在 Windows on Snapdragon（WoS）ARM64 设备上验证
- **自动修复** 转换失败时的不支持算子
- **分析对比** 不同精度的模型输出

### 三条转换流程

| 流程 | 输出 | 适用场景 |
|------|------|----------|
| **Flow A — QNN**（`ONNX → C++/BIN → DLL → .bin`） | QNN Context Binary | 默认、最稳健——由 `run_pipeline.py` 全自动 |
| **Flow B — SNPE** | `.dlc` | Android / DSP 目标 |
| **Flow C — DLC → bin**（`qairt-converter + qairt-quantizer + qnn-context-binary-generator`） | `.bin` | CLE 精度兜底 / 跨设备 |

技能脚本（`run_pipeline.py`、`qai_convert_fp.py`、`qai_convert_int.py`、`qai_inspect_onnxio.py`、`qai_dev_gen_contextbin.py` 等）位于 `factory/chat_features/model-builder/scripts/`。它们读取 `data/config/qairt_env.json`（由 `Setup.bat` 自动生成），内部初始化 VS ARM64 环境，自动处理 HTP 文件复制与 Context Binary 生成。

### 支持的转换目标格式

| 格式 | 说明 |
|------|------|
| **QNN FP16** | 半精度浮点——HTP 上速度与精度的最佳平衡 |
| **QNN FP32** | 全精度浮点——最高精度 |
| **QNN W8A16** | 8 位权重 + 16 位激活——精度高、体积小 |
| **QNN W8A8** | 8 位权重 + 8 位激活——最大压缩比 |
| **QNN W4A16** | 4 位权重 + 16 位激活——超紧凑 |
| **QNN W4A8** | 4 位权重 + 8 位激活 |
| **SNPE DLC** | 骁龙神经处理引擎 DLC 格式 |

> **已验证平台：** Windows on Snapdragon（WoS）X Elite —— 在同一设备上完成转换与推理。HTP v73（8380）/ v81（8480）。

### 如何使用 Model Builder

1. **激活** —— 在聊天输入框工具栏切换到 **Model Builder** 模式（可上传模型文件并选择目标精度）。
2. **描述你的需求** —— AI 会自动完成整个流水线。

#### 示例提示词（均经过实际验证）

**图像分类 — Inception V3：**

```
帮我下载原始 inception_v3 模型，将其转换为 FP16 和 W8A8 两种精度的 QNN 模型，
运行推理并比较两者的差异。使用 C:\test\images\dog.jpg 作为测试图片。
自动执行所有步骤，直到任务完成。
```

**超分辨率 — Real-ESRGAN x4plus：**

```
帮我下载原始 real_esrgan_x4plus 模型，分别转换为 FP16 和 W8A8 两种精度的 QNN
模型，运行推理并对比两者的精度。使用 C:\test\images\flower.jpg 作为测试图片。
```

**目标检测 — YOLOv8：**

```
帮我下载原始 YOLOv8 模型，分别转换成 FP16 和 W8A8 两种精度的 QNN 模型，
运行推理并对比两者的检测精度。使用 C:\test\images\yolo.jpg 作为测试图片。
```

**快速单精度转换：**

```
将我的 model.onnx 转换为 QNN FP16，并在 C:\test\images\sample.jpg 上运行推理。
```

AI 会自动完成源模型下载、ONNX 导出、多精度转换、推理执行，并生成精度对比报告——一气呵成。如需结构化项目工作流，可让它初始化工作区（默认根目录 `C:\WoS_AI\<model_name>\`），编辑 `qai_plan.md`，再说 *"执行所有项目工作"*。

#### 量化模型精度不佳时的改进方法

如果量化模型（W8A8 / W8A16 / W4A8 / W4A16）精度不理想，直接把问题描述给 AI，它会引导你完成修复：

| # | 方法 | 如何操作 |
|---|------|----------|
| 1 | **使用真实校准数据集** | 提供来自目标场景的真实图片，而非合成数据——更能代表模型输入分布，显著提升量化精度。 |
| 2 | **增加校准样本数量** | 使用 20～200 张有代表性的图片，而非默认的少量样本（样本越多精度越高，但转换时间相应增加）。 |
| 3 | **切换到更高精度格式** | 用 W8A16 替代 W8A8，或对敏感层回退到 FP16。 |
| 4 | **让 AI 诊断并修复** | 在聊天中描述精度问题——AI 分析输出、定位问题层、应用逐层量化覆盖或修复算子。 |
| 5 | **混合精度量化** | 将特定敏感层（如首尾卷积层）保持 FP16，其余层正常量化。 |
| 6 | **对比 FP16 与量化输出** | 在同一图片上运行两种精度，让 AI 计算余弦相似度或 PSNR，判断精度损失是否可接受。 |

> **无需 AI 专业知识。** 只需描述你观察到的现象，AI 就会提出并执行相应的修复方案。

### 前置要求

`Setup.bat` 自动安装全部环境：QAIRT SDK（~2 GB）、Visual Studio 2022 C++ ARM64 构建工具、用于 QNN 转换器的 x86_64 Python 3.10 venv，以及 ARM64 Python 3.13 运行时。若只需聊天 / App Builder，可给 `Setup.bat` 传 `--no-builder` 跳过转换工具链。

---

## 💬 聊天 —— 多 Agent 流式对话

- **流式传输：** 主链路为 **WebSocket**（`/api/chat/ws`）；WS 重试耗尽后自动回退到 **SSE**（`/api/chat/conversations/{id}/stream`）。两条链路消费同一套 stream-frame 协议。
- **讨论模式：** 多个具名 agent 角色轮流就话题辩论（每帧带 `sender_id`，可点名指定发言者），随后收敛成实施计划。讨论模式走 SSE。
- **角色模板 / Personas：** 可定义可复用的 **Agent Template**（单个角色：显示名 + 模型 + persona）、**Roster Template**（可复用的角色组合）、**Mode Template**（讨论模式预设）。
- **Markdown + 语法高亮**、多标签对话，以及 OpenAI 兼容 API（`/v1/chat/completions`、`/v1/models`）。
- **原生 Mermaid 渲染：** 回复中的 ` ```mermaid ` 代码块会被渲染成实时图表（flowchart / 时序图 / 状态机 / 类图 / ER / 甘特 / 饼图 / 思维导图……），并跟随应用的亮/暗主题。

### 内置工具

聊天回合中，助手可以调用以下内置工具（由它判断何时使用；你只需用自然语言提出需求）：

| 类别 | 工具 | 作用 |
|------|------|------|
| **文件** | `read` | 读取文本文件（大文件支持行 offset/limit）。 |
| | `list` | 列出单层目录的条目。 |
| | `write` | 创建或整体覆盖一个文件。 |
| | `edit` | 对单个文件做精确的文本替换。 |
| | `glob` | 按 glob 模式查找文件（如 `src/**/*.ts`）。 |
| | `grep` | 按正则搜索文件内容，返回 `路径:行号`。 |
| | `apply_patch` | 原子地应用多文件补丁。 |
| **命令** | `exec` | 在 Windows 上执行命令（自动检测 cmd / PowerShell / sh，受权限审批工作流约束）。 |
| **网络** | `webfetch` | 抓取 URL 并提取其可读内容（markdown/text）。 |
| **子 Agent** | `agent` | 派一个子 Agent 自治完成自包含任务。通过 `subagent_type` 支持两种模式：**`explore`**（快速、**只读**的代码库探索 —— 只能搜索/读取，不能修改）与 **`general`**（全能型，复杂研究 + 多步工作）。 |
| | `list_subagents` | 列出本会话已派发的子 Agent。 |
| **技能** | `skill` | 按 id 即时加载某个匹配技能的完整指令（见 [Skill 系统](#-skill-系统)）。 |
| **规划** | `todowrite` | 为多步任务维护一份结构化待办清单。 |
| **交互** | `question` | 向你提出澄清性问题并等待回答。 |

> 某一回合实际提供的工具集取决于模式与 provider；云端与本地模型通过不同传输方式获得同一套工具。

### 试试这些

把下面任意一条复制进聊天框，即可看到这些工具的实际效果：

**画一张图（Mermaid）**
- `用 Mermaid 流程图解释这个项目的架构。`
- `用 Mermaid sequenceDiagram 画出主 Agent 调用子 Agent 的过程。`
- `用 Mermaid stateDiagram-v2 画出聊天 tab 的状态：idle、streaming、aborting、error。`

**探索代码库（explore 子 Agent）**
- `派一个 explore 子 Agent 找出聊天流式逻辑所在，并给出 路径:行号。`
- `派一个 explore 子 Agent 列出 src/qai/chat 下的 context 与 2 个关键文件。`

**加载技能**
- `你有哪些技能？`
- `加载 data-analyst 技能并用它分析 X。`

**研究 / 多步任务（general 子 Agent）**
- `派一个 general 子 Agent 调研模型转换是如何接线的并做总结。`

---

## 🚀 安装

> **平台：** Windows on Snapdragon（ARM64）。`Setup.bat` 自动把 `uv`、Python 3.13 ARM64、PortableGit、Node.js 下载到 `%LOCALAPPDATA%\QAIModelBuilder\` —— **无需管理员权限，无需手动安装 Python**。

共三种使用模式（完整速查见 [`QUICK-START.zh-CN.md`](QUICK-START.zh-CN.md)）：

| 模式 | 适用 | 脚本链 |
|------|------|--------|
| **开发模式（源码运行）** | 贡献者、调试 | `Setup.bat` → `Build.bat` → `Start.bat` |
| **桌面 App（Tauri）** | 需要独立 `.exe` / `.msi` | `Setup.bat --desktop` → `Build.bat --desktop` |
| **发布包** | 给最终用户打包 | `Release.bat [version]` → 分发 `dist\release\` → 用户跑 `Setup.bat` → `Start.bat` |

### 启动器脚本（仓库根）

| 脚本 | 作用 |
|------|------|
| `Setup.bat` | **唯一安装入口。** 下载 `uv`、安装 Python 3.13 ARM64、在 `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313` 建 venv、安装运行时依赖（`uv pip install -e .`）、初始化 `data/` 目录（经 `python -m scripts.init.install`），并安装 PortableGit / Node+pnpm / QAIRT SDK / VS 2022 / TTS 数据 / WebView2。可选参数：`--no-builder`（跳过转换工具链）、`--dev`、`--desktop`、`--no-pause`。 |
| `Start.bat` | 启动服务（受监管）。端口**不硬编码**——监管器探测回退列表，把真实 URL 写入 `data/runtime/server.endpoint.json` 并自动开浏览器。`Start.bat --reload` 启用热重载。 |
| `Build.bat` | 把 Vue 3 SPA 构建到 `frontend/dist/`（pnpm）。`--full`（typecheck+lint+test）、`--install`、`--clean`、`--desktop`（Tauri 打包）。 |
| `Release.bat` | 构建可直接分发给最终用户的 clean-cutover 发布产物。 |
| `Console.bat` | 打开已激活 ARM64 venv 的交互式 shell（装额外包、运行 Python 命令）。 |
| `Uninstall.bat` | 卸载器——回滚 `Setup.bat` 装在项目目录外的内容；**不删除 `data/`**。 |

> 旧的 `Install.bat` / `Launch.bat` 流程已移除——安装用 `Setup.bat`，启动用 `Start.bat`。

---

## ▶️ 快速启动

安装完成后，双击 **`Start.bat`**（或启动桌面 App）。服务会选取可用端口（默认 **8989**，被占用时回退到 `8088 / 7799 / 12989 / 18989 / 28989`）并自动打开浏览器。真实 URL 写入 `data/runtime/server.endpoint.json`。

> 改了**后端**？重启 `Start.bat` 即可（Python 解释执行，无构建步骤）。改了**前端**？先跑 `Build.bat`，再 `Start.bat`。

---

## 📁 项目结构

```
QAIModelBuilder/
├── apps/                 # 应用入口层
│   ├── api/              #   FastAPI app 工厂（create_app）、DI 容器、lifespan
│   └── cli/              #   qai CLI + qai-serve 监管器
├── interfaces/           # 协议适配层：HTTP / WS / SSE / Webhook 路由
├── src/qai/              # 限界上下文（Clean Architecture / DDD）
│   ├── chat/  app_builder/  model_builder/
│   ├── model_catalog/  model_runtime/  security/
│   ├── tools/  user_prefs/  dep_broker/  exec_broker/  service_release/
│   └── platform/         #   共享内核（config、persistence、crypto、edition、skills…）
├── frontend/             # Vue 3 + Vite SPA
│   ├── src/              #   views / components / composables / stores（Pinia）/ router
│   └── dist/             #   构建产物（运行时由 FastAPI 提供）
├── factory/              # 出厂资产
│   ├── _source/          #   出厂源料（编译器输入——运行时绝不读）
│   ├── app_builder/      #   内置 Model Pack（models/<id>/manifest.json + runner.py）
│   ├── chat_features/    #   内置聊天技能（code-assist / model-builder / ppt-gen）
│   ├── db_staging/       #   编译入库种子（*.jsonl）
│   └── config/           #   编译配置种子
├── skills/               # 用户可安装技能（每个含 SKILL.md）
├── scripts/              # build / ci / dev / init / release / setup 脚本
├── tools/                # 可导入工具包（factory_compiler、install pipeline、openapi）
├── models/               # 预置端侧模型权重
├── data/                 # 运行时数据（install 生成；gitignore）
├── vendor/               # 离线 wheel（ARM64）+ 内置二进制 + g2p/nltk 数据
├── Setup.bat  Start.bat  Build.bat  Release.bat  Console.bat  Uninstall.bat  qai.bat
└── pyproject.toml        # Python 依赖与控制台脚本（唯一权威）
```

---

## ⚙️ 配置说明

> 所有配置均可通过 WebUI 完成，无需手动编辑配置文件。

### 配置目录矩阵

| 目录 | 职责 |
|------|------|
| `factory\_source\` | 出厂源料——编译器输入，**运行时绝不读** |
| `factory\` | 编译后、已脱敏的种子（config + DB staging）——build/install 的供给源 |
| `data\config\` | **运行时用户配置 + daemon 工作目录**（`forge_config.json`、`qairt_env.json`）——install 从 factory 种子生成 |
| `data\bin\` | 下载的二进制及其同目录配置（aria2c、GenieAPIService 的 `service_config.json`） |
| `bin\` | 开发期工具链（`uv`、`aria2c`、`7zr`，由 `Setup.bat` 下载） |

> 仓库根**不再有 `config/` 目录**——运行时配置位于 `data\config\`。

### GenieAPIService 安装与启动

GenieAPIService 是在骁龙 NPU 上运行 LLM 的本地推理 daemon。若只用云端模型可跳过。它由独立的 **服务（Service）视图**（侧边栏 → 服务，路由 `/service`）控制，**不是设置页的标签**。

1. 打开 WebUI → **下载中心** → 软件版本视图，下载 GenieAPIService 并安装。（二进制安装前，服务页会显示引导式"未安装"卡片。）
2. 打开 **服务** 视图，从下拉选择模型，可选设置端口 / 日志级别，点击 **▶ 启动服务**。就绪后状态指示灯变绿（并显示 PID / uptime）。
3. 勾选 **重启后自动启动**，下次自动拉起。

> 安装位置（`data\bin\`）与模型根目录（`data\models\`）现为**固定默认值**——不再有手动填路径的输入框。只需通过 WebUI 安装二进制并下载模型即可。

### 可下载的 NPU 模型

**下载中心** 浏览的是骁龙 NPU 优化量化模型的远程 release manifest（Llama / Qwen / Gemma 系列等）。下载使用 aria2c 多线程加速，支持断点续传与 sha256 校验。具体模型清单由 manifest 动态提供——**并非在仓库内硬编码**。

### 云端模型配置

打开 WebUI → **设置 → 云端模型**，添加一个 OpenAI 兼容 provider，填写 Base URL、API Key、模型名称。API Key 经 OS keyring（加密兜底）存储，绝不明文落盘。保存后立即生效。

### 网络代理

在 **设置 → App Config**（网络部分）配置出站代理，立即对所有出站请求生效。

### 设置页标签

设置页共四个标签：**App Config**（服务 / 网络 / 安全）、**Cloud Models**（云端模型）、**Coding Modes**（编码 persona 提示词）、**Hooks**（聊天动作 hooks）。主题与语言在侧边栏底部切换。

---

## ⚡ Skill 系统

Skill 是插件扩展机制，分为两类，设计上彼此隔离：

- **出厂内置聊天技能**（`factory/chat_features/`）：`code-assist`、`model-builder`、`ppt-gen`。
- **用户可安装技能**（`skills/`）：每个是一个含 `SKILL.md` 的目录。当前内置集：

| Skill | 说明 |
|-------|------|
| `aihub-model-run` | 运行高通 AI Hub 预编译模型包 |
| `data-analyst` | 数据分析与可视化 |
| `file-manager` | 文件系统操作 |
| `weather` | 中国城市天气查询 |
| `read-arxiv-paper` | 阅读并摘要 arXiv 论文 |
| `summarize` | 文章 / 新闻摘要 |
| `stooq-market` | 股市行情数据 |
| `email-163-com` | 163 邮箱收发 |
| `outlook` | Outlook 邮件 |
| `invoice-summary` | 发票摘要 |

### 使用方式

打开 WebUI → **Skills**，逐个开关技能。启用的技能自动注入系统提示词。点击 **Reload** 即可热加载新技能，无需重启。

### 自定义 Skill

在 `skills/` 下新建子目录，添加 `SKILL.md`：

```markdown
---
name: 我的自定义 Skill
description: 描述这个 Skill 的功能
use_for: 适用场景描述
---

# Skill 内容
在此描述 AI 应如何使用此 Skill...
```

---

## 💻 系统要求

| 项目 | 要求 |
|------|------|
| **操作系统** | 推荐 Windows 11（ARM64）；仅云端模型可用 Windows 10/11（x64） |
| **处理器**（NPU 推理） | Qualcomm Snapdragon X Elite 或 X Plus（Windows on ARM） |
| **Python** | 3.12+（`Setup.bat` 自动装 3.13 ARM64）；Model Builder 转换需 x64 3.10 |
| **内存** | NPU 推理建议 16 GB 以上；仅云端模型 8 GB 即可 |
| **存储** | NPU 模型文件通常 2～8 GB，建议预留 20 GB 以上 |
| **网络** | 安装时及使用云端模型 API 时需要 |
| **QAIRT SDK** | Model Builder 技能需 2.45+（由 `Setup.bat` 安装） |
| **Visual Studio** | Model Builder 需 2022 Community C++ ARM64 构建工具（由 `Setup.bat` 安装） |

> **运行时依赖** 仅声明于 `pyproject.toml`（`[project].dependencies`）——`requirements.txt` 已不存在。

### 主要 Python 依赖

| 依赖包 | 用途 |
|--------|------|
| `fastapi` + `uvicorn` + `starlette` | Web 框架与 ASGI 服务器 |
| `websockets` | 聊天 WebSocket 传输 |
| `httpx` | 异步 HTTP 客户端（云端 provider、下载） |
| `pydantic` + `pydantic-settings` | 数据校验与配置 |
| `sqlalchemy` + `aiosqlite` + `alembic` | 持久化与 schema 迁移 |
| `cryptography` + `keyring` | 凭据安全存储 |
| `structlog` | 结构化日志 |
| `qai_appbuilder` | 高通 AI 引擎推理库（App Builder / Model Builder，ARM64 wheel） |
| `onnx` + `onnxruntime` | ONNX 检查与验证（Model Builder） |

---

## ❓ 常见问题

**Q：启动后打不开 WebUI？**

服务会自动选端口（默认 8989）。查看终端打印的真实 URL 或 `data/runtime/server.endpoint.json`，确认防火墙未拦截，并检查终端输出是否有错误。

**Q：如何使用本地 NPU 模型？**

参见 [GenieAPIService 安装与启动](#genieapiservice-安装与启动)——全程通过 WebUI 完成下载、配置和启动。

**Q：如何把 PyTorch/ONNX 模型转换为 QNN 格式？**

运行 `Setup.bat`（安装完整转换工具链），把聊天切到 **Model Builder** 模式，描述需求——例如 *"将我的 model.onnx 转换为 QNN FP16，并在 C:\test\images\sample.jpg 上运行推理。"* AI 会自动跑完整个流水线。

**Q：Model Builder 支持哪些模型格式？**

输入：PyTorch（`.pt` / `.pth`）和 ONNX（`.onnx`）。输出：QNN Context Binary（`.bin`）、QNN 模型库（`.dll`）、SNPE DLC（`.dlc`）。精度：FP16、FP32、W8A16、W8A8、W4A16、W4A8（及 INT8）。

**Q：Model Builder 能转换 LLM（Llama / Qwen / Gemma）吗？**

暂不支持——它面向 ~2 GB 以下的中小型模型。如需在 NPU 上运行 LLM，请从下载中心下载已预转换的量化模型，并通过 GenieAPIService 运行。

**Q：如何在不写代码的情况下把自己的模型加入 App Builder？**

用 Model Builder 转换后，使用 **导入到 App Builder（Promote）**——它扫描输出、识别每种精度、让你勾选变体与默认精度，并生成完整 Pack（含 dry-run + 冒烟测试）暴露到 App Builder。

**Q：为什么同一模型的第二次推理这么快？**

**Sticky Worker** 让推理子进程在同一模型的多次 Run 之间保持存活，复用已加载的 `QnnContext`——后续 Run 端到端典型 **< 1 秒**。空闲 10 分钟自动释放 NPU；崩溃自动透明退化。

**Q：App Builder 在云端还是本机推理？**

**始终在本机。** 每个 Pack 都通过 QAI AppBuilder + QNN HTP 在本地 NPU 运行。输入永不离开本机；云端 LLM（如使用）只能看到你主动发送的结果摘要文本。

---

## 📄 许可证

QAI ModelBuilder 采用 BSD 3-Clause "New" or "Revised" License 授权。详情请查阅 [LICENSE](LICENSE) 文件。

---

*构建于 Clean Architecture / DDD 内核之上 —— FastAPI 后端 + Vue 3（Vite）前端。*
