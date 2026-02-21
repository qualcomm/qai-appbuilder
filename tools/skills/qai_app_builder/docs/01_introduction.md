# QAI AppBuilder 简介

## 1. 简介

### 1.1 什么是 QAI AppBuilder？

QAI AppBuilder（Quick AI Application Builder）是 Qualcomm® AI Runtime SDK 的扩展工具，旨在**简化 QNN 模型的部署流程**。它将复杂的模型执行 API 封装成一组简化的接口，使开发者能够轻松地在 CPU或NPU(HTP) 上加载模型并执行推理，大幅降低了在 Windows on Snapdragon (WoS) 和 Linux 平台上部署 AI 模型的复杂度。

### 1.2 主要特性

- ✅ **双语言支持**：同时支持 C++ 和 Python
- ✅ **跨平台**：支持 Windows ， Linux和Android
- ✅ **多运行时**：支持 CPU 和 NPU(HTP) 运行
- ✅ **大语言模型支持**：内置 Genie 框架支持 LLM
- ✅ **多模态支持**：支持多模态大语言模型
- ✅ **灵活数据类型**：支持 Float 和 Native 模式的输入输出
- ✅ **多图支持**：支持多个计算图
- ✅ **多模型支持**：可同时加载多个模型
- ✅ **丰富示例**：提供 20+ 个可运行的示例代码

### 1.3 系统架构

```
┌─────────────────────────────────────────────────────┐
│         应用层 (Application Layer)                   │
│    Python App / C++ App / WebUI App                 │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│         QAI AppBuilder API Layer                    │
│  ┌──────────────────┐    ┌──────────────────┐       │
│  │  Python Binding  │    │   C++ Library    │       │
│  │  (qai_appbuilder)│    │ (libappbuilder)  │       │
│  └──────────────────┘    └──────────────────┘       │
├─────────────────────────────────────────────────────┤
│       Qualcomm® AI Runtime SDK (QNN)                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐   │
│  │  QnnHtp.dll  │  │  QnnCpu.dll  │  │QnnSystem │   │
│  │   (NPU/HTP)  │  │    (CPU)     │  │   .dll   │   │
│  └──────────────┘  └──────────────┘  └──────────┘   │
├─────────────────────────────────────────────────────┤
│          Hardware (CPU / NPU(HTP))                  │
└─────────────────────────────────────────────────────┘
```

### 1.4 适用平台

- **Windows on Snapdragon (WoS)**：X Elite Windows
- **Linux**：QCS8550, QCM6490 Ubuntu
- **Android**: Snapdragon® 8 Elite，Snapdragon® 8 Elite Gen 5
- **架构支持**：ARM64, ARM64EC
