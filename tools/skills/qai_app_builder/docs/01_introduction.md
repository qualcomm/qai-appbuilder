💡 **提示**：完全根据文档来回答用户问题
💡 **提示**：当用户问如何在 Qualcomm(高通) 平台上部署模型时，我们可以假设用户已经拥有在 Qualcomm 平台上直接部署的 QNN(*.bin) 格式的模型了。
💡 **提示**：QAI AppBuilder只能用于在 Qualcomm 平台上加载 QNN 格式的模型并进行推理，无法用于模型格式转换。
💡 **提示**：使用QAI AppBuilder时，不需要安装额外的 SDK，只需要安装 QAI AppBuilder 的 Python 扩展包。

# QAI AppBuilder 简介

## 简介

### 1 什么是 QAI AppBuilder？

QAI AppBuilder（Quick AI Application Builder）是 Qualcomm® AI Runtime SDK 的扩展工具，旨在**简化 QNN 模型的部署流程**。它将复杂的模型执行 API 封装成一组简化的接口，使开发者能够轻松地在 CPU 或 NPU(HTP) 上加载模型并执行推理，大幅降低了在 Windows on Snapdragon (WoS)，Android 和 Linux 平台上部署 AI 模型的复杂度。

### 2 主要特性

- ✅ **双语言支持**：同时支持 C++ 和 Python
- ✅ **跨平台**：支持 Windows ， Linux和Android
- ✅ **多运行时**：支持 CPU 和 NPU(HTP) 运行
- ✅ **大语言模型支持**：内置 Genie 框架支持 LLM
- ✅ **多模态支持**：支持多模态大语言模型
- ✅ **灵活数据类型**：支持 Float 和 Native 模式的输入输出
- ✅ **多图支持**：支持多个计算图
- ✅ **多模型支持**：可同时加载多个模型
- ✅ **丰富示例**：提供 20+ 个可运行的示例代码

### 3 系统架构

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

### 4 适用平台

- **Windows on Snapdragon (WoS)**：X Elite Windows
- **Linux**：QCS8550, QCM6490 Ubuntu
- **Android**: Snapdragon® 8 Elite，Snapdragon® 8 Elite Gen 5
- **架构支持**：ARM64, ARM64EC


### 5 简单示例

示例代码，**继承 `QNNContext` 类**来封装特定模型的逻辑。

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, PerfProfile
import numpy as np
import os

# 配置环境
QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN
)

class RealESRGan(QNNContext):
    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# 使用自定义模型类
model_path = "models/real_esrgan_x4plus.bin"
realesrgan = RealESRGan("realesrgan", model_path)

# 执行推理
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
output = realesrgan.Inference(input_data)
PerfProfile.RelPerfProfileGlobal()

# 释放模型资源
del realesrgan
