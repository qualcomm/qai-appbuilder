---
name: genie-api-service-docs
description: GenieAPIService technical documentation retrieval. Find guides on platform deployment, model configuration, and API usage. GenieAPIService is an OpenAI-compatible API service that enables running large language models(include LLM & VLM model) locally on Qualcomm(高通) Windows on Snapdragon, Android, and Linux platforms. It leverages the device local NPU(HTP) or CPU for efficient inference.
GitHub: https://github.com/quic/ai-engine-direct-helper
metadata: {"openclaw":{"emoji":"🧞","always":true}}
---

# GenieAPIService 文档知识库

获取 GenieAPIService 的全套技术文档，支持多平台部署、模型配置及客户端 API 集成。

文档根目录路径： C:/Users/zhanweiw/.openclaw/Skills/genie_api_service

## CRITICAL:

**Required Action**: Call read tool to read documentation.

**Tool call**:
<tool_call>
{"name":"read_file","arguments":{"path":"C:/Users/zhanweiw/.openclaw/Skills/genie_api_service/docs/01_introduction_and_requirements.md"}}
</tool_call>

**Instructions**:
1. **Analyze**: Identify the user's intent and keywords.
2. **Select**: Look up the [Document List](#文档列表) below. Choose the **ONE** most relevant file path.
3. **Execute**: Call the `read` tool with the chosen path.
4. **Answer**: Summarize the file content to answer the user. Answer user questions based on what you actually see, rather than guessing based on your own knowledge. If something is unclear, you may respond that you do not know or obtain the correct answer through an online search.

## 文档列表

### 📘 文档1：简介和系统要求 (`docs/01_introduction_and_requirements.md`)

**适用问题：** GenieAPIService介绍、功能特性、平台支持（Windows/Android/Linux）、系统要求、硬件配置、内存存储、模型格式（BIN/MNN/GGUF）、NPU加速、多模态支持
**关键词：** 简介、系统要求、硬件、平台、功能、模型格式

### 🚀 文档2：平台部署指南 (`docs/02_platform_deployment_guide.md`)

**适用问题：** Windows/Android安装部署、下载GenieAPIService、配置模型文件、启动服务、验证服务、Android后台运行、客户端应用安装（GenieChat/GenieFletUI）
**关键词：** 安装、部署、下载、启动、配置、APK

### 🧠 文档3：模型配置详解 (`docs/03_model_configuration_guide.md`)

**适用问题：** config.json配置、模型配置文件结构、文本模型部署、多模态模型（Qwen2.5-VL-3B）、prompt.json配置、模型目录结构、视觉语言模型、tokenizer/sampler参数
**关键词：** 模型配置、config.json、prompt.json、多模态、VLM、提示词

### 🛠️ 文档4：服务使用指南 (`docs/04_service_usage_guide.md`)

**适用问题：** 启动GenieAPIService、服务参数、C++客户端、Python客户端、端口设置、日志级别、思考模式、LoRA适配器、服务状态检查、GenieAPIClient使用
**关键词：** 启动服务、参数、C++客户端、Python客户端、LoRA

### 🌐 文档4B：其他语言客户端 (`docs/04B_other_language_clients.md`)

**适用问题：** JavaScript/Node.js、Java、Go、C#、Ruby、PHP、Rust调用方式、其他编程语言集成
**关键词：** JavaScript、Java、Go、C#、Ruby、PHP、Rust、多语言

### 🔨 文档5：从源码构建 (`docs/05_build_from_source.md`)

**适用问题：** 源码编译、Windows/Android构建、构建工具（CMake/NDK）、QAIRT SDK路径、MNN/GGUF格式支持、Android APK构建、构建依赖
**关键词：** 源码构建、编译、CMake、NDK、构建工具、QAIRT SDK

### 📡 文档6：API接口说明和工具 (`docs/06_api_reference_and_tools.md`)

**适用问题：** 聊天接口（/v1/chat/completions）、模型列表、文本分割、停止输出、历史记录（清除/重载/获取）、上下文大小、性能信息、停止服务、encode/decode/wav工具、base64编码解码、音频录制、API参数、流式响应
**关键词：** API接口、聊天、历史记录、工具、encode、decode、wav、参数

### 📄 文档7：进阶示例 (`docs/07_advanced_examples.md`)

**适用问题：** 基本对话实现、工具调用（Function Calling）、视觉语言模型、完整代码示例、Python流式对话、外部工具定义调用、图像处理（base64编码）、JavaScript/C#调用
**关键词：** 示例、代码、工具调用、Function Calling、视觉模型、图像

### ❓ 文档8：常见问题和技术支持 (`docs/08_faq_and_support.md`)

**适用问题：** 服务启动失败、模型加载失败、NPU不可用、推理速度慢、流式输出问题、多模态图像识别、Android服务停止、工具调用失效、历史记录丢失、端口占用、文本乱码、Bug报告、技术支持
**关键词：** 问题、错误、故障、解决方案、troubleshooting、Bug、技术支持

GitHub: https://github.com/quic/ai-engine-direct-helper
