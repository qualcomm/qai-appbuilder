# QAI AppBuilder 使用指南

<div align="center">
  <h2>基于 Qualcomm® AI Runtime SDK 的快速 AI 应用开发框架</h2>
  <p><i>简单 | 易用 | 高效 | 可靠</i></p>
</div>

---

## 📑 目录

1. [简介](#1-简介)
   
   - 1.1 [什么是 QAI AppBuilder？](#11-什么是-qai-appbuilder)
   - 1.2 [主要特性](#12-主要特性)
   - 1.3 [系统架构](#13-系统架构)
   - 1.4 [适用平台](#14-适用平台)

2. [环境准备](#2-环境准备)
   
   - 2.1 [Windows 环境配置](#21-windows-环境配置)
   - 2.2 [Linux 环境配置](#22-linux-环境配置)
   - 2.3 [C++ 环境配置](#23-c-环境配置)

3. [Python API 详解](#3-python-api-详解)
   
   - 3.1 [核心类概览](#31-核心类概览)
   - 3.2 [QNNConfig - 全局配置（必需）](#32-qnnconfig---全局配置必需)
   - 3.3 [QNNContext - 标准模型上下文（核心类）](#33-qnncontext---标准模型上下文核心类)
   - 3.4 [继承 QNNContext 的最佳实践](#34-继承-qnncontext-的最佳实践)
   - 3.5 [完整示例：图像超分辨率（Real-ESRGAN）](#35-完整示例图像超分辨率real-esrgan)
   - 3.6 [完整示例：图像分类（BEiT）](#36-完整示例图像分类beit)
   - 3.7 [完整示例：语音识别（Whisper）- Native 模式](#37-完整示例语音识别whisper---native-模式)
   - 3.8 [完整示例：Stable Diffusion 文生图](#38-完整示例stable-diffusion-文生图)
   - 3.9 [PerfProfile - 性能模式管理](#39-perfprofile---性能模式管理)
   - 3.10 [Native 模式详解（高性能）](#310-native-模式详解高性能)

4. [C++ API 详解](#4-c-api-详解)
   
   - 4.1 [LibAppBuilder 类](#41-libappbuilder-类)
   - 4.2 [日志和性能函数](#42-日志和性能函数)
   - 4.3 [完整 C++ 示例](#43-完整-c-示例)

5. [高级功能](#5-高级功能)
   
   - 5.1 [LoRA 适配器支持](#51-lora-适配器支持)
   - 5.2 [多图模型支持](#52-多图模型支持)
   - 5.3 [支持的模型格式](#53-支持的模型格式)

6. [性能优化](#6-性能优化)
   
   - 6.1 [使用 Native 模式（推荐）](#61-使用-native-模式推荐)
   - 6.2 [使用 Burst 性能模式](#62-使用-burst-性能模式)
   - 6.3 [批量推理优化](#63-批量推理优化)
   - 6.4 [使用 ARM64 Python（Windows）](#64-使用-arm64-pythonwindows)

7. [常见问题](#7-常见问题)
   
   - 7.1 [模型加载失败](#71-模型加载失败)
   - 7.2 [推理结果不正确](#72-推理结果不正确)
   - 7.3 [内存泄漏](#73-内存泄漏)
   - 7.4 [Native 模式数据类型不匹配](#74-native-模式数据类型不匹配)
   - 7.5 [C++ 链接错误](#75-c-链接错误)
   - 7.6 [性能不佳](#76-性能不佳)

8. [参考资源](#8-参考资源)
   
   - 8.1 [官方文档和资源](#81-官方文档和资源)
   - 8.2 [教程和博客](#82-教程和博客)
   - 8.3 [示例代码](#83-示例代码)
   - 8.4 [模型资源](#84-模型资源)

9. [快速开始指南](#9-快速开始指南)
   
   - 9.1 [第一个 Python 程序](#91-第一个-python-程序)
   - 9.2 [第一个 C++ 程序](#92-第一个-c-程序)

10. [版本历史](#10-版本历史)

11. [许可证](#11-许可证)

12. [免责声明](#12-免责声明)

13. [贡献和支持](#13-贡献和支持)

---

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
- ✅ **LoRA 支持**：支持 LoRA 适配器动态加载
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

---

## 2. 环境准备

### 2.1 Windows 环境配置

#### 步骤 1：安装依赖软件

**1. 安装 Git**

```bash
# 下载 Git for ARM64
https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe
```

**2. 安装 Python 3.12.8**

使用 **x64 版本** 可获得更好的 Python 扩展生态(当前相对来说，有更多的 Python 扩展可在x64 Python 环境中运行，而对于 ARM64 Python，有部分扩展需要自己编译)：

```bash
# 下载 Python 3.12.8 x64
https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
```

或使用 **ARM64 版本** 以获得更好的性能：

```bash
# 下载 Python 3.12.8 ARM64
https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe
```

⚠️ **重要提示**：

- 安装时必须勾选 "Add python.exe to PATH"
- 如果系统中有多个 Python 版本，确保新安装的版本在 PATH 环境变量的首位

验证 Python 版本顺序：

```cmd
where python
```

**3. 安装 Visual C++ Redistributable**

```bash
# 下载并安装
https://aka.ms/vs/17/release/vc_redist.x64.exe
https://aka.ms/vs/17/release/vc_redist.arm64.exe
```

#### 步骤 2：克隆 QAI AppBuilder 仓库

```bash
# 克隆仓库
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive

# 如果已克隆，更新代码
cd ai-engine-direct-helper
git pull --recurse-submodules
```

#### 步骤 3：安装 QAI AppBuilder Python 扩展

从 [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases) 下载对应版本的 `.whl` 文件：

```bash
# 对于 x64 Python
pip install qai_appbuilder-{version}-cp312-cp312-win_amd64.whl

# 对于 ARM64 Python
pip install qai_appbuilder-{version}-cp312-cp312-win_arm64.whl
```

💡 **重要提示**：从 v2.0.0 版本开始，QAI AppBuilder Python 扩展已经包含了所有必需的依赖库（包括 Qualcomm® AI Runtime SDK 运行时库），无需额外安装 Qualcomm® AI Runtime SDK。这大大简化了 Python 开发者的环境配置过程。

### 2.2 Linux 环境配置

#### 步骤 1：安装依赖软件

**1. 安装基础工具**

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y git python3 python3-pip build-essential

# 验证安装
python3 --version
pip3 --version
```

**2. 安装 Python 依赖**

```bash
# 安装常用的 Python 库
pip3 install numpy pillow opencv-python
```

#### 步骤 2：克隆 QAI AppBuilder 仓库

```bash
# 克隆仓库
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive

# 如果已克隆，更新代码
cd ai-engine-direct-helper
git pull --recurse-submodules
```

#### 步骤 3：安装 QAI AppBuilder Python 扩展

从 [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases) 下载对应版本的 `.whl` 文件：

```bash
# 对于 Linux ARM64
pip3 install qai_appbuilder-{version}-cp310-cp310-linux_aarch64.whl
```

💡 **重要提示**：从 v2.0.0 版本开始，QAI AppBuilder Python 扩展已经包含了所有必需的依赖库（包括 Qualcomm® AI Runtime SDK 运行时库），无需额外安装 Qualcomm® AI Runtime SDK。

#### 步骤 4：配置环境变量（可选）

对于某些 Linux 平台，可能需要设置 `ADSP_LIBRARY_PATH` 环境变量：

```bash
# 添加到 ~/.bashrc 或 ~/.zshrc
export ADSP_LIBRARY_PATH=/path/to/qnn/libs

# 使配置生效
source ~/.bashrc
```

#### Linux 与 Windows 的主要区别

| 项目      | Windows                          | Linux                                  |
| ------- | -------------------------------- | -------------------------------------- |
| 库文件扩展名  | `.dll`                           | `.so`                                  |
| 后端库     | `QnnHtp.dll`, `QnnCpu.dll`       | `libQnnHtp.so`, `libQnnCpu.so`         |
| 系统库     | `QnnSystem.dll`                  | `libQnnSystem.so`                      |
| Python  | `python.exe`                     | `python3`                              |
| 路径分隔符   | `\` (反斜杠)                        | `/` (正斜杠)                              |
| 环境变量设置  | 系统属性 → 环境变量                     | `~/.bashrc` 或 `~/.zshrc`               |
| 特殊环境变量  | 无                                | `ADSP_LIBRARY_PATH` (某些平台需要)           |

### 2.3 C++ 环境配置

#### 步骤 1：下载预编译库

从 [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases) 下载对应平台的预编译库：

**Windows ARM64**：
```
QAI_AppBuilder-win_arm64-{version}-Release.zip
```

**Linux ARM64**：
```
QAI_AppBuilder-linux_aarch64-{version}-Release.tar.gz
```

解压后包含：

- **Windows**：
  - `libappbuilder.dll` - 主库
  - `libappbuilder.lib` - 导入库
  - `LibAppBuilder.hpp` - 头文件
  - `Lora.hpp` - LoRA 支持头文件

- **Linux**：
  - `libappbuilder.so` - 主库
  - `LibAppBuilder.hpp` - 头文件
  - `Lora.hpp` - LoRA 支持头文件

#### 步骤 2：配置编译环境

##### Windows - Visual Studio 配置

**必需的项目配置**：

1. **包含目录**
   
   - 项目属性 → C/C++ → 常规 → 附加包含目录
   - 添加：`$(ProjectDir)include` 或头文件所在路径

2. **库目录**
   
   - 项目属性 → 链接器 → 常规 → 附加库目录
   - 添加：`$(ProjectDir)lib` 或 `.lib` 文件所在路径

3. **链接器输入**
   
   - 项目属性 → 链接器 → 输入 → 附加依赖项
   - 添加：`libappbuilder.lib`

4. **运行时库**（⚠️ 重要）
   
   - 项目属性 → C/C++ → 代码生成 → 运行时库
   - 设置为：**多线程 DLL (/MD)**

5. **C++ 标准**
   
   - 项目属性 → C/C++ → 语言 → C++ 语言标准
   - 设置为：**ISO C++17 标准 (/std:c++17)** 或更高

---

## 3. Python API 详解

### 3.1 核心类概览

| 类名               | 用途           | 推荐使用                  | 说明                        |
| ---------------- | ------------ | --------------------- | ------------------------- |
| `QNNContext`     | 标准模型上下文      | ✅ 推荐                  | 最常用的类，适用于大多数场景            |
| `QNNLoraContext` | LoRA 模型上下文   | ✅ 推荐                  | 支持动态加载 LoRA 适配器           |
| `QNNContextProc` | 进程隔离模型上下文    | ✅ 推荐                  | 用于多进程场景             |
| `QNNShareMemory` | 共享内存管理       | 与 QNNContextProc 配合使用 | 进程间高效数据传输                 |
| `QNNConfig`      | 全局配置管理       | ✅ 必需                  | 必须在使用其他 API 之前调用          |
| `LogLevel`       | 日志级别控制       | ✅ 推荐                  | ERROR, WARN, INFO, VERBOSE, DEBUG |
| `ProfilingLevel` | 性能分析级别       | 可选                    | OFF, BASIC, DETAILED      |
| `PerfProfile`    | 性能模式管理       | ✅ 推荐                  | DEFAULT, HIGH_PERFORMANCE, BURST |
| `Runtime`        | 运行时选择        | ✅ 必需                  | HTP 或 CPU          |
| `DataType`       | 数据类型模式       | ✅ 推荐                  | FLOAT 或 NATIVE           |
| `LoraAdapter`    | LoRA 适配器     | 与 QNNLoraContext 配合使用 | 定义 LoRA 适配器文件路径           |
| `GenieContext`   | 大语言模型专用上下文   | ✅ 推荐（LLM 场景）         | 专为 LLM 优化的上下文类   |

### 3.2 QNNConfig - 全局配置（必需）

`QNNConfig` 用于配置 QNN 运行环境，**必须在使用其他 API 之前调用**。

#### API 签名

```python
class QNNConfig:
    @staticmethod
    def Config(
        qnn_lib_path: str = "None",                    # QAIRT运行 库路径
        runtime: str = Runtime.HTP,                    # 运行时
        log_level: int = LogLevel.ERROR,               # 日志级别
        profiling_level: int = ProfilingLevel.OFF,     # 性能分析级别
        log_path: str = "None"                         # 日志文件路径
    ) -> None
```

#### 参数详解

| 参数                | 类型  | 默认值                | 说明                                                                        |
| ----------------- | --- | ------------------ | ------------------------------------------------------------------------- |
| `qnn_lib_path`    | str | "None"             | QNN 库文件目录路径（包含 QnnHtp.dll 等，**从QAI AppBuilder v2.0.0开始，不需要设置此参数，默认置空即可**） |
| `runtime`         | str | Runtime.HTP        | `Runtime.HTP` (NPU) 或 `Runtime.CPU`                                       |
| `log_level`       | int | LogLevel.ERROR     | ERROR(1), WARN(2), INFO(3), VERBOSE(4), DEBUG(5)                          |
| `profiling_level` | int | ProfilingLevel.OFF | OFF(0), BASIC(1), DETAILED(2)                                             |
| `log_path`        | str | "None"             | 日志文件路径，"None" 表示输出到控制台                                                    |

#### 使用示例

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel

QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)
```

### 3.3 QNNContext - 标准模型上下文（核心类）

`QNNContext` 是最常用的类，用于加载模型、执行推理和管理模型生命周期。

#### 构造函数

```python
class QNNContext:
    def __init__(
        self,
        model_name: str = "None",                      # 模型名称（唯一标识）
        model_path: str = "None",                      # 模型文件路径
        backend_lib_path: str = "None",                # 后端库路径（可选）
        system_lib_path: str = "None",                 # 系统库路径（可选）
        is_async: bool = False,                        # 是否异步执行
        input_data_type: str = DataType.FLOAT,         # 输入数据类型
        output_data_type: str = DataType.FLOAT         # 输出数据类型
    ) -> None
```

#### 参数详解

| 参数                 | 类型   | 默认值            | 说明                                                                   |
| ------------------ | ---- | -------------- | -------------------------------------------------------------------- |
| `model_name`       | str  | "None"         | 模型唯一标识符，用于区分不同模型                                                     |
| `model_path`       | str  | "None"         | 模型文件路径（支持 `.bin` 和 `.dlc` 格式）                                        |
| `backend_lib_path` | str  | "None"         | QnnHtp.dll 或 QnnCpu.dll 路径（可选，**从QAI AppBuilder v2.0.0开始，不需要设置此参数**） |
| `system_lib_path`  | str  | "None"         | QnnSystem.dll 路径（可选，**从QAI AppBuilder v2.0.0开始，不需要设置此参数**）           |
| `is_async`         | bool | False          | 是否启用异步推理                                                             |
| `input_data_type`  | str  | DataType.FLOAT | `DataType.FLOAT` 或 `DataType.NATIVE`                                 |
| `output_data_type` | str  | DataType.FLOAT | `DataType.FLOAT` 或 `DataType.NATIVE`                                 |

💡 **提示**：从QAI AppBuilder **v2.0.0** 开始，不需要设置参数：`backend_lib_path` 和 `system_lib_path` 。

#### 核心方法

##### Inference - 执行推理

```python
def Inference(
    self,
    input: List[np.ndarray],                           # 输入数据列表
    perf_profile: str = PerfProfile.DEFAULT,           # 性能模式
    graphIndex: int = 0                                # 图索引
) -> List[np.ndarray]                                  # 返回输出列表
```

**参数说明**：

- `input`：输入数据列表，每个元素是一个 NumPy 数组
- `perf_profile`：性能模式(不推荐使用此参数。)
  - `PerfProfile.DEFAULT`：默认模式（不改变性能配置）
  - `PerfProfile.HIGH_PERFORMANCE`：高性能模式
  - `PerfProfile.BURST`：突发模式（最高性能）
- `graphIndex`：图索引（用于多图模型，默认为 0）

**返回值**：

- 输出数据列表，每个元素是一个 NumPy 数组

💡 **提示**：不推荐使用perf_profile参数，建议通过配对使用PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST) 、PerfProfile.RelPerfProfileGlobal()来实现设置 NPU 为高性能模式。

##### 模型信息查询方法

```python
# 获取输入形状（例如：[[1, 3, 224, 224]]）
def getInputShapes(self) -> List[List[int]]

# 获取输出形状（例如：[[1, 1000]]）
def getOutputShapes(self) -> List[List[int]]

# 获取输入数据类型（例如：['float32'] 或 ['float16']）
def getInputDataType(self) -> List[str]

# 获取输出数据类型（例如：['float32'] 或 ['float16']）
def getOutputDataType(self) -> List[str]

# 获取图名称
def getGraphName(self) -> str

# 获取输入张量名称（例如：['input']）
def getInputName(self) -> List[str]

# 获取输出张量名称（例如：['output']）
def getOutputName(self) -> List[str]
```

### 3.4 继承 QNNContext 的最佳实践

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

# ============================================
# 方式 1：简单继承（最常用）
# ============================================
class RealESRGan(QNNContext):
    """Real-ESRGAN 图像超分辨率模型"""

    def Inference(self, input_data):
        """重写 Inference 方法以简化调用"""
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

del realesrgan

# ============================================
# 方式 2：多输入模型
# ============================================
class Unet(QNNContext):
    """UNet 去噪模型 - 多输入"""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        """接受多个输入"""
        # 重塑为一维数组
        input_data_1 = input_data_1.reshape(input_data_1.size)
        # input_data_2 已经是一维，不需要重塑
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        # 重塑输出
        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

# 使用
unet = Unet("unet", "models/unet.bin")
output = unet.Inference(latent, timestep, text_embedding)
del unet

# ============================================
# 方式 3：多输出模型
# ============================================
class Encoder(QNNContext):
    """Whisper Encoder - 多输出"""

    def Inference(self, input_data):
        """返回多个输出"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)

        # 重塑每个输出
        k_cache_cross = output_data[0].reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1].reshape(6, 8, 1500, 64)

        return k_cache_cross, v_cache_cross

# 使用
encoder = Encoder("whisper_encoder", "models/encoder.bin")
k_cache, v_cache = encoder.Inference(mel_input)
del encoder
```

### 3.5 完整示例：图像超分辨率（Real-ESRGAN）

参考代码：`samples/python/real_esrgan_x4plus/real_esrgan_x4plus.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile
)
import numpy as np
from PIL import Image
from pathlib import Path
import os

# ============================================
# 1. 配置 QNN 环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

# 处理不同的工作目录情况
if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "real_esrgan_x4plus"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # 此参数从 v2.0.0 开始可以不进行设置，留空即可。
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. 定义模型类（继承自 QNNContext）
# ============================================
class RealESRGan(QNNContext):
    """Real-ESRGAN 图像超分辨率模型"""

    def Inference(self, input_data):
        """重写 Inference 方法以简化调用"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. 初始化模型
# ============================================
IMAGE_SIZE = 512
model_path = model_dir / f"{MODEL_NAME}.bin"

# 创建模型实例
realesrgan = RealESRGan("realesrgan", str(model_path))

# 查询模型信息（可选）
print(f"模型名称: {realesrgan.getGraphName()}")
print(f"输入形状: {realesrgan.getInputShapes()}")
print(f"输出形状: {realesrgan.getOutputShapes()}")
print(f"输入数据类型: {realesrgan.getInputDataType()}")
print(f"输出数据类型: {realesrgan.getOutputDataType()}")

# ============================================
# 4. 图像预处理辅助函数
# ============================================
def pil_resize_pad(image, target_size):
    """调整图像大小并填充到目标尺寸"""
    orig_width, orig_height = image.size
    target_width, target_height = target_size

    # 计算缩放比例
    scale = min(target_width / orig_width, target_height / orig_height)
    new_width = int(orig_width * scale)
    new_height = int(orig_height * scale)

    # 调整大小
    image = image.resize((new_width, new_height), Image.LANCZOS)

    # 创建新图像并填充
    new_image = Image.new('RGB', target_size, (0, 0, 0))
    paste_x = (target_width - new_width) // 2
    paste_y = (target_height - new_height) // 2
    new_image.paste(image, (paste_x, paste_y))

    padding = (paste_x, paste_y)
    return new_image, scale, padding

def pil_undo_resize_pad(image, original_size, scale, padding):
    """移除填充并恢复到原始尺寸"""
    # 裁剪填充
    width, height = image.size
    left = padding[0] * 4
    top = padding[1] * 4
    right = width - padding[0] * 4
    bottom = height - padding[1] * 4
    image = image.crop((left, top, right, bottom))

    # 调整到原始尺寸
    image = image.resize(original_size, Image.LANCZOS)
    return image

# ============================================
# 5. 执行推理
# ============================================
input_image_path = execution_ws / "input.jpg"
output_image_path = execution_ws / "output.png"

# 读取和预处理图像
orig_image = Image.open(input_image_path)
image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

image = np.array(image)
image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)  # 归一化

# 设置 HTP 为突发模式以获得最佳性能
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 执行推理
output_image = realesrgan.Inference(image)

# 重置 HTP 性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 6. 后处理
# ============================================
# 重塑输出形状
output_image = output_image.reshape(IMAGE_SIZE * 4, IMAGE_SIZE * 4, 3)

# 反归一化
output_image = np.clip(output_image, 0.0, 1.0)
output_image = (output_image * 255).astype(np.uint8)

# 转换为 PIL 图像
output_image = Image.fromarray(output_image)

# 移除填充并恢复原始尺寸
image_size = (orig_image.size[0] * 4, orig_image.size[1] * 4)
image_padding = (padding[0] * 4, padding[1] * 4)
output_image = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

# 保存结果
output_image.save(output_image_path)
print(f"超分辨率图像已保存到: {output_image_path}")

# ============================================
# 7. 清理资源
# ============================================
del realesrgan
```

### 3.6 完整示例：图像分类（BEiT）

参考代码：`samples/python/beit/beit.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile
)
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from pathlib import Path
import os

# ============================================
# 1. 配置环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "beit"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # 此参数从 v2.0.0 开始可以不进行设置，留空即可。
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. 定义 BEiT 模型类
# ============================================
class Beit(QNNContext):
    """BEiT 图像分类模型"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. 初始化模型
# ============================================
IMAGE_SIZE = 224
model_path = model_dir / f"{MODEL_NAME}.bin"

beit = Beit("beit", str(model_path))

# ============================================
# 4. 图像预处理
# ============================================
def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """预处理 PIL 图像"""
    preprocess = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
    ])

    img = preprocess(image)
    img = img.unsqueeze(0)
    return img

# ============================================
# 5. 执行推理
# ============================================
input_image_path = execution_ws / "input.jpg"

# 读取和预处理图像
image = Image.open(input_image_path)
image = preprocess_PIL_image(image).numpy()
image = np.transpose(image, (0, 2, 3, 1))  # NCHW -> NHWC

# 设置突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 执行推理
output_data = beit.Inference(image)

# 重置性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 6. 后处理
# ============================================
# 转换为 torch tensor 并应用 softmax
output = torch.from_numpy(output_data).squeeze(0)
probabilities = torch.softmax(output, dim=0)

# 获取 Top-5 预测
top5_prob, top5_catid = torch.topk(probabilities, 5)

print("\nTop 5 预测结果:")
for i in range(5):
    print(f"{i+1}. 类别 {top5_catid[i]}: {top5_prob[i].item():.6f}")

# ============================================
# 7. 清理资源
# ============================================
del beit
```

### 3.7 完整示例：语音识别（Whisper）- Native 模式

参考代码：`samples/python/whisper_base_en/whisper_base_en.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile, DataType
)
import numpy as np
import torch
import audio2numpy as a2n
import samplerate
import whisper
from pathlib import Path
import os

# ============================================
# 1. 配置环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "whisper_base_en"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # 此参数从 v2.0.0 开始可以不进行设置，留空即可。
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. 定义 Encoder 和 Decoder 类（使用 Native 模式）
# ============================================
class Encoder(QNNContext):
    """Whisper Encoder - 使用 Native 模式"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)

        # 重塑输出
        k_cache_cross = output_data[0].reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1].reshape(6, 8, 1500, 64)

        return k_cache_cross, v_cache_cross

class Decoder(QNNContext):
    """Whisper Decoder - 使用 Native 模式"""

    def Inference(self, x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self):
        input_datas = [x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self]
        output_data = super().Inference(input_datas)

        # 重塑输出
        logits = output_data[0].reshape(1, 1, 51864)
        k_cache = output_data[1].reshape(6, 8, 64, 224)
        v_cache = output_data[2].reshape(6, 8, 224, 64)

        return logits, k_cache, v_cache

# ============================================
# 3. 初始化模型（Native 模式）
# ============================================
encoder_model_path = model_dir / "whisper_base_en-whisperencoder.bin"
decoder_model_path = model_dir / "whisper_base_en-whisperdecoder.bin"

# 使用 Native 模式初始化
encoder = Encoder(
    "whisper_encoder",
    str(encoder_model_path),
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

decoder = Decoder(
    "whisper_decoder",
    str(decoder_model_path),
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 查看模型的原生数据类型
print("\nEncoder 模型信息:")
print(f"  输入数据类型: {encoder.getInputDataType()}")
print(f"  输出数据类型: {encoder.getOutputDataType()}")

print("\nDecoder 模型信息:")
print(f"  输入数据类型: {decoder.getInputDataType()}")
print(f"  输出数据类型: {decoder.getOutputDataType()}")

# ============================================
# 4. Whisper 常量定义
# ============================================
TOKEN_SOT = 50257  # Start of transcript
TOKEN_EOT = 50256  # End of transcript
SAMPLE_RATE = 16000
CHUNK_LENGTH = 30  # seconds
MAX_AUDIO_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE

# ============================================
# 5. 音频预处理函数
# ============================================
def log_mel_spectrogram(audio_np: np.ndarray) -> np.ndarray:
    """计算 Mel 频谱图（返回 float16）"""
    audio = torch.from_numpy(audio_np)

    # 填充音频到固定长度
    padding = MAX_AUDIO_SAMPLES - len(audio)
    if padding > 0:
        audio = torch.nn.functional.pad(audio, (0, padding))

    # 计算 STFT
    n_fft = 400
    hop_length = 160
    window = torch.hann_window(n_fft)
    stft = torch.stft(audio, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    # 应用 Mel 滤波器（需要预先加载）
    # mel_filter = np.load("mel_filters.npz")["mel_80"]
    # mel_spec = torch.from_numpy(mel_filter) @ magnitudes

    # 计算 log mel spectrogram
    log_spec = torch.clamp(magnitudes, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0

    # 返回 float16（Native 模式）
    return log_spec.unsqueeze(0).to(dtype=torch.float16).cpu().numpy()

# ============================================
# 6. 执行推理
# ============================================
audio_path = execution_ws / "jfk.wav"

# 读取音频文件
audio, audio_sample_rate = a2n.audio_from_file(str(audio_path))

# 重采样到 16kHz
if audio_sample_rate != SAMPLE_RATE:
    audio = samplerate.resample(audio, SAMPLE_RATE / audio_sample_rate)

# 计算 Mel 频谱图（返回 float16）
mel_input = log_mel_spectrogram(audio)
print(f"mel_input: dtype={mel_input.dtype}, shape={mel_input.shape}")

# 设置突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Encoder 推理
print("执行 Encoder 推理...")
k_cache_cross, v_cache_cross = encoder.Inference(mel_input)

print(f"k_cache_cross: shape={k_cache_cross.shape}, dtype={k_cache_cross.dtype}")
print(f"v_cache_cross: shape={v_cache_cross.shape}, dtype={v_cache_cross.dtype}")

# ============================================
# 7. Decoder 推理（自回归生成）
# ============================================
# 初始化 Decoder 输入
x = np.array([[TOKEN_SOT]], dtype=np.int32)
index = np.array([[0]], dtype=np.int32)
k_cache_self = np.zeros((6, 8, 64, 224), dtype=np.float16)
v_cache_self = np.zeros((6, 8, 224, 64), dtype=np.float16)

decoded_tokens = [TOKEN_SOT]
max_tokens = 100

print("\n执行 Decoder 推理（自回归生成）...")
for i in range(max_tokens):
    index = np.array([[i]], dtype=np.int32)

    # Decoder 推理
    logits, k_cache_self, v_cache_self = decoder.Inference(
        x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
    )

    # 获取下一个 token
    next_token = np.argmax(logits[0, -1])
    decoded_tokens.append(int(next_token))

    # 检查是否结束
    if next_token == TOKEN_EOT:
        break

    # 更新输入
    x = np.array([[next_token]], dtype=np.int32)

    if (i + 1) % 10 == 0:
        print(f"  已生成 {i + 1} 个 tokens...")

# 重置性能模式
PerfProfile.RelPerfProfileGlobal()

print(f"\n生成完成，共 {len(decoded_tokens)} 个 tokens")

# ============================================
# 8. 解码 tokens 为文本
# ============================================
tokenizer = whisper.decoding.get_tokenizer(
    multilingual=False, language="en", task="transcribe"
)
text = tokenizer.decode(decoded_tokens[1:])  # 移除 TOKEN_SOT
print(f"转录结果: {text.strip()}")

# ============================================
# 9. 清理资源
# ============================================
del encoder
del decoder
```

### 3.8 完整示例：Stable Diffusion 文生图

参考代码：`samples/python/stable_diffusion_v1_5/stable_diffusion_v1_5.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile
)
import numpy as np
from PIL import Image
from pathlib import Path
import torch
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler
import os

# ============================================
# 1. 配置环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "stable_diffusion_v1_5"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # 此参数从 v2.0.0 开始可以不进行设置，留空即可。
    runtime=Runtime.HTP,
    log_level=LogLevel.ERROR,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. 定义模型类
# ============================================
class TextEncoder(QNNContext):
    """文本编码器"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        # 输出形状应该是 (1, 77, 768)
        output_data = output_data.reshape((1, 77, 768))
        return output_data

class Unet(QNNContext):
    """UNet 去噪模型"""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        # 重塑为一维数组
        input_data_1 = input_data_1.reshape(input_data_1.size)
        # input_data_2 已经是一维，不需要重塑
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        # 重塑输出为 (1, 64, 64, 4)
        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

class VaeDecoder(QNNContext):
    """VAE 解码器"""

    def Inference(self, input_data):
        input_data = input_data.reshape(input_data.size)
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. 初始化所有模型
# ============================================
text_encoder = TextEncoder(
    "text_encoder",
    str(model_dir / "text_encoder.bin")
)

unet = Unet(
    "model_unet",
    str(model_dir / "unet.bin")
)

vae_decoder = VaeDecoder(
    "vae_decoder",
    str(model_dir / "vae_decoder.bin")
)

# ============================================
# 4. 初始化 Tokenizer 和 Scheduler
# ============================================
# 初始化 CLIP Tokenizer
tokenizer_dir = model_dir / "tokenizer"
tokenizer = CLIPTokenizer.from_pretrained(
    "openai/clip-vit-large-patch14",
    cache_dir=str(tokenizer_dir)
)

# 初始化 Scheduler
scheduler = DPMSolverMultistepScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    beta_schedule="scaled_linear"
)

# ============================================
# 5. 设置生成参数
# ============================================
user_prompt = "spectacular view of northern lights from Alaska"
uncond_prompt = "lowres, text, error, cropped, worst quality"
user_seed = np.int64(42)
user_step = 20
user_text_guidance = 7.5

# ============================================
# 6. Tokenize 提示词
# ============================================
def run_tokenizer(prompt, max_length=77):
    """Tokenize 文本"""
    text_input = tokenizer(
        prompt,
        padding="max_length",
        max_length=max_length,
        truncation=True
    )
    text_input = np.array(text_input.input_ids, dtype=np.float32)
    return text_input

cond_tokens = run_tokenizer(user_prompt)
uncond_tokens = run_tokenizer(uncond_prompt)

# ============================================
# 7. 执行完整的生成流程
# ============================================
# 设置突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 设置 scheduler 时间步
scheduler.set_timesteps(user_step)

# 编码文本
print("编码文本提示...")
uncond_text_embedding = text_encoder.Inference(uncond_tokens)
user_text_embedding = text_encoder.Inference(cond_tokens)

# 初始化随机 latent
random_init_latent = torch.randn(
    (1, 4, 64, 64),
    generator=torch.manual_seed(user_seed)
).numpy()
latent_in = random_init_latent.transpose(0, 2, 3, 1)  # NCHW -> NHWC

# 去噪循环
print(f"开始去噪（{user_step} 步）...")
for step in range(user_step):
    print(f'  步骤 {step + 1}/{user_step}')

    # 获取当前时间步
    time_step = np.int32(scheduler.timesteps.numpy()[step])

    # UNet 推理（无条件）
    unconditional_noise_pred = unet.Inference(
        latent_in, time_step, uncond_text_embedding
    )

    # UNet 推理（有条件）
    conditional_noise_pred = unet.Inference(
        latent_in, time_step, user_text_embedding
    )

    # 合并噪声预测
    noise_pred_uncond = np.transpose(unconditional_noise_pred, (0, 3, 1, 2))
    noise_pred_text = np.transpose(conditional_noise_pred, (0, 3, 1, 2))
    latent_in_nchw = np.transpose(latent_in, (0, 3, 1, 2))

    noise_pred_uncond = torch.from_numpy(noise_pred_uncond)
    noise_pred_text = torch.from_numpy(noise_pred_text)
    latent_in_torch = torch.from_numpy(latent_in_nchw)

    # 应用 guidance
    noise_pred = noise_pred_uncond + user_text_guidance * (noise_pred_text - noise_pred_uncond)

    # Scheduler 步骤
    latent_out = scheduler.step(noise_pred, time_step, latent_in_torch).prev_sample.numpy()

    # 转换回 NHWC
    latent_in = np.transpose(latent_out, (0, 2, 3, 1))

# VAE 解码
print("解码为图像...")
output_image = vae_decoder.Inference(latent_in)

# 重置性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 8. 后处理
# ============================================
image_size = 512
output_image = np.clip(output_image * 255.0, 0.0, 255.0).astype(np.uint8)
output_image = output_image.reshape(image_size, image_size, 3)
output_image = Image.fromarray(output_image, mode="RGB")

# 保存图像
output_path = execution_ws / "generated_image.png"
output_image.save(output_path)
print(f"图像已保存到: {output_path}")

# ============================================
# 9. 清理资源
# ============================================
del text_encoder
del unet
del vae_decoder
```

### 3.9 PerfProfile - 性能模式管理

`PerfProfile` 用于控制 HTP (NPU) 的性能模式。

#### 性能模式对比

| 模式                 | 说明    | 功耗  | 性能  | 适用场景       |
| ------------------ | ----- | --- | --- | ---------- |
| `DEFAULT`          | 默认模式  | 低   | 中   | 不改变性能配置    |
| `HIGH_PERFORMANCE` | 高性能模式 | 中   | 高   | 持续高负载推理    |
| `BURST`            | 突发模式  | 高   | 最高  | 短时间内需要最高性能 |

#### 使用方法

```python
from qai_appbuilder import PerfProfile, QNNContext

model = QNNContext(...)

# ============================================
# 方法 1：全局设置（推荐用于批量推理）
# ============================================
# 设置为突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 执行多次推理（都使用突发模式）
for i in range(100):
    # 注意：perf_profile 参数设置为 DEFAULT 以使用全局配置
    output = model.Inference([input_data], perf_profile=PerfProfile.DEFAULT)

# 释放性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 方法 2：单次推理设置
# ============================================
# 每次推理时指定性能模式
output = model.Inference([input_data], perf_profile=PerfProfile.BURST)
```

⚠️ **重要提示**：

- 使用 `SetPerfProfileGlobal()` 后，`Inference()` 的 `perf_profile` 参数应设置为 `PerfProfile.DEFAULT`
- 如果在 `Inference()` 中指定其他性能模式，会覆盖全局设置

### 3.10 Native 模式详解（高性能）

Native 模式直接使用模型的原生数据类型，避免数据转换，**显著提升性能**。

#### 支持的数据类型

- `int8` / `uint8`
- `int16` / `uint16`
- `int32` / `uint32`
- `float16` (fp16) - 最常用
- `float32` (fp32)
- `bool`

#### Native 模式使用步骤

```python
from qai_appbuilder import QNNContext, DataType
import numpy as np

# 1. 创建模型时指定 native 模式
model = QNNContext(
    model_name="my_model",
    model_path="models/my_model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 2. 查询模型所需的数据类型
input_dtypes = model.getInputDataType()
output_dtypes = model.getOutputDataType()
print(f"Input data types: {input_dtypes}")   # 例如：['float16']
print(f"Output data types: {output_dtypes}") # 例如：['float16']

# 3. 创建数据类型映射
dtype_map = {
    'float16': np.float16,
    'fp16': np.float16,
    'float32': np.float32,
    'fp32': np.float32,
    'float': np.float32,
    'int8': np.int8,
    'uint8': np.uint8,
    'int16': np.int16,
    'uint16': np.uint16,
    'int32': np.int32,
    'uint32': np.uint32,
    'bool': np.bool_
}

# 4. 根据模型要求准备数据
input_dtype_str = input_dtypes[0].lower()
input_dtype = dtype_map.get(input_dtype_str, np.float32)

input_shapes = model.getInputShapes()
input_data = np.random.randn(*input_shapes[0]).astype(input_dtype)

# 5. 执行推理
outputs = model.Inference([input_data])

# 6. 输出也是原生类型
print(f"Output dtype: {outputs[0].dtype}")  # 例如：float16
```

---

## 4. C++ API 详解

### 4.1 LibAppBuilder 类

`LibAppBuilder` 是 C++ API 的核心类。

#### 头文件引用

```cpp
#include "LibAppBuilder.hpp"
```

#### 主要方法

##### ModelInitialize - 初始化模型

```cpp
bool ModelInitialize(
    const std::string& model_name,                     // 模型名称
    const std::string& model_path,                     // 模型文件路径
    const std::string& backend_lib_path,               // 后端库路径
    const std::string& system_lib_path,                // 系统库路径
    bool async = false,                                // 是否异步
    const std::string& input_data_type = "float",      // 输入数据类型
    const std::string& output_data_type = "float"      // 输出数据类型
);
```

**参数说明**：

- `model_name`：模型唯一标识符（如 "mobilenet_v2"）
- `model_path`：模型文件路径（支持 `.bin` 和 `.dlc`）
- `backend_lib_path`：QnnHtp.dll 或 QnnCpu.dll 的完整路径
- `system_lib_path`：QnnSystem.dll 的完整路径
- `async`：是否启用异步推理
- `input_data_type`：`"float"` 或 `"native"`
- `output_data_type`：`"float"` 或 `"native"`

**返回值**：

- `true`：初始化成功
- `false`：初始化失败

⚠️ **重要提示**：

- 调用 C++ API，在初始化时要确保正确准备 QAIRT SDK 运行时库，并通过 backend_lib_path 及 system_lib_path 参数正确设置相应库文件路径。

##### ModelInference - 执行推理

```cpp
bool ModelInference(
    std::string model_name,                            // 模型名称
    std::vector<uint8_t*>& inputBuffers,               // 输入缓冲区
    std::vector<uint8_t*>& outputBuffers,              // 输出缓冲区（函数分配）
    std::vector<size_t>& outputSize,                   // 输出大小
    std::string& perfProfile,                          // 性能模式
    size_t graphIndex = 0,                             // 图索引
);
```

**参数说明**：

- `model_name`：模型名称（与 ModelInitialize 中的一致）
- `inputBuffers`：输入数据缓冲区列表（`uint8_t*` 指针）
- `outputBuffers`：输出数据缓冲区列表（**函数会自动分配内存**）
- `outputSize`：输出数据大小列表（字节数）
- `perfProfile`：性能模式字符串
  - `"default"`：默认模式
  - `"high_performance"`：高性能模式
  - `"burst"`：突发模式
- `graphIndex`：图索引（多图模型）

**返回值**：

- `true`：推理成功
- `false`：推理失败

⚠️ **内存管理重要提示**：

- `outputBuffers` 中的内存由函数自动分配
- **必须手动释放**：使用 `free(outputBuffers[i])` 释放每个输出缓冲区

##### ModelDestroy - 销毁模型

```cpp
bool ModelDestroy(std::string model_name);
```

##### 模型信息查询方法

```cpp
// 获取输入形状
std::vector<std::vector<size_t>> getInputShapes(std::string model_name);

// 获取输出形状
std::vector<std::vector<size_t>> getOutputShapes(std::string model_name);

// 获取输入数据类型
std::vector<std::string> getInputDataType(std::string model_name);

// 获取输出数据类型
std::vector<std::string> getOutputDataType(std::string model_name);

// 获取图名称
std::string getGraphName(std::string model_name);

// 获取输入名称
std::vector<std::string> getInputName(std::string model_name);

// 获取输出名称
std::vector<std::string> getOutputName(std::string model_name);
```

### 4.2 日志和性能函数

#### 日志函数

```cpp
// 设置日志级别
bool SetLogLevel(int32_t log_level, const std::string log_path = "None");

// 日志输出函数
void QNN_ERR(const char* fmt, ...);  // 错误日志
void QNN_WAR(const char* fmt, ...);  // 警告日志
void QNN_INF(const char* fmt, ...);  // 信息日志
void QNN_VEB(const char* fmt, ...);  // 详细日志
void QNN_DBG(const char* fmt, ...);  // 调试日志
```

**日志级别常量**：

```cpp
#define QNN_LOG_LEVEL_ERROR   1
#define QNN_LOG_LEVEL_WARN    2
#define QNN_LOG_LEVEL_INFO    3
#define QNN_LOG_LEVEL_VERBOSE 4
#define QNN_LOG_LEVEL_DEBUG   5
```

#### 性能配置函数

```cpp
// 设置全局性能模式
bool SetPerfProfileGlobal(const std::string& perf_profile);

// 释放全局性能模式
bool RelPerfProfileGlobal();

// 设置性能分析级别
bool SetProfilingLevel(int32_t profiling_level);
```

**性能分析级别**：

```cpp
#define QNN_PROFILING_LEVEL_OFF      0
#define QNN_PROFILING_LEVEL_BASIC    1
#define QNN_PROFILING_LEVEL_DETAILED 2
```

#### TimerHelper - 计时工具类

```cpp
class TimerHelper {
public:
    TimerHelper();                                     // 构造函数（自动开始计时）
    void Reset();                                      // 重置计时器
    void Print(std::string message);                   // 打印经过的时间
    void Print(std::string message, bool reset);       // 打印并可选重置
};
```

### 4.3 完整 C++ 示例

#### 示例 1：图像超分辨率（Real-ESRGAN）

基于真实示例代码：`samples/C++/real_esrgan_x4plus/real_esrgan_x4plus.cpp`

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>
#include <filesystem>
#include <opencv2/opencv.hpp>
#include <xtensor/xarray.hpp>
#include <xtensor/xadapt.hpp>

namespace fs = std::filesystem;

const std::string MODEL_NAME = "real_esrgan_x4plus";
const int IMAGE_SIZE = 512;
const int SCALE = 4;

#define RGB_IMAGE_SIZE_F32(width, height) ((width) * (height) * 3 * 4)

// ============================================
// 辅助函数：转换 OpenCV Mat 为 xtensor
// ============================================
xt::xarray<float> ConvertTensor(cv::Mat &img, int scale) {
    int b = 1;
    int ch = img.channels();
    int hh = img.rows;
    int hw = img.cols;
    int out_channel = ch * (scale * scale);
    int h = hh / scale;
    int w = hw / scale;

    // 输入 img 是 HWC 格式
    size_t size = img.total();
    size_t channels = img.channels();
    std::vector<int> shape = {img.rows, img.cols, img.channels()};

    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, h, w, out_channel};

    // 转换为 xarray
    xt::xarray<float> input = xt::adapt(
        (float*)img.data, 
        size * channels, 
        xt::no_ownership(), 
        shape
    );

    input.reshape(reshape_scale);
    input = xt::transpose(input, {0, 1, 3, 5, 2, 4});
    input.reshape(reshape_final);

    return input;
}

int main() {
    // ============================================
    // 1. 设置路径
    // ============================================
    fs::path execution_ws = fs::current_path();
    fs::path backend_lib_path = execution_ws / "QnnHtp.dll";
    fs::path system_lib_path = execution_ws / "QnnSystem.dll";
    fs::path model_path = execution_ws / (MODEL_NAME + ".bin");
    fs::path input_path = execution_ws / "input.jpg";
    fs::path output_path = execution_ws / "output.jpg";

    // ============================================
    // 2. 初始化日志和性能分析
    // ============================================
    SetLogLevel(QNN_LOG_LEVEL_WARN);
    SetProfilingLevel(QNN_PROFILING_LEVEL_BASIC);

    // ============================================
    // 3. 创建 LibAppBuilder 实例并初始化模型
    // ============================================
    LibAppBuilder libAppBuilder;

    std::cout << "正在初始化模型..." << std::endl;
    int ret = libAppBuilder.ModelInitialize(
        MODEL_NAME,
        model_path.string(),
        backend_lib_path.string(),
        system_lib_path.string()
    );

    if (ret < 0) {
        std::cout << "模型加载失败" << std::endl;
        return -1;
    }
    std::cout << "模型初始化完成" << std::endl;

    // ============================================
    // 4. 读取和预处理图像
    // ============================================
    cv::Mat orig_image = cv::imread(input_path.string(), cv::IMREAD_COLOR);
    if (orig_image.empty()) {
        QNN_ERR("无法读取图像: %s", input_path.string().c_str());
        return -1;
    }

    // 转换为 RGB
    cv::Mat rgb_image;
    cv::cvtColor(orig_image, rgb_image, cv::COLOR_BGR2RGB);

    // 调整大小
    cv::Mat resized_image;
    cv::resize(rgb_image, resized_image, cv::Size(IMAGE_SIZE, IMAGE_SIZE));

    // 归一化到 [0, 1]
    cv::Mat input_mat;
    resized_image.convertTo(input_mat, CV_32FC3, 1.0 / 255.0);

    // 转换为模型输入格式
    xt::xarray<float> input_tensor = ConvertTensor(input_mat, 1);

    // 分配输入缓冲区
    uint32_t size = RGB_IMAGE_SIZE_F32(IMAGE_SIZE, IMAGE_SIZE);
    float* input_buffer = new float[size / 4];
    std::copy(input_tensor.begin(), input_tensor.end(), input_buffer);

    // ============================================
    // 5. 执行推理
    // ============================================
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    std::string perfProfile = "burst";

    inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_buffer));

    // 设置全局性能模式
    SetPerfProfileGlobal("burst");

    std::cout << "正在执行推理..." << std::endl;
    TimerHelper timer;

    ret = libAppBuilder.ModelInference(
        MODEL_NAME,
        inputBuffers,
        outputBuffers,
        outputSize,
        perfProfile
    );

    timer.Print("推理时间: ", false);

    // 释放性能模式
    RelPerfProfileGlobal();

    if (ret < 0) {
        std::cout << "推理失败" << std::endl;
        delete[] input_buffer;
        return -1;
    }
    std::cout << "推理完成" << std::endl;

    // ============================================
    // 6. 后处理
    // ============================================
    float* output_data = reinterpret_cast<float*>(outputBuffers[0]);

    int output_width = IMAGE_SIZE * SCALE;
    int output_height = IMAGE_SIZE * SCALE;
    int output_channels = 3;
    int output_tensor_size = output_width * output_height * output_channels;

    // 反归一化并转换为 uint8
    char* buffer = new char[output_tensor_size];
    for (int i = 0; i < output_tensor_size; i++) {
        float val = output_data[i];
        buffer[i] = std::max(0.0, std::min(255.0, val * 255.0));
    }

    // 创建输出图像
    cv::Mat output_mat(output_height, output_width, CV_8UC3, buffer);
    cv::Mat output_mat_bgr;
    cv::cvtColor(output_mat, output_mat_bgr, cv::COLOR_RGB2BGR);

    // 保存图像
    cv::imwrite(output_path.string(), output_mat_bgr);
    std::cout << "输出图像已保存到: " << output_path.string() << std::endl;

    // 显示图像（可选）
    cv::imshow("Output Image", output_mat_bgr);
    cv::waitKey(0);

    // ============================================
    // 7. 清理资源
    // ============================================
    delete[] input_buffer;
    delete[] buffer;

    // 释放输出缓冲区（重要！）
    for (size_t j = 0; j < outputBuffers.size(); j++) {
        free(outputBuffers[j]);
    }
    outputBuffers.clear();
    outputSize.clear();

    // 销毁模型
    libAppBuilder.ModelDestroy(MODEL_NAME);

    return 0;
}
```

#### 示例 2：图像分类（BEiT）

基于真实示例代码：`samples/C++/beit/beit.cpp`

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>
#include <vector>
#include <fstream>
#include <filesystem>
#include <opencv2/opencv.hpp>
#include <xtensor/xarray.hpp>
#include <xtensor/xadapt.hpp>
#include <xtensor/xmath.hpp>

namespace fs = std::filesystem;

const int IMAGENET_DIM = 224;

// ============================================
// Softmax 函数
// ============================================
xt::xarray<float> softmax(const xt::xarray<float>& x, std::size_t dim) {
    xt::xarray<float> exp_x = xt::exp(x);
    xt::xarray<float> sum_exp = xt::sum(exp_x, {dim}, xt::keep_dims);
    return exp_x / sum_exp;
}

// ============================================
// 转换 OpenCV Mat 为 xtensor（NCHW 格式）
// ============================================
xt::xarray<float> ConvertTensor(cv::Mat &img, int scale) {
    int b = 1;
    int ch = img.channels();
    int hh = img.rows;
    int hw = img.cols;
    int out_channel = ch * (scale * scale);
    int h = hh / scale;
    int w = hw / scale;

    // 输入 img 是 HWC 格式
    size_t size = img.total();
    size_t channels = img.channels();
    std::vector<int> shape = {img.cols, img.rows, img.channels()};

    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, out_channel, h, w};

    // 转换为 xarray
    xt::xarray<float> input = xt::adapt(
        (float*)img.data,
        size * channels,
        xt::no_ownership(),
        shape
    );

    input.reshape(reshape_scale);
    input = xt::transpose(input, {0, 1, 3, 5, 2, 4});
    input.reshape(reshape_final);

    return input;
}

// ============================================
// 预处理图像（ImageNet 标准）
// ============================================
xt::xarray<float> preprocess_image(const cv::Mat& image) {
    cv::Mat rgb_image;
    int scale = 1;

    // 转换为 RGB
    if (image.channels() == 3) {
        cv::cvtColor(image, rgb_image, cv::COLOR_BGR2RGB);
    } else {
        cv::cvtColor(image, rgb_image, cv::COLOR_GRAY2RGB);
    }

    // 1. 调整大小到 256x256
    cv::Mat resized_image;
    cv::resize(rgb_image, resized_image, cv::Size(256, 256), 0, 0, cv::INTER_LINEAR);

    // 2. 中心裁剪到 224x224
    int crop_x = (256 - IMAGENET_DIM) / 2;
    int crop_y = (256 - IMAGENET_DIM) / 2;
    cv::Rect roi(crop_x, crop_y, IMAGENET_DIM, IMAGENET_DIM);
    cv::Mat cropped_image = resized_image(roi).clone();

    // 3. 归一化到 [0, 1]
    cv::Mat input_mat;
    cropped_image.convertTo(input_mat, CV_32FC3, 1.0 / 255.0);

    // 4. 转换为 NCHW 格式
    xt::xarray<float> input_tensor = ConvertTensor(input_mat, scale);

    return input_tensor;
}

// ============================================
// BEiT 分类器类
// ============================================
class BEIT {
public:
    std::string model_name = "beit";
    std::string model_path;
    std::string backend_lib;
    std::string system_lib;
    LibAppBuilder libAppBuilder;

    BEIT(const std::string& model_path,
         const std::string& backend_lib,
         const std::string& system_lib)
        : model_path(model_path),
          backend_lib(backend_lib),
          system_lib(system_lib) {}

    int LoadModel() {
        std::cout << "正在加载模型..." << std::endl;
        int ret = libAppBuilder.ModelInitialize(
            model_name,
            model_path,
            backend_lib,
            system_lib,
            false  // async
        );
        std::cout << "模型加载完成" << std::endl;
        return ret;
    }

    int DestroyModel() {
        std::cout << "正在销毁模型..." << std::endl;
        int ret = libAppBuilder.ModelDestroy(model_name);
        return ret;
    }

    xt::xarray<float> predict(const cv::Mat& image) {
        std::cout << "正在预测..." << std::endl;

        int size = 3 * IMAGENET_DIM * IMAGENET_DIM;
        std::unique_ptr<float[]> input_buffer(new float[size]);

        // 预处理图像
        xt::xarray<float> input_tensor = preprocess_image(image);
        std::copy(input_tensor.begin(), input_tensor.end(), input_buffer.get());

        // 准备输入输出缓冲区
        std::vector<uint8_t*> inputBuffers;
        std::vector<uint8_t*> outputBuffers;
        std::vector<size_t> outputSize;
        std::string perfProfile = "burst";
        int graphIndex = 0;

        inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_buffer.get()));

        // 执行推理
        std::cout << "正在执行推理..." << std::endl;
        int ret = libAppBuilder.ModelInference(
            model_name,
            inputBuffers,
            outputBuffers,
            outputSize,
            perfProfile,
            graphIndex
        );
        std::cout << "推理完成" << std::endl;

        if (ret < 0) {
            std::cout << "推理失败" << std::endl;
            return xt::zeros<float>({1000});
        }

        // 处理输出
        float* pred_output = reinterpret_cast<float*>(outputBuffers[0]);
        size_t output_elements = 1000;  // ImageNet 类别数

        xt::xarray<float> output = xt::zeros<float>({output_elements});
        std::copy(pred_output, pred_output + output_elements, output.begin());

        // 释放输出缓冲区
        for (auto buffer : outputBuffers) {
            free(buffer);
        }

        // 应用 softmax
        return softmax(output, 0);
    }
};

// ============================================
// 加载 ImageNet 标签
// ============================================
std::vector<std::string> load_labels(const std::string& file_path) {
    std::vector<std::string> labels;
    std::ifstream file(file_path);

    if (file.is_open()) {
        std::string line;
        while (std::getline(file, line)) {
            labels.push_back(line);
        }
        file.close();
    }

    return labels;
}

// ============================================
// 主函数
// ============================================
int main() {
    // 设置路径
    std::string image_path = "../input.jpg";
    std::string json_path = "../models/imagenet_labels.json";
    std::string model_path = "../models/beit.bin";
    std::string backend_lib = "../qai_libs/QnnHtp.dll";
    std::string system_lib = "../qai_libs/QnnSystem.dll";

    // 读取图像
    cv::Mat image = cv::imread(image_path);
    if (image.empty()) {
        std::cout << "无法读取图像" << std::endl;
        return -1;
    }

    // 创建分类器
    BEIT beit(model_path, backend_lib, system_lib);

    // 设置日志级别
    SetLogLevel(QNN_LOG_LEVEL_WARN);

    // 加载模型
    int ret = beit.LoadModel();
    if (ret < 0) {
        std::cout << "模型加载失败" << std::endl;
        return -1;
    }

    // 执行预测
    xt::xarray<float> probabilities = beit.predict(image);
    std::cout << "预测完成，概率数组大小: " << probabilities.size() << std::endl;

    // 找到 Top-5 预测
    std::vector<std::pair<float, int>> indexed_probs;
    for (size_t i = 0; i < probabilities.size(); ++i) {
        indexed_probs.emplace_back(probabilities[i], static_cast<int>(i));
    }
    std::sort(indexed_probs.begin(), indexed_probs.end(),
              std::greater<std::pair<float, int>>());

    // 加载标签
    std::vector<std::string> labels = load_labels(json_path);

    // 打印 Top-5 结果
    std::cout << "\nTop 5 预测结果:" << std::endl;
    for (int i = 0; i < 5; ++i) {
        int class_idx = indexed_probs[i].second;
        float prob = indexed_probs[i].first;
        std::string label = (class_idx < labels.size()) ? labels[class_idx] : "Unknown";
        std::cout << (i + 1) << ". " << label << ": " 
                  << (100 * prob) << "%" << std::endl;
    }

    // 销毁模型
    ret = beit.DestroyModel();
    if (ret < 0) {
        std::cout << "模型销毁失败" << std::endl;
        return -1;
    }

    return 0;
}
```

## 5. 高级功能

### 5.1 LoRA 适配器支持

LoRA (Low-Rank Adaptation) 允许动态加载和切换模型适配器。

#### Python 示例

```python
from qai_appbuilder import QNNLoraContext, LoraAdapter, QNNConfig, Runtime, LogLevel

# 配置环境
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# ============================================
# 创建 LoRA 适配器
# ============================================
adapter1 = LoraAdapter(
    graph_name="llm_graph",
    lora_file_paths=[
        "lora/adapter1_layer1.bin",
        "lora/adapter1_layer2.bin"
    ]
)

adapter2 = LoraAdapter(
    graph_name="llm_graph",
    lora_file_paths=[
        "lora/adapter2_layer1.bin",
        "lora/adapter2_layer2.bin"
    ]
)

# ============================================
# 初始化带 LoRA 的模型
# ============================================
model = QNNLoraContext(
    model_name="llm_with_lora",
    model_path="models/base_llm.bin",
    lora_adapters=[adapter1]  # 初始加载 adapter1
)

# 执行推理
output1 = model.Inference([input_data])
print(f"使用 adapter1 的输出: {output1[0].shape}")

# ============================================
# 动态切换 LoRA 适配器
# ============================================
model.apply_binary_update([adapter2])

output2 = model.Inference([input_data])
print(f"使用 adapter2 的输出: {output2[0].shape}")

# 清理
del model
```

### 5.2 多图模型支持

某些模型包含多个计算图，可以通过 `graphIndex` 参数选择。

```python
from qai_appbuilder import QNNContext

model = QNNContext(
    model_name="multi_graph_model",
    model_path="models/multi_graph_model.bin"
)

# 使用第一个图
output1 = model.Inference([input_data], graphIndex=0)

# 使用第二个图
output2 = model.Inference([input_data], graphIndex=1)

del model
```

### 5.3 支持的模型格式

QAI AppBuilder 支持多种模型格式，适用于不同的运行时和场景。

#### 5.3.1 支持的模型格式列表

| 格式     | 运行时     | 说明                      |
| ------ | ------- | ----------------------- |
| `.bin` | HTP/CPU | 预编译的二进制格式，加载速度最快（推荐）    |
| `.dlc` | HTP/CPU | 从 QAIRT 2.41.0 开始支持直接加载 |
| `.so`  | CPU     | 共享库格式，仅在 CPU 上运行        |

#### 5.3.2 DLC 模型直接加载

从 QAIRT 2.41.0 版本开始，支持直接加载 `.dlc` 模型文件。

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel

QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# ============================================
# 直接加载 .dlc 文件
# ============================================
model = QNNContext(
    model_name="my_model",
    model_path="models/my_model.dlc"  # 使用 .dlc 文件
)

# 首次运行时，会自动生成 my_model.dlc.bin 文件
# 后续运行会直接加载 .dlc.bin 以提高速度

output = model.Inference([input_data])

del model
```

**注意事项**：

- 首次加载 `.dlc` 文件时会自动转换为 `.dlc.bin`
- 转换后的 `.dlc.bin` 文件会保存在同一目录
- 后续运行会直接加载 `.dlc.bin` 文件，速度更快
- 如果需要重新转换，删除 `.dlc.bin` 文件即可

#### 5.3.3 SO 模型格式（CPU 运行时）

对于需要在 CPU 上运行的模型，可以使用 `.so` 格式：

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel

# 配置为 CPU 运行时
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.CPU,  # 使用 CPU 运行时
    log_level=LogLevel.INFO
)

# 加载 .so 模型文件
model = QNNContext(
    model_name="cpu_model",
    model_path="models/my_model.so"  # 使用 .so 文件
)

output = model.Inference([input_data])

del model
```

---

## 6. 性能优化

### 6.1 使用 Native 模式（推荐）

**性能提升**：Native 模式可以减少 10%-200% 的数据转换开销。

```python
# ❌ Float 模式（默认）- 有转换开销
model_float = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.FLOAT,
    output_data_type=DataType.FLOAT
)

# ✅ Native 模式 - 无转换开销，性能更好
model_native = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)
```

### 6.2 使用 Burst 性能模式

在需要最高性能的场景下使用 Burst 模式。

```python
from qai_appbuilder import PerfProfile

# ✅ 全局设置（用于批量推理）
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

for i in range(100):
    output = model.Inference([input_data], perf_profile=PerfProfile.DEFAULT)

PerfProfile.RelPerfProfileGlobal()
```

### 6.3 批量推理优化

```python
# ❌ 不推荐：每次都初始化模型
for data in dataset:
    model = QNNContext(...)
    output = model.Inference([data])
    del model

# ✅ 推荐：只初始化一次
model = QNNContext(...)

PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
for data in dataset:
    output = model.Inference([data])
PerfProfile.RelPerfProfileGlobal()

del model
```

### 6.4 使用 ARM64 Python（Windows）

在 Windows on Snapdragon 平台上，ARM64 Python 比 x64 Python 性能更好。

```bash
# 下载 ARM64 Python
https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe

# 安装 ARM64 版本的 QAI AppBuilder
pip install qai_appbuilder-{version}-cp312-cp312-win_arm64.whl
```

## 7. 常见问题

### 7.1 模型加载失败

**问题**：`ModelInitialize` 返回 `False` 或失败

**解决方案**：

```python
from qai_appbuilder import QNNConfig, LogLevel, Runtime
from pathlib import Path

# 1. 启用详细日志
QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.DEBUG,  # 使用 DEBUG 级别
    log_path="./debug.log"     # 输出到文件
)

# 2. 检查文件是否存在
model_path = Path("models/my_model.bin")
if not model_path.exists():
    print(f"错误：模型文件不存在: {model_path}")
    exit()
```

### 7.2 推理结果不正确

**问题**：推理输出与预期不符

**解决方案**：

```python
# 1. 验证模型信息
print(f"输入形状: {model.getInputShapes()}")
print(f"输出形状: {model.getOutputShapes()}")
print(f"输入数据类型: {model.getInputDataType()}")
print(f"输出数据类型: {model.getOutputDataType()}")

# 2. 验证输入数据
expected_shape = model.getInputShapes()[0]
print(f"期望形状: {expected_shape}")
print(f"实际形状: {input_data.shape}")
print(f"实际数据类型: {input_data.dtype}")

# 3. 检查数据范围
print(f"输入数据范围: [{input_data.min()}, {input_data.max()}]")

# 4. 检查输出
output = model.Inference([input_data])
print(f"输出形状: {output[0].shape}")
print(f"输出数据类型: {output[0].dtype}")
print(f"输出数据范围: [{output[0].min()}, {output[0].max()}]")
```

### 7.3 内存泄漏

**Python 解决方案**：

```python
import gc

# 显式删除模型
model = QNNContext(...)
# ... 使用模型 ...
del model
gc.collect()  # 强制垃圾回收
```

**C++ 解决方案**：

```cpp
// 必须手动释放输出缓冲区
for (auto buffer : outputBuffers) {
    free(buffer);  // 释放每个输出缓冲区
}
outputBuffers.clear();
outputSize.clear();

// 销毁模型
appBuilder.ModelDestroy(model_name);
```

### 7.4 Native 模式数据类型不匹配

**问题**：Native 模式下数据类型错误导致推理失败

**解决方案**：

```python
from qai_appbuilder import QNNContext, DataType
import numpy as np

# 1. 使用 Native 模式初始化
model = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 2. 查询模型要求的数据类型
input_dtypes = model.getInputDataType()
input_shapes = model.getInputShapes()

print(f"模型要求的输入数据类型: {input_dtypes}")
print(f"模型要求的输入形状: {input_shapes}")

# 3. 创建数据类型映射
dtype_map = {
    'float16': np.float16,
    'fp16': np.float16,
    'float32': np.float32,
    'fp32': np.float32,
    'float': np.float32,
    'int8': np.int8,
    'uint8': np.uint8,
    'int16': np.int16,
    'uint16': np.uint16,
    'int32': np.int32,
    'uint32': np.uint32,
    'bool': np.bool_
}

# 4. 根据模型要求准备数据
input_dtype_str = input_dtypes[0].lower()
input_dtype = dtype_map.get(input_dtype_str, np.float32)

print(f"使用数据类型: {input_dtype}")

# 5. 创建正确类型的输入数据
input_data = np.random.randn(*input_shapes[0]).astype(input_dtype)

print(f"输入数据类型: {input_data.dtype}")
print(f"输入数据形状: {input_data.shape}")

# 6. 执行推理
output = model.Inference([input_data])

print(f"输出数据类型: {output[0].dtype}")
print(f"输出数据形状: {output[0].shape}")
```

### 7.5 C++ 链接错误

**问题**：LNK2038、LNK2001 或其他链接错误

**解决方案**：

确保 Visual Studio 项目配置正确：

1. **运行时库**（最常见问题）
   
   - 项目属性 → C/C++ → 代码生成 → 运行时库
   - 必须设置为：**多线程 DLL (/MD)**

2. **平台**
   
   - 项目属性 → 常规 → 平台
   - 设置为：**ARM64**（对于 WoS 平台）

3. **配置**
   
   - 使用 **Release** 配置（而非 Debug）

4. **C++ 标准**
   
   - 项目属性 → C/C++ → 语言 → C++ 语言标准
   - 设置为：**ISO C++17** 或更高

5. **字符集**
   
   - 项目属性 → 高级 → 字符集
   - 设置为：**使用 Unicode 字符集**

### 7.6 性能不佳

**问题**：推理速度慢于预期

**诊断和解决**：

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel, DataType, PerfProfile
import time

# 1. 确保使用 HTP（NPU）而非 CPU
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,  # 确保是 HTP
    log_level=LogLevel.INFO,
    profiling_level=ProfilingLevel.BASIC  # 启用性能分析
)

# 2. 使用 Native 模式
model = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 3. 使用 Burst 模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 4. 测试性能
start_time = time.time()
for _ in range(100):
    output = model.Inference([input_data])
end_time = time.time()

avg_time = (end_time - start_time) / 100
print(f"平均推理时间: {avg_time * 1000:.2f} ms")

PerfProfile.RelPerfProfileGlobal()
```

---

## 8. 参考资源

### 8.1 官方文档和资源

- **GitHub 仓库**：https://github.com/quic/ai-engine-direct-helper
- **Qualcomm AI Hub**：https://aihub.qualcomm.com/
- **AI Dev Home**：https://www.aidevhome.com/
- **Qualcomm® AI Runtime SDK**：https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

### 8.2 教程和博客

- [QAI_AppBuilder: 让本地 AI 部署触手可及！](https://docs.qualcomm.com/bundle/publicresource/80-94755-1_REV_AA_QAI_AppBuilder_-_WoS.pdf)
- [大语言模型系列(1): 3分钟上手，在骁龙AI PC上部署DeepSeek!](https://blog.csdn.net/csdnsqst0050/article/details/149425691)
- [大语言模型系列(2): 本地 OpenAI 兼容 API 服务的配置与部署](https://blog.csdn.net/csdnsqst0050/article/details/150208814)
- [高通平台大语言模型精选](https://www.aidevhome.com/?id=51)
- [QAI AppBuilder on Linux (QCS6490)](https://docs.radxa.com/en/dragon/q6a/app-dev/npu-dev/qai-appbuilder)

### 8.3 示例代码

- **Python 示例**：https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python
  
  - Real-ESRGAN（图像超分辨率）
  - YOLOv8（目标检测）
  - Whisper（语音识别）
  - Stable Diffusion（文生图）
  - BEiT（图像分类）
  - OpenPose（姿态估计）
  - Depth Anything（深度估计）
  - 等 20+ 个示例

- **C++ 示例**：https://github.com/quic/ai-engine-direct-helper/tree/main/samples/c++
  
  - Real-ESRGAN
  - BEiT（图像分类）

- **WebUI 应用**：https://github.com/quic/ai-engine-direct-helper/tree/main/samples/webui
  
  - ImageRepairApp（图像修复）
  - StableDiffusionApp（文生图）
  - GenieWebUI（LLM 对话）

### 8.4 模型资源

- **AI Hub 模型库**：https://aihub.qualcomm.com/compute/models
- **AI Dev Home 模型库**：https://www.aidevhome.com/data/models/
- **Qwen2 7B SSD**：https://www.aidevhome.com/data/adh2/models/8380/qwen2_7b_ssd_250702.html
- **DeepSeek-R1-Distill-Qwen-7B**：https://aiot.aidlux.com/zh/models/detail/78

---

## 9. 快速开始指南

### 9.1 第一个 Python 程序

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel
import numpy as np

# 1. 配置环境（必需）
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# 2. 加载模型
model = QNNContext(
    model_name="my_first_model",
    model_path="models/my_model.bin"
)

# 3. 准备输入
input_shape = model.getInputShapes()[0]
input_data = np.random.randn(*input_shape).astype(np.float32)

# 4. 执行推理
output = model.Inference([input_data])

# 5. 查看结果
print(f"输出形状: {output[0].shape}")
print(f"输出数据类型: {output[0].dtype}")

# 6. 清理
del model
```

### 9.2 第一个 C++ 程序

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>

int main() {
    // 1. 设置日志
    SetLogLevel(QNN_LOG_LEVEL_INFO);

    // 2. 创建 AppBuilder
    LibAppBuilder appBuilder;

    // 3. 初始化模型
    bool success = appBuilder.ModelInitialize(
        "my_first_model",
        "models/my_model.bin",
        "qai_libs/QnnHtp.dll",
        "qai_libs/QnnSystem.dll"
    );

    if (!success) {
        QNN_ERR("模型初始化失败");
        return -1;
    }

    // 4. 准备输入
    auto inputShapes = appBuilder.getInputShapes("my_first_model");
    size_t input_size = 1;
    for (auto dim : inputShapes[0]) {
        input_size *= dim;
    }

    float* input_data = new float[input_size];
    // ... 填充输入数据 ...

    std::vector<uint8_t*> inputBuffers;
    inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_data));

    // 5. 执行推理
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    std::string perfProfile = "burst";

    success = appBuilder.ModelInference(
        "my_first_model",
        inputBuffers,
        outputBuffers,
        outputSize,
        perfProfile
    );

    if (!success) {
        QNN_ERR("推理失败");
        delete[] input_data;
        return -1;
    }

    // 6. 处理输出
    float* output_data = reinterpret_cast<float*>(outputBuffers[0]);
    // ... 处理输出 ...

    // 7. 清理
    delete[] input_data;
    for (auto buffer : outputBuffers) {
        free(buffer);
    }
    appBuilder.ModelDestroy("my_first_model");

    return 0;
}
```

---

## 10. 版本历史

### v2.0.0（2025年1月 - 重大更新）

**主要新特性**：

- ✅ **简化部署**：Python 扩展包含所有必需的依赖库（包括 QAIRT SDK 运行时）
- ✅ **多模态支持**：对多模态模型 (Qwen2.5-3B-VL / Qwen2.5-3B-omini) 的支持。
- ✅ **DLC 支持**：支持直接加载 `.dlc` 模型文件（QAIRT 2.41.0+）
- ✅ **LLM 优化**：新增 `GenieContext` 类，专为大语言模型优化
- ✅ **性能提升**：改进 Native 模式，减少数据转换开销
- ✅ **增强分析**：改进性能分析功能，提供更详细的性能数据

**API 变更**：

- `QNNConfig.Config()` 的 `qnn_lib_path` 参数现在可选（默认使用内置库）
- `QNNContext` 的 `backend_lib_path` 和 `system_lib_path` 参数现在可选
- 新增 `GenieContext` 类用于 LLM 推理

**已知问题**：

- 某些 ARM64 Python 扩展可能需要手动编译
- Linux 平台上某些模型可能需要设置 `ADSP_LIBRARY_PATH`

### v1.x（历史版本）

**v1.5.0**（2024年12月）：
- 添加 LoRA 适配器支持
- 改进多图模型支持
- 优化内存管理

**v1.0.0**（2024年10月）：
- 首次正式发布
- 支持 Python 和 C++ API
- 支持 Windows 和 Linux 平台
- 支持 HTP 和 CPU 运行时

---

## 11. 许可证

QAI AppBuilder 采用 **BSD 3-Clause "New" or "Revised" License**。

详见：https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE

---

## 12. 免责声明

本软件按"原样"提供，不提供任何明示或暗示的保证。作者和贡献者不对因使用本软件而产生的任何损害承担责任。代码可能不完整或测试不充分。用户需自行评估其适用性并承担所有相关风险。

**注意**：欢迎贡献。在关键系统中部署前请确保充分测试。

---

## 13. 贡献和支持

### 报告问题

如果遇到问题，请访问：

- **GitHub Issues**：https://github.com/quic/ai-engine-direct-helper/issues
- **GitHub Discussions**：https://github.com/quic/ai-engine-direct-helper/discussions

### 贡献代码

欢迎提交 Pull Request！请参阅：

- **贡献指南**：https://github.com/quic/ai-engine-direct-helper/blob/main/CONTRIBUTING.md
- **行为准则**：https://github.com/quic/ai-engine-direct-helper/blob/main/CODE-OF-CONDUCT.md

---

<div align="center">
  <p>⭐ 如果这个项目对你有帮助，请给我们一个 Star！</p>
  <p>📧 有问题或建议？访问 <a href="https://github.com/quic/ai-engine-direct-helper">GitHub 仓库</a></p>
</div>

---

**文档版本**：2.1  
**最后更新**：2025年1月26日  
**适用于**：QAI AppBuilder v2.0.0+
