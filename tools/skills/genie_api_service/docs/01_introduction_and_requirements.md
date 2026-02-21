# GenieAPIService 简介和系统要求

## 简介

GenieAPIService 是一个基于 C++ 开发的 OpenAI 兼容 API 服务，可以在 Windows on Snapdragon (WoS)、移动设备和 Linux 平台上运行。该服务允许开发者在本地设备的 NPU（神经处理单元）或 CPU 上运行大语言模型，无需依赖云端服务。

### 主要优势

- **本地运行**：所有推理在本地设备上完成，保护数据隐私
- **OpenAI 兼容**：使用标准的 OpenAI API 格式，易于集成
- **多平台支持**：支持 Windows、Android 和 Linux 平台
- **高性能**：利用 Qualcomm® AI Runtime SDK 实现 NPU 加速

---

## 系统要求

### Windows 平台

- **操作系统**：Windows 11 或更高版本
- **处理器**：支持 Qualcomm Snapdragon 的设备
- **内存**：至少 16GB RAM（推荐 32GB 或更多）
- **存储空间**：至少 10GB 可用空间（用于模型文件）
- **软件依赖**：
    - Qualcomm® AI Runtime SDK (QAIRT) 2.42.0 或更高版本(软件包自带，不需额外安装)
    - [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-160)

### Android 平台

- **操作系统**：Android 10 或更高版本
- **处理器**：Qualcomm Snapdragon 芯片（支持 NPU）
- **内存**：至少 8GB RAM（推荐 16GB 或更多）
- **存储空间**：至少 10GB 可用空间
- **权限**：需要存储访问权限和后台运行权限

### Linux 平台

- **操作系统**：Ubuntu 20.04 或更高版本
- **处理器**：ARM64
- **内存**：至少 16GB RAM
- **存储空间**：至少 10GB 可用空间

---

## 功能特性

GenieAPIService 提供了丰富的功能特性：

### 核心功能

- ✅ **CPU & NPU 支持**：支持在 CPU 和 NPU 上运行 LLM
- ✅ **流式和非流式模式**：支持流式输出和完整响应两种模式
- ✅ **模型切换**：支持在运行时切换不同的模型
- ✅ **多模态支持**：支持视觉语言模型（VLM）
- ✅ **自定义模型**：支持用户自定义模型配置
- ✅ **文本分割**：内置文本分割功能，处理长文本输入
- ✅ **工具调用**：支持 Function Calling 功能
- ✅ **思考模式**：支持启用/禁用思考模式
- ✅ **LoRA 支持**：支持 LoRA 适配器
- ✅ **历史记录**：支持对话历史记录功能

### 支持的模型格式

- **BIN 格式**：Qualcomm QNN 格式模型（默认）
- **MNN 格式**：阿里巴巴 MNN 框架模型（需编译时启用）
- **GGUF 格式**：llama.cpp 格式模型（需编译时启用）
