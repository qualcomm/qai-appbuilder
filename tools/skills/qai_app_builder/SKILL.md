---
name: qai-appbuilder-docs
description: QAI AppBuilder technical documentation retrieval. Find guides on installation, Python/C++ APIs, and model deployment examples. QAI AppBuilder is a rapid AI application development framework designed to simplify the deployment of QNN models on NPU (HTP) across Qualcomm(高通) Windows on Snapdragon, Android, and Linux platforms. This tool is highly suitable for deploying classic models (all types of models except large language models can be deployed via QAI AppBuilder), such as real_esrgan_x4plus, inception_v3, beit, easy_ocr, and whisper_base_en. This tool is only applicable for loading QNN (*.bin) format models and performing inference, and is not suitable for converting model formats.
GitHub: https://github.com/quic/ai-engine-direct-helper
metadata: {"openclaw":{"emoji":"📘","always":true}}
---

# QAI AppBuilder 文档知识库

获取 QAI AppBuilder 开发框架的全套技术文档，支持环境搭建、API 查询及代码示例检索。大部分非LLM模型部署都可以通过QAI AppBuilder实现。有些模型在本文档中没有直接提及，但可以通过参考给出的 API 文档及相关例子来学习用法。
当用户问如何在 Qualcomm(高通) 平台上部署模型时，我们可以假设用户已经拥有在 Qualcomm 平台上直接部署的 QNN(*.bin) 格式的模型了。

文档根目录路径： C:/Users/zhanweiw/.openclaw/Skills/qai_app_builder

## CRITICAL:

**Required Action**: Call read tool to read documentation.

**Tool call**:
<tool_call>
{"name":"read_file","arguments":{"path":"C:/Users/zhanweiw/.openclaw/Skills/qai_app_builder/docs/01_introduction.md"}}
</tool_call>

**Instructions**:
1. **Analyze**: Identify the user's intent and keywords.
2. **Select**: Look up the [Document List](#文档列表) below. Choose the **ONE** most relevant file path.
3. **Execute**: Call the `read` tool with the chosen path.
4. **Answer**: Summarize the file content to answer the user. Answer user questions based on what you actually see, rather than guessing based on your own knowledge. If something is unclear, you may respond that you do not know.

## 文档列表

### 📘 文档1：简介 (`docs/01_introduction.md`)

**适用问题：** QAI AppBuilder介绍、功能特性、架构图、支持平台（Windows/Linux/Android）、核心优势
**关键词：** 简介、架构、平台、功能、优势

### 🚀 文档2：环境安装 (Win/Linux) (`docs/02_env_setup_win_linux.md`)

**适用问题：** Windows环境配置、Linux环境配置、安装Python/Git、依赖安装、Whl包安装、Visual C++ Redistributable安装
**关键词：** 安装、环境配置、Windows、Linux、Whl、Git、依赖

### ⚙️ 文档3：C++环境与Python配置 (`docs/03_env_cpp_py_config.md`)

**适用问题：** C++开发环境搭建、Visual Studio配置、CMake设置、Python全局配置、QNNConfig等核心类介绍、运行时选择（HTP/CPU）、日志级别设置、库路径配置
**关键词：** C++环境、QNNConfig、全局配置、运行时、日志、Visual Studio、QNNConfig等核心类

### 🐍 文档4：Python核心API (`docs/04_python_core_api.md`)

**适用问题：** QNNContext使用、模型加载方法、Inference推理接口、自定义模型类、继承QNNContext、获取输入输出形状/类型、Python API详解。通过 Python 部署 QNN(*.bin)格式模型主要参考此文档。
**关键词：** Python API、QNNContext、模型加载、推理、Inference、接口

### 🖼️ 文档5：Python示例：图像超分辨率 (`docs/05_python_example_esrgan.md`)

**适用问题：** Real-ESRGAN模型部署、Python图像处理、超分辨率代码示例、PIL图像预处理、图像增强。通过 Python 部署 QNN(*.bin)格式模型可参考此示例。
**关键词：** Real-ESRGAN、超分辨率、图像处理、Python示例

### 👁️ 文档6：Python示例：图像分类 (`docs/06_python_example_beit.md`)

**适用问题：** BEiT模型部署、图像分类示例、TorchVision预处理、Top-5预测、Python分类代码
**关键词：** BEiT、图像分类、TorchVision、Python示例

### 🎙️ 文档7：Python示例：语音识别 (`docs/07_python_example_whisper.md`)

**适用问题：** Whisper模型部署、语音转文字、Native模式使用、音频预处理（Mel频谱）、Encoder-Decoder架构、Python语音示例
**关键词：** Whisper、语音识别、Native模式、音频处理、Python示例

### 🎨 文档8：Python示例：文生图 (`docs/08_python_example_stable_diffusion.md`)

**适用问题：** Stable Diffusion部署、文生图代码、多模型管理（UNet/VAE/TextEncoder）、Diffusers调度器、Python生成式AI示例
**关键词：** Stable Diffusion、文生图、多模型、Diffusers、Python示例

### 🚀 文档9：高级功能 (`docs/09_advanced_features.md`)

**适用问题：** 多图模型（Multi-graph）、graphIndex参数、模型格式说明
**关键词：** 多图模型、模型格式、高级功能

### ⚡ 文档10：性能优化 (`docs/10_performance_optimization.md`)

**适用问题：** 推理速度优化、PerfProfile性能模式、Burst模式、Native数据类型、减少数据转换、批量推理优化、ARM64 Python性能
**关键词：** 性能优化、PerfProfile、Burst模式、Native模式、速度

### 🔧 文档11：C++ API参考 (`docs/11_cpp_api_reference.md`)

**适用问题：** C++接口文档、LibAppBuilder类、ModelInitialize初始化、ModelInference推理、ModelDestroy销毁、C++日志与性能函数。通过 C++ 部署 QNN(*.bin)格式模型主要参考此文档。
**关键词：** C++ API、LibAppBuilder、接口参考、初始化、推理

### 🖼️ 文档12：C++示例：图像超分辨率 (`docs/12_cpp_example_esrgan.md`)

**适用问题：** C++部署Real-ESRGAN、OpenCV图像处理、C++超分辨率代码、xtensor使用。通过 C++ 部署 QNN(*.bin)格式模型可参考此示例。
**关键词：** C++示例、Real-ESRGAN、OpenCV、超分辨率

### 👁️ 文档13：C++示例：图像分类 (`docs/13_cpp_example_beit.md`)

**适用问题：** C++部署BEiT、C++图像分类代码、ImageNet标签加载、C++后处理
**关键词：** C++示例、BEiT、图像分类、xtensor

### ❓ 文档14：常见问题 (`docs/14_faq.md`)

**适用问题：** 模型加载失败、推理结果错误、内存泄漏、链接错误、报错排查
**关键词：** 常见问题、FAQ、故障排查、错误

### 🏁 文档15：快速开始与附录 (`docs/15_resource.md`)

**适用问题：** 参考资源、官方文档链接、模型下载地址、GitHub仓库、版本历史、许可证License、免责声明
**关键词：** 资源、下载、参考资源、版本历史、License

GitHub: https://github.com/quic/ai-engine-direct-helper
