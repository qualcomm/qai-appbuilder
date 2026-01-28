# QAI AppBuilder User Guide

<div align="center">
  <h2>Rapid AI Application Development Framework Based on Qualcomm® AI Runtime SDK</h2>
  <p><i>Simple | Easy | Efficient | Reliable</i></p>
</div>

---

## 📑 Table of Contents

1. [Introduction](#1-introduction)
   
   - 1.1 [What is QAI AppBuilder?](#11-what-is-qai-appbuilder)
   - 1.2 [Key Features](#12-key-features)
   - 1.3 [System Architecture](#13-system-architecture)
   - 1.4 [Supported Platforms](#14-supported-platforms)

2. [Environment Setup](#2-environment-setup)
   
   - 2.1 [Windows Environment Configuration](#21-windows-environment-configuration)
   - 2.2 [Linux Environment Configuration](#22-linux-environment-configuration)
   - 2.3 [C++ Environment Configuration](#23-c-environment-configuration)

3. [Python API Reference](#3-python-api-reference)
   
   - 3.1 [Core Classes Overview](#31-core-classes-overview)
   - 3.2 [QNNConfig - Global Configuration (Required)](#32-qnnconfig---global-configuration-required)
   - 3.3 [QNNContext - Standard Model Context (Core Class)](#33-qnncontext---standard-model-context-core-class)
   - 3.4 [Best Practices for Inheriting QNNContext](#34-best-practices-for-inheriting-qnncontext)
   - 3.5 [Complete Example: Image Super-Resolution (Real-ESRGAN)](#35-complete-example-image-super-resolution-real-esrgan)
   - 3.6 [Complete Example: Image Classification (BEiT)](#36-complete-example-image-classification-beit)
   - 3.7 [Complete Example: Speech Recognition (Whisper) - Native Mode](#37-complete-example-speech-recognition-whisper---native-mode)
   - 3.8 [Complete Example: Stable Diffusion Text-to-Image](#38-complete-example-stable-diffusion-text-to-image)
   - 3.9 [PerfProfile - Performance Mode Management](#39-perfprofile---performance-mode-management)
   - 3.10 [Native Mode Explained (High Performance)](#310-native-mode-explained-high-performance)

4. [C++ API Reference](#4-c-api-reference)
   
   - 4.1 [LibAppBuilder Class](#41-libappbuilder-class)
   - 4.2 [Logging and Performance Functions](#42-logging-and-performance-functions)
   - 4.3 [Complete C++ Examples](#43-complete-c-examples)

5. [Advanced Features](#5-advanced-features)
   
   - 5.1 [LoRA Adapter Support](#51-lora-adapter-support)
   - 5.2 [Multi-Graph Model Support](#52-multi-graph-model-support)
   - 5.3 [Supported Model Formats](#53-supported-model-formats)

6. [Performance Optimization](#6-performance-optimization)
   
   - 6.1 [Use Native Mode (Recommended)](#61-use-native-mode-recommended)
   - 6.2 [Use Burst Performance Mode](#62-use-burst-performance-mode)
   - 6.3 [Batch Inference Optimization](#63-batch-inference-optimization)
   - 6.4 [Use ARM64 Python (Windows)](#64-use-arm64-python-windows)

7. [FAQ](#7-faq)
   
   - 7.1 [Model Loading Failure](#71-model-loading-failure)
   - 7.2 [Incorrect Inference Results](#72-incorrect-inference-results)
   - 7.3 [Memory Leaks](#73-memory-leaks)
   - 7.4 [Native Mode Data Type Mismatch](#74-native-mode-data-type-mismatch)
   - 7.5 [C++ Linking Errors](#75-c-linking-errors)
   - 7.6 [Poor Performance](#76-poor-performance)

8. [Reference Resources](#8-reference-resources)
   
   - 8.1 [Official Documentation and Resources](#81-official-documentation-and-resources)
   - 8.2 [Tutorials and Blogs](#82-tutorials-and-blogs)
   - 8.3 [Sample Code](#83-sample-code)
   - 8.4 [Model Resources](#84-model-resources)

9. [Quick Start Guide](#9-quick-start-guide)
   
   - 9.1 [First Python Program](#91-first-python-program)
   - 9.2 [First C++ Program](#92-first-c-program)

10. [Version History](#10-version-history)

11. [License](#11-license)

12. [Disclaimer](#12-disclaimer)

13. [Contribution and Support](#13-contribution-and-support)

---

## 1. Introduction

### 1.1 What is QAI AppBuilder?

QAI AppBuilder (Quick AI Application Builder) is an extension tool for Qualcomm® AI Runtime SDK, designed to **simplify the deployment process of QNN models**. It encapsulates complex model execution APIs into a set of simplified interfaces, enabling developers to easily load models and execute inference on CPU or NPU(HTP), significantly reducing the complexity of deploying AI models on Windows on Snapdragon (WoS) and Linux platforms.

### 1.2 Key Features

- ✅ **Dual Language Support**: Supports both C++ and Python
- ✅ **Cross-Platform**: Supports Windows, Linux, and Android
- ✅ **Multi-Runtime**: Supports CPU and NPU(HTP) execution
- ✅ **Large Language Model Support**: Built-in Genie framework for LLM support
- ✅ **Multimodal Support**: Supports multimodal large language models
- ✅ **Flexible Data Types**: Supports Float and Native mode input/output
- ✅ **Multi-Graph Support**: Supports multiple computation graphs
- ✅ **LoRA Support**: Supports dynamic loading of LoRA adapters
- ✅ **Multi-Model Support**: Can load multiple models simultaneously
- ✅ **Rich Examples**: Provides 20+ runnable example codes

### 1.3 System Architecture

```
┌─────────────────────────────────────────────────────┐
│         Application Layer                            │
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

### 1.4 Supported Platforms

- **Windows on Snapdragon (WoS)**: X Elite Windows
- **Linux**: QCS8550, QCM6490 Ubuntu
- **Android**: Snapdragon® 8 Elite, Snapdragon® 8 Elite Gen 5
- **Architecture Support**: ARM64, ARM64EC

---

## 2. Environment Setup

### 2.1 Windows Environment Configuration

#### Step 1: Install Dependencies

**1. Install Git**

```bash
# Download Git for ARM64
https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe
```

**2. Install Python 3.12.8**

Use **x64 version** for better Python extension ecosystem (currently, more Python extensions can run in x64 Python environment, while some extensions for ARM64 Python need to be compiled manually):

```bash
# Download Python 3.12.8 x64
https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
```

Or use **ARM64 version** for better performance:

```bash
# Download Python 3.12.8 ARM64
https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe
```

⚠️ **Important Notes**:

- Must check "Add python.exe to PATH" during installation
- If there are multiple Python versions in the system, ensure the newly installed version is first in the PATH environment variable

Verify Python version order:

```cmd
where python
```

**3. Install Visual C++ Redistributable**

```bash
# Download and install
https://aka.ms/vs/17/release/vc_redist.x64.exe
https://aka.ms/vs/17/release/vc_redist.arm64.exe
```

#### Step 2: Clone QAI AppBuilder Repository

```bash
# Clone repository
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive

# If already cloned, update code
cd ai-engine-direct-helper
git pull --recurse-submodules
```

#### Step 3: Install QAI AppBuilder Python Extension

Download the corresponding version of `.whl` file from [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases):

```bash
# For x64 Python
pip install qai_appbuilder-{version}-cp312-cp312-win_amd64.whl

# For ARM64 Python
pip install qai_appbuilder-{version}-cp312-cp312-win_arm64.whl
```

💡 **Important Note**: Starting from v2.0.0, the QAI AppBuilder Python extension already includes all necessary dependency libraries (including Qualcomm® AI Runtime SDK runtime libraries), eliminating the need for additional Qualcomm® AI Runtime SDK installation. This greatly simplifies the environment configuration process for Python developers.

### 2.2 Linux Environment Configuration

#### Step 1: Install Dependencies

**1. Install Basic Tools**

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y git python3 python3-pip build-essential

# Verify installation
python3 --version
pip3 --version
```

**2. Install Python Dependencies**

```bash
# Install common Python libraries
pip3 install numpy pillow opencv-python
```

#### Step 2: Clone QAI AppBuilder Repository

```bash
# Clone repository
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive

# If already cloned, update code
cd ai-engine-direct-helper
git pull --recurse-submodules
```

#### Step 3: Install QAI AppBuilder Python Extension

Download the corresponding version of `.whl` file from [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases):

```bash
# For Linux ARM64
pip3 install qai_appbuilder-{version}-cp310-cp310-linux_aarch64.whl
```

💡 **Important Note**: Starting from v2.0.0, the QAI AppBuilder Python extension already includes all necessary dependency libraries (including Qualcomm® AI Runtime SDK runtime libraries), eliminating the need for additional Qualcomm® AI Runtime SDK installation.

#### Step 4: Configure Environment Variables (Optional)

For some Linux platforms, you may need to set the `ADSP_LIBRARY_PATH` environment variable:

```bash
# Add to ~/.bashrc or ~/.zshrc
export ADSP_LIBRARY_PATH=/path/to/qnn/libs

# Apply configuration
source ~/.bashrc
```

#### Key Differences Between Linux and Windows

| Item | Windows | Linux |
| --- | --- | --- |
| Library Extension | `.dll` | `.so` |
| Backend Libraries | `QnnHtp.dll`, `QnnCpu.dll` | `libQnnHtp.so`, `libQnnCpu.so` |
| System Library | `QnnSystem.dll` | `libQnnSystem.so` |
| Python | `python.exe` | `python3` |
| Path Separator | `\` (backslash) | `/` (forward slash) |
| Environment Variable Setup | System Properties → Environment Variables | `~/.bashrc` or `~/.zshrc` |
| Special Environment Variable | None | `ADSP_LIBRARY_PATH` (required on some platforms) |

### 2.3 C++ Environment Configuration

#### Step 1: Download Precompiled Libraries

Download the precompiled library for the corresponding platform from [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases):

**Windows ARM64**:
```
QAI_AppBuilder-win_arm64-{version}-Release.zip
```

**Linux ARM64**:
```
QAI_AppBuilder-linux_aarch64-{version}-Release.tar.gz
```

After extraction, includes:

- **Windows**:
  - `libappbuilder.dll` - Main library
  - `libappbuilder.lib` - Import library
  - `LibAppBuilder.hpp` - Header file
  - `Lora.hpp` - LoRA support header file

- **Linux**:
  - `libappbuilder.so` - Main library
  - `LibAppBuilder.hpp` - Header file
  - `Lora.hpp` - LoRA support header file

#### Step 2: Configure Build Environment

##### Windows - Visual Studio Configuration

**Required Project Configuration**:

1. **Include Directories**
   
   - Project Properties → C/C++ → General → Additional Include Directories
   - Add: `$(ProjectDir)include` or path where header files are located

2. **Library Directories**
   
   - Project Properties → Linker → General → Additional Library Directories
   - Add: `$(ProjectDir)lib` or path where `.lib` files are located

3. **Linker Input**
   
   - Project Properties → Linker → Input → Additional Dependencies
   - Add: `libappbuilder.lib`

4. **Runtime Library** (⚠️ Important)
   
   - Project Properties → C/C++ → Code Generation → Runtime Library
   - Set to: **Multi-threaded DLL (/MD)**

5. **C++ Standard**
   
   - Project Properties → C/C++ → Language → C++ Language Standard
   - Set to: **ISO C++17 Standard (/std:c++17)** or higher

---

## 3. Python API Reference

### 3.1 Core Classes Overview

| Class Name | Purpose | Recommended Use | Description |
| --- | --- | --- | --- |
| `QNNContext` | Standard model context | ✅ Recommended | Most commonly used class, suitable for most scenarios |
| `QNNLoraContext` | LoRA model context | ✅ Recommended | Supports dynamic loading of LoRA adapters |
| `QNNContextProc` | Process-isolated model context | ✅ Recommended | For multi-process scenarios |
| `QNNShareMemory` | Shared memory management | Use with QNNContextProc | Efficient data transfer between processes |
| `QNNConfig` | Global configuration management | ✅ Required | Must be called before using other APIs |
| `LogLevel` | Log level control | ✅ Recommended | ERROR, WARN, INFO, VERBOSE, DEBUG |
| `ProfilingLevel` | Performance profiling level | Optional | OFF, BASIC, DETAILED |
| `PerfProfile` | Performance mode management | ✅ Recommended | DEFAULT, HIGH_PERFORMANCE, BURST |
| `Runtime` | Runtime selection | ✅ Required | HTP or CPU |
| `DataType` | Data type mode | ✅ Recommended | FLOAT or NATIVE |
| `LoraAdapter` | LoRA adapter | Use with QNNLoraContext | Define LoRA adapter file paths |
| `GenieContext` | LLM-specific context | ✅ Recommended (LLM scenarios) | Context class optimized for LLM |

### 3.2 QNNConfig - Global Configuration (Required)

`QNNConfig` is used to configure the QNN runtime environment and **must be called before using other APIs**.

#### API Signature

```python
class QNNConfig:
    @staticmethod
    def Config(
        qnn_lib_path: str = "None",                    # QAIRT runtime library path
        runtime: str = Runtime.HTP,                    # Runtime
        log_level: int = LogLevel.ERROR,               # Log level
        profiling_level: int = ProfilingLevel.OFF,     # Performance profiling level
        log_path: str = "None"                         # Log file path
    ) -> None
```

#### Parameter Details

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `qnn_lib_path` | str | "None" | QNN library file directory path (containing QnnHtp.dll, etc., **Starting from QAI AppBuilder v2.0.0, this parameter does not need to be set, leave it empty by default**) |
| `runtime` | str | Runtime.HTP | `Runtime.HTP` (NPU) or `Runtime.CPU` |
| `log_level` | int | LogLevel.ERROR | ERROR(1), WARN(2), INFO(3), VERBOSE(4), DEBUG(5) |
| `profiling_level` | int | ProfilingLevel.OFF | OFF(0), BASIC(1), DETAILED(2) |
| `log_path` | str | "None" | Log file path, "None" means output to console |

#### Usage Example

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel

QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)
```

### 3.3 QNNContext - Standard Model Context (Core Class)

`QNNContext` is the most commonly used class for loading models, executing inference, and managing model lifecycle.

#### Constructor

```python
class QNNContext:
    def __init__(
        self,
        model_name: str = "None",                      # Model name (unique identifier)
        model_path: str = "None",                      # Model file path
        backend_lib_path: str = "None",                # Backend library path (optional)
        system_lib_path: str = "None",                 # System library path (optional)
        is_async: bool = False,                        # Whether to execute asynchronously
        input_data_type: str = DataType.FLOAT,         # Input data type
        output_data_type: str = DataType.FLOAT         # Output data type
    ) -> None
```

#### Parameter Details

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `model_name` | str | "None" | Model unique identifier for distinguishing different models |
| `model_path` | str | "None" | Model file path (supports `.bin` and `.dlc` formats) |
| `backend_lib_path` | str | "None" | QnnHtp.dll or QnnCpu.dll path (optional, **Starting from QAI AppBuilder v2.0.0, this parameter does not need to be set**) |
| `system_lib_path` | str | "None" | QnnSystem.dll path (optional, **Starting from QAI AppBuilder v2.0.0, this parameter does not need to be set**) |
| `is_async` | bool | False | Whether to enable asynchronous inference |
| `input_data_type` | str | DataType.FLOAT | `DataType.FLOAT` or `DataType.NATIVE` |
| `output_data_type` | str | DataType.FLOAT | `DataType.FLOAT` or `DataType.NATIVE` |

💡 **Tip**: Starting from QAI AppBuilder **v2.0.0**, the parameters `backend_lib_path` and `system_lib_path` do not need to be set.

#### Core Methods

##### Inference - Execute Inference

```python
def Inference(
    self,
    input: List[np.ndarray],                           # Input data list
    perf_profile: str = PerfProfile.DEFAULT,           # Performance mode
    graphIndex: int = 0                                # Graph index
) -> List[np.ndarray]                                  # Returns output list
```

**Parameter Description**:

- `input`: Input data list, each element is a NumPy array
- `perf_profile`: Performance mode (not recommended to use this parameter)
  - `PerfProfile.DEFAULT`: Default mode (does not change performance configuration)
  - `PerfProfile.HIGH_PERFORMANCE`: High performance mode
  - `PerfProfile.BURST`: Burst mode (highest performance)
- `graphIndex`: Graph index (for multi-graph models, default is 0)

**Return Value**:

- Output data list, each element is a NumPy array

💡 **Tip**: It is not recommended to use the perf_profile parameter. It is recommended to use PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST) and PerfProfile.RelPerfProfileGlobal() in pairs to set the NPU to high performance mode.

##### Model Information Query Methods

```python
# Get input shapes (e.g., [[1, 3, 224, 224]])
def getInputShapes(self) -> List[List[int]]

# Get output shapes (e.g., [[1, 1000]])
def getOutputShapes(self) -> List[List[int]]

# Get input data types (e.g., ['float32'] or ['float16'])
def getInputDataType(self) -> List[str]

# Get output data types (e.g., ['float32'] or ['float16'])
def getOutputDataType(self) -> List[str]

# Get graph name
def getGraphName(self) -> str

# Get input tensor names (e.g., ['input'])
def getInputName(self) -> List[str]

# Get output tensor names (e.g., ['output'])
def getOutputName(self) -> List[str]
```

### 3.4 Best Practices for Inheriting QNNContext

Example code showing how to **inherit the `QNNContext` class** to encapsulate specific model logic.

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, PerfProfile
import numpy as np
import os

# Configure environment
QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN
)

# ============================================
# Method 1: Simple Inheritance (Most Common)
# ============================================
class RealESRGan(QNNContext):
    """Real-ESRGAN Image Super-Resolution Model"""

    def Inference(self, input_data):
        """Override Inference method to simplify calls"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# Use custom model class
model_path = "models/real_esrgan_x4plus.bin"
realesrgan = RealESRGan("realesrgan", model_path)

# Execute inference
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
output = realesrgan.Inference(input_data)
PerfProfile.RelPerfProfileGlobal()

del realesrgan

# ============================================
# Method 2: Multi-Input Model
# ============================================
class Unet(QNNContext):
    """UNet Denoising Model - Multi-Input"""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        """Accept multiple inputs"""
        # Reshape to 1D array
        input_data_1 = input_data_1.reshape(input_data_1.size)
        # input_data_2 is already 1D, no need to reshape
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        # Reshape output
        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

# Usage
unet = Unet("unet", "models/unet.bin")
output = unet.Inference(latent, timestep, text_embedding)
del unet

# ============================================
# Method 3: Multi-Output Model
# ============================================
class Encoder(QNNContext):
    """Whisper Encoder - Multi-Output"""

    def Inference(self, input_data):
        """Return multiple outputs"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)

        # Reshape each output
        k_cache_cross = output_data[0].reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1].reshape(6, 8, 1500, 64)

        return k_cache_cross, v_cache_cross

# Usage
encoder = Encoder("whisper_encoder", "models/encoder.bin")
k_cache, v_cache = encoder.Inference(mel_input)
del encoder
```

### 3.5 Complete Example: Image Super-Resolution (Real-ESRGAN)

Reference code: `samples/python/real_esrgan_x4plus/real_esrgan_x4plus.py`

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
# 1. Configure QNN Environment
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

# Handle different working directory situations
if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "real_esrgan_x4plus"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # This parameter can be left empty starting from v2.0.0
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. Define Model Class (Inherit from QNNContext)
# ============================================
class RealESRGan(QNNContext):
    """Real-ESRGAN Image Super-Resolution Model"""

    def Inference(self, input_data):
        """Override Inference method to simplify calls"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. Initialize Model
# ============================================
IMAGE_SIZE = 512
model_path = model_dir / f"{MODEL_NAME}.bin"

# Create model instance
realesrgan = RealESRGan("realesrgan", str(model_path))

# Query model information (optional)
print(f"Model name: {realesrgan.getGraphName()}")
print(f"Input shapes: {realesrgan.getInputShapes()}")
print(f"Output shapes: {realesrgan.getOutputShapes()}")
print(f"Input data types: {realesrgan.getInputDataType()}")
print(f"Output data types: {realesrgan.getOutputDataType()}")

# ============================================
# 4. Image Preprocessing Helper Functions
# ============================================
def pil_resize_pad(image, target_size):
    """Resize image and pad to target size"""
    orig_width, orig_height = image.size
    target_width, target_height = target_size

    # Calculate scale ratio
    scale = min(target_width / orig_width, target_height / orig_height)
    new_width = int(orig_width * scale)
    new_height = int(orig_height * scale)

    # Resize
    image = image.resize((new_width, new_height), Image.LANCZOS)

    # Create new image and pad
    new_image = Image.new('RGB', target_size, (0, 0, 0))
    paste_x = (target_width - new_width) // 2
    paste_y = (target_height - new_height) // 2
    new_image.paste(image, (paste_x, paste_y))

    padding = (paste_x, paste_y)
    return new_image, scale, padding

def pil_undo_resize_pad(image, original_size, scale, padding):
    """Remove padding and restore to original size"""
    # Crop padding
    width, height = image.size
    left = padding[0] * 4
    top = padding[1] * 4
    right = width - padding[0] * 4
    bottom = height - padding[1] * 4
    image = image.crop((left, top, right, bottom))

    # Resize to original size
    image = image.resize(original_size, Image.LANCZOS)
    return image

# ============================================
# 5. Execute Inference
# ============================================
input_image_path = execution_ws / "input.jpg"
output_image_path = execution_ws / "output.png"

# Read and preprocess image
orig_image = Image.open(input_image_path)
image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

image = np.array(image)
image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)  # Normalize

# Set HTP to burst mode for best performance
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Execute inference
output_image = realesrgan.Inference(image)

# Reset HTP performance mode
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 6. Post-processing
# ============================================
# Reshape output shape
output_image = output_image.reshape(IMAGE_SIZE * 4, IMAGE_SIZE * 4, 3)

# Denormalize
output_image = np.clip(output_image, 0.0, 1.0)
output_image = (output_image * 255).astype(np.uint8)

# Convert to PIL image
output_image = Image.fromarray(output_image)

# Remove padding and restore original size
image_size = (orig_image.size[0] * 4, orig_image.size[1] * 4)
image_padding = (padding[0] * 4, padding[1] * 4)
output_image = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

# Save result
output_image.save(output_image_path)
print(f"Super-resolution image saved to: {output_image_path}")

# ============================================
# 7. Clean Up Resources
# ============================================
del realesrgan
```

### 3.6 Complete Example: Image Classification (BEiT)

Reference code: `samples/python/beit/beit.py`

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
# 1. Configure Environment
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
    qnn_lib_path=str(qnn_dir),  # This parameter can be left empty starting from v2.0.0
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. Define BEiT Model Class
# ============================================
class Beit(QNNContext):
    """BEiT Image Classification Model"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. Initialize Model
# ============================================
IMAGE_SIZE = 224
model_path = model_dir / f"{MODEL_NAME}.bin"

beit = Beit("beit", str(model_path))

# ============================================
# 4. Image Preprocessing
# ============================================
def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Preprocess PIL image"""
    preprocess = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
    ])

    img = preprocess(image)
    img = img.unsqueeze(0)
    return img

# ============================================
# 5. Execute Inference
# ============================================
input_image_path = execution_ws / "input.jpg"

# Read and preprocess image
image = Image.open(input_image_path)
image = preprocess_PIL_image(image).numpy()
image = np.transpose(image, (0, 2, 3, 1))  # NCHW -> NHWC

# Set burst mode
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Execute inference
output_data = beit.Inference(image)

# Reset performance mode
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 6. Post-processing
# ============================================
# Convert to torch tensor and apply softmax
output = torch.from_numpy(output_data).squeeze(0)
probabilities = torch.softmax(output, dim=0)

# Get Top-5 predictions
top5_prob, top5_catid = torch.topk(probabilities, 5)

print("\nTop 5 Prediction Results:")
for i in range(5):
    print(f"{i+1}. Class {top5_catid[i]}: {top5_prob[i].item():.6f}")

# ============================================
# 7. Clean Up Resources
# ============================================
del beit
```

### 3.7 Complete Example: Speech Recognition (Whisper) - Native Mode

Reference code: `samples/python/whisper_base_en/whisper_base_en.py`

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
# 1. Configure Environment
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
    qnn_lib_path=str(qnn_dir),  # This parameter can be left empty starting from v2.0.0
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. Define Encoder and Decoder Classes (Using Native Mode)
# ============================================
class Encoder(QNNContext):
    """Whisper Encoder - Using Native Mode"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)

        # Reshape outputs
        k_cache_cross = output_data[0].reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1].reshape(6, 8, 1500, 64)

        return k_cache_cross, v_cache_cross

class Decoder(QNNContext):
    """Whisper Decoder - Using Native Mode"""

    def Inference(self, x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self):
        input_datas = [x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self]
        output_data = super().Inference(input_datas)

        # Reshape outputs
        logits = output_data[0].reshape(1, 1, 51864)
        k_cache = output_data[1].reshape(6, 8, 64, 224)
        v_cache = output_data[2].reshape(6, 8, 224, 64)

        return logits, k_cache, v_cache

# ============================================
# 3. Initialize Models (Native Mode)
# ============================================
encoder_model_path = model_dir / "whisper_base_en-whisperencoder.bin"
decoder_model_path = model_dir / "whisper_base_en-whisperdecoder.bin"

# Initialize using Native mode
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

# View model's native data types
print("\nEncoder Model Information:")
print(f"  Input data types: {encoder.getInputDataType()}")
print(f"  Output data types: {encoder.getOutputDataType()}")

print("\nDecoder Model Information:")
print(f"  Input data types: {decoder.getInputDataType()}")
print(f"  Output data types: {decoder.getOutputDataType()}")

# ============================================
# 4. Whisper Constants Definition
# ============================================
TOKEN_SOT = 50257  # Start of transcript
TOKEN_EOT = 50256  # End of transcript
SAMPLE_RATE = 16000
CHUNK_LENGTH = 30  # seconds
MAX_AUDIO_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE

# ============================================
# 5. Audio Preprocessing Function
# ============================================
def log_mel_spectrogram(audio_np: np.ndarray) -> np.ndarray:
    """Compute Mel spectrogram (returns float16)"""
    audio = torch.from_numpy(audio_np)

    # Pad audio to fixed length
    padding = MAX_AUDIO_SAMPLES - len(audio)
    if padding > 0:
        audio = torch.nn.functional.pad(audio, (0, padding))

    # Compute STFT
    n_fft = 400
    hop_length = 160
    window = torch.hann_window(n_fft)
    stft = torch.stft(audio, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    # Apply Mel filters (need to be pre-loaded)
    # mel_filter = np.load("mel_filters.npz")["mel_80"]
    # mel_spec = torch.from_numpy(mel_filter) @ magnitudes

    # Compute log mel spectrogram
    log_spec = torch.clamp(magnitudes, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0

    # Return float16 (Native mode)
    return log_spec.unsqueeze(0).to(dtype=torch.float16).cpu().numpy()

# ============================================
# 6. Execute Inference
# ============================================
audio_path = execution_ws / "jfk.wav"

# Read audio file
audio, audio_sample_rate = a2n.audio_from_file(str(audio_path))

# Resample to 16kHz
if audio_sample_rate != SAMPLE_RATE:
    audio = samplerate.resample(audio, SAMPLE_RATE / audio_sample_rate)

# Compute Mel spectrogram (returns float16)
mel_input = log_mel_spectrogram(audio)
print(f"mel_input: dtype={mel_input.dtype}, shape={mel_input.shape}")

# Set burst mode
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Encoder inference
print("Executing Encoder inference...")
k_cache_cross, v_cache_cross = encoder.Inference(mel_input)

print(f"k_cache_cross: shape={k_cache_cross.shape}, dtype={k_cache_cross.dtype}")
print(f"v_cache_cross: shape={v_cache_cross.shape}, dtype={v_cache_cross.dtype}")

# ============================================
# 7. Decoder Inference (Autoregressive Generation)
# ============================================
# Initialize Decoder inputs
x = np.array([[TOKEN_SOT]], dtype=np.int32)
index = np.array([[0]], dtype=np.int32)
k_cache_self = np.zeros((6, 8, 64, 224), dtype=np.float16)
v_cache_self = np.zeros((6, 8, 224, 64), dtype=np.float16)

decoded_tokens = [TOKEN_SOT]
max_tokens = 100

print("\nExecuting Decoder inference (autoregressive generation)...")
for i in range(max_tokens):
    index = np.array([[i]], dtype=np.int32)

    # Decoder inference
    logits, k_cache_self, v_cache_self = decoder.Inference(
        x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
    )

    # Get next token
    next_token = np.argmax(logits[0, -1])
    decoded_tokens.append(int(next_token))

    # Check if finished
    if next_token == TOKEN_EOT:
        break

    # Update input
    x = np.array([[next_token]], dtype=np.int32)

    if (i + 1) % 10 == 0:
        print(f"  Generated {i + 1} tokens...")

# Reset performance mode
PerfProfile.RelPerfProfileGlobal()

print(f"\nGeneration complete, total {len(decoded_tokens)} tokens")

# ============================================
# 8. Decode Tokens to Text
# ============================================
tokenizer = whisper.decoding.get_tokenizer(
    multilingual=False, language="en", task="transcribe"
)
text = tokenizer.decode(decoded_tokens[1:])  # Remove TOKEN_SOT
print(f"Transcription result: {text.strip()}")

# ============================================
# 9. Clean Up Resources
# ============================================
del encoder
del decoder
```

### 3.8 Complete Example: Stable Diffusion Text-to-Image

Reference code: `samples/python/stable_diffusion_v1_5/stable_diffusion_v1_5.py`

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
# 1. Configure Environment
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
    qnn_lib_path=str(qnn_dir),  # This parameter can be left empty starting from v2.0.0
    runtime=Runtime.HTP,
    log_level=LogLevel.ERROR,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. Define Model Classes
# ============================================
class TextEncoder(QNNContext):
    """Text Encoder"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        # Output shape should be (1, 77, 768)
        output_data = output_data.reshape((1, 77, 768))
        return output_data

class Unet(QNNContext):
    """UNet Denoising Model"""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        # Reshape to 1D array
        input_data_1 = input_data_1.reshape(input_data_1.size)
        # input_data_2 is already 1D, no need to reshape
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        # Reshape output to (1, 64, 64, 4)
        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

class VaeDecoder(QNNContext):
    """VAE Decoder"""

    def Inference(self, input_data):
        input_data = input_data.reshape(input_data.size)
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. Initialize All Models
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
# 4. Initialize Tokenizer and Scheduler
# ============================================
# Initialize CLIP Tokenizer
tokenizer_dir = model_dir / "tokenizer"
tokenizer = CLIPTokenizer.from_pretrained(
    "openai/clip-vit-large-patch14",
    cache_dir=str(tokenizer_dir)
)

# Initialize Scheduler
scheduler = DPMSolverMultistepScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    beta_schedule="scaled_linear"
)

# ============================================
# 5. Set Generation Parameters
# ============================================
user_prompt = "spectacular view of northern lights from Alaska"
uncond_prompt = "lowres, text, error, cropped, worst quality"
user_seed = np.int64(42)
user_step = 20
user_text_guidance = 7.5

# ============================================
# 6. Tokenize Prompts
# ============================================
def run_tokenizer(prompt, max_length=77):
    """Tokenize text"""
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
# 7. Execute Complete Generation Process
# ============================================
# Set burst mode
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Set scheduler timesteps
scheduler.set_timesteps(user_step)

# Encode text
print("Encoding text prompts...")
uncond_text_embedding = text_encoder.Inference(uncond_tokens)
user_text_embedding = text_encoder.Inference(cond_tokens)

# Initialize random latent
random_init_latent = torch.randn(
    (1, 4, 64, 64),
    generator=torch.manual_seed(user_seed)
).numpy()
latent_in = random_init_latent.transpose(0, 2, 3, 1)  # NCHW -> NHWC

# Denoising loop
print(f"Starting denoising ({user_step} steps)...")
for step in range(user_step):
    print(f'  Step {step + 1}/{user_step}')

    # Get current timestep
    time_step = np.int32(scheduler.timesteps.numpy()[step])

    # UNet inference (unconditional)
    unconditional_noise_pred = unet.Inference(
        latent_in, time_step, uncond_text_embedding
    )

    # UNet inference (conditional)
    conditional_noise_pred = unet.Inference(
        latent_in, time_step, user_text_embedding
    )

    # Merge noise predictions
    noise_pred_uncond = np.transpose(unconditional_noise_pred, (0, 3, 1, 2))
    noise_pred_text = np.transpose(conditional_noise_pred, (0, 3, 1, 2))
    latent_in_nchw = np.transpose(latent_in, (0, 3, 1, 2))

    noise_pred_uncond = torch.from_numpy(noise_pred_uncond)
    noise_pred_text = torch.from_numpy(noise_pred_text)
    latent_in_torch = torch.from_numpy(latent_in_nchw)

    # Apply guidance
    noise_pred = noise_pred_uncond + user_text_guidance * (noise_pred_text - noise_pred_uncond)

    # Scheduler step
    latent_out = scheduler.step(noise_pred, time_step, latent_in_torch).prev_sample.numpy()

    # Convert back to NHWC
    latent_in = np.transpose(latent_out, (0, 2, 3, 1))

# VAE decode
print("Decoding to image...")
output_image = vae_decoder.Inference(latent_in)

# Reset performance mode
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 8. Post-processing
# ============================================
image_size = 512
output_image = np.clip(output_image * 255.0, 0.0, 255.0).astype(np.uint8)
output_image = output_image.reshape(image_size, image_size, 3)
output_image = Image.fromarray(output_image, mode="RGB")

# Save image
output_path = execution_ws / "generated_image.png"
output_image.save(output_path)
print(f"Image saved to: {output_path}")

# ============================================
# 9. Clean Up Resources
# ============================================
del text_encoder
del unet
del vae_decoder
```

### 3.9 PerfProfile - Performance Mode Management

`PerfProfile` is used to control the performance mode of HTP (NPU).

#### Performance Mode Comparison

| Mode | Description | Power | Performance | Use Case |
| --- | --- | --- | --- | --- |
| `DEFAULT` | Default mode | Low | Medium | Does not change performance configuration |
| `HIGH_PERFORMANCE` | High performance mode | Medium | High | Sustained high-load inference |
| `BURST` | Burst mode | High | Highest | Highest performance needed for short periods |

#### Usage

```python
from qai_appbuilder import PerfProfile, QNNContext

model = QNNContext(...)

# ============================================
# Method 1: Global Setting (Recommended for Batch Inference)
# ============================================
# Set to burst mode
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Execute multiple inferences (all use burst mode)
for i in range(100):
    # Note: perf_profile parameter set to DEFAULT to use global configuration
    output = model.Inference([input_data], perf_profile=PerfProfile.DEFAULT)

# Release performance mode
PerfProfile.RelPerfProfileGlobal()

# ============================================
# Method 2: Single Inference Setting
# ============================================
# Specify performance mode for each inference
output = model.Inference([input_data], perf_profile=PerfProfile.BURST)
```

⚠️ **Important Notes**:

- After using `SetPerfProfileGlobal()`, the `perf_profile` parameter of `Inference()` should be set to `PerfProfile.DEFAULT`
- If other performance modes are specified in `Inference()`, it will override the global setting

### 3.10 Native Mode Explained (High Performance)

Native mode directly uses the model's native data types, avoiding data conversion and **significantly improving performance**.

#### Supported Data Types

- `int8` / `uint8`
- `int16` / `uint16`
- `int32` / `uint32`
- `float16` (fp16) - Most commonly used
- `float32` (fp32)
- `bool`

#### Native Mode Usage Steps

```python
from qai_appbuilder import QNNContext, DataType
import numpy as np

# 1. Specify native mode when creating model
model = QNNContext(
    model_name="my_model",
    model_path="models/my_model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 2. Query required data types for the model
input_dtypes = model.getInputDataType()
output_dtypes = model.getOutputDataType()
print(f"Input data types: {input_dtypes}")   # e.g., ['float16']
print(f"Output data types: {output_dtypes}") # e.g., ['float16']

# 3. Create data type mapping
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

# 4. Prepare data according to model requirements
input_dtype_str = input_dtypes[0].lower()
input_dtype = dtype_map.get(input_dtype_str, np.float32)

input_shapes = model.getInputShapes()
input_data = np.random.randn(*input_shapes[0]).astype(input_dtype)

# 5. Execute inference
outputs = model.Inference([input_data])

# 6. Output is also in native type
print(f"Output dtype: {outputs[0].dtype}")  # e.g., float16
```

---

## 4. C++ API Reference

### 4.1 LibAppBuilder Class

`LibAppBuilder` is the core class of the C++ API.

#### Header File Include

```cpp
#include "LibAppBuilder.hpp"
```

#### Main Methods

##### ModelInitialize - Initialize Model

```cpp
bool ModelInitialize(
    const std::string& model_name,                     // Model name
    const std::string& model_path,                     // Model file path
    const std::string& backend_lib_path,               // Backend library path
    const std::string& system_lib_path,                // System library path
    bool async = false,                                // Whether asynchronous
    const std::string& input_data_type = "float",      // Input data type
    const std::string& output_data_type = "float"      // Output data type
);
```

**Parameter Description**:

- `model_name`: Model unique identifier (e.g., "mobilenet_v2")
- `model_path`: Model file path (supports `.bin` and `.dlc`)
- `backend_lib_path`: Full path to QnnHtp.dll or QnnCpu.dll
- `system_lib_path`: Full path to QnnSystem.dll
- `async`: Whether to enable asynchronous inference
- `input_data_type`: `"float"` or `"native"`
- `output_data_type`: `"float"` or `"native"`

**Return Value**:

- `true`: Initialization successful
- `false`: Initialization failed

⚠️ **Important Note**:

- When calling C++ API, ensure proper preparation of QAIRT SDK runtime libraries during initialization, and correctly set the corresponding library file paths through backend_lib_path and system_lib_path parameters.

##### ModelInference - Execute Inference

```cpp
bool ModelInference(
    std::string model_name,                            // Model name
    std::vector<uint8_t*>& inputBuffers,               // Input buffers
    std::vector<uint8_t*>& outputBuffers,              // Output buffers (allocated by function)
    std::vector<size_t>& outputSize,                   // Output sizes
    std::string& perfProfile,                          // Performance mode
    size_t graphIndex = 0,                             // Graph index
);
```

**Parameter Description**:

- `model_name`: Model name (consistent with ModelInitialize)
- `inputBuffers`: Input data buffer list (`uint8_t*` pointers)
- `outputBuffers`: Output data buffer list (**function automatically allocates memory**)
- `outputSize`: Output data size list (in bytes)
- `perfProfile`: Performance mode string
  - `"default"`: Default mode
  - `"high_performance"`: High performance mode
  - `"burst"`: Burst mode
- `graphIndex`: Graph index (for multi-graph models)

**Return Value**:

- `true`: Inference successful
- `false`: Inference failed

⚠️ **Important Memory Management Note**:

- Memory in `outputBuffers` is automatically allocated by the function
- **Must be manually freed**: Use `free(outputBuffers[i])` to free each output buffer

##### ModelDestroy - Destroy Model

```cpp
bool ModelDestroy(std::string model_name);
```

##### Model Information Query Methods

```cpp
// Get input shapes
std::vector<std::vector<size_t>> getInputShapes(std::string model_name);

// Get output shapes
std::vector<std::vector<size_t>> getOutputShapes(std::string model_name);

// Get input data types
std::vector<std::string> getInputDataType(std::string model_name);

// Get output data types
std::vector<std::string> getOutputDataType(std::string model_name);

// Get graph name
std::string getGraphName(std::string model_name);

// Get input names
std::vector<std::string> getInputName(std::string model_name);

// Get output names
std::vector<std::string> getOutputName(std::string model_name);
```

### 4.2 Logging and Performance Functions

#### Logging Functions

```cpp
// Set log level
bool SetLogLevel(int32_t log_level, const std::string log_path = "None");

// Log output functions
void QNN_ERR(const char* fmt, ...);  // Error log
void QNN_WAR(const char* fmt, ...);  // Warning log
void QNN_INF(const char* fmt, ...);  // Info log
void QNN_VEB(const char* fmt, ...);  // Verbose log
void QNN_DBG(const char* fmt, ...);  // Debug log
```

**Log Level Constants**:

```cpp
#define QNN_LOG_LEVEL_ERROR   1
#define QNN_LOG_LEVEL_WARN    2
#define QNN_LOG_LEVEL_INFO    3
#define QNN_LOG_LEVEL_VERBOSE 4
#define QNN_LOG_LEVEL_DEBUG   5
```

#### Performance Configuration Functions

```cpp
// Set global performance mode
bool SetPerfProfileGlobal(const std::string& perf_profile);

// Release global performance mode
bool RelPerfProfileGlobal();

// Set profiling level
bool SetProfilingLevel(int32_t profiling_level);
```

**Profiling Levels**:

```cpp
#define QNN_PROFILING_LEVEL_OFF      0
#define QNN_PROFILING_LEVEL_BASIC    1
#define QNN_PROFILING_LEVEL_DETAILED 2
```

#### TimerHelper - Timing Utility Class

```cpp
class TimerHelper {
public:
    TimerHelper();                                     // Constructor (automatically starts timing)
    void Reset();                                      // Reset timer
    void Print(std::string message);                   // Print elapsed time
    void Print(std::string message, bool reset);       // Print and optionally reset
};
```

### 4.3 Complete C++ Examples

#### Example 1: Image Super-Resolution (Real-ESRGAN)

Based on real example code: `samples/C++/real_esrgan_x4plus/real_esrgan_x4plus.cpp`

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
// Helper Function: Convert OpenCV Mat to xtensor
// ============================================
xt::xarray<float> ConvertTensor(cv::Mat &img, int scale) {
    int b = 1;
    int ch = img.channels();
    int hh = img.rows;
    int hw = img.cols;
    int out_channel = ch * (scale * scale);
    int h = hh / scale;
    int w = hw / scale;

    // Input img is in HWC format
    size_t size = img.total();
    size_t channels = img.channels();
    std::vector<int> shape = {img.rows, img.cols, img.channels()};

    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, h, w, out_channel};

    // Convert to xarray
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
    // 1. Set Paths
    // ============================================
    fs::path execution_ws = fs::current_path();
    fs::path backend_lib_path = execution_ws / "QnnHtp.dll";
    fs::path system_lib_path = execution_ws / "QnnSystem.dll";
    fs::path model_path = execution_ws / (MODEL_NAME + ".bin");
    fs::path input_path = execution_ws / "input.jpg";
    fs::path output_path = execution_ws / "output.jpg";

    // ============================================
    // 2. Initialize Logging and Profiling
    // ============================================
    SetLogLevel(QNN_LOG_LEVEL_WARN);
    SetProfilingLevel(QNN_PROFILING_LEVEL_BASIC);

    // ============================================
    // 3. Create LibAppBuilder Instance and Initialize Model
    // ============================================
    LibAppBuilder libAppBuilder;

    std::cout << "Initializing model..." << std::endl;
    int ret = libAppBuilder.ModelInitialize(
        MODEL_NAME,
        model_path.string(),
        backend_lib_path.string(),
        system_lib_path.string()
    );

    if (ret < 0) {
        std::cout << "Model loading failed" << std::endl;
        return -1;
    }
    std::cout << "Model initialization complete" << std::endl;

    // ============================================
    // 4. Read and Preprocess Image
    // ============================================
    cv::Mat orig_image = cv::imread(input_path.string(), cv::IMREAD_COLOR);
    if (orig_image.empty()) {
        QNN_ERR("Cannot read image: %s", input_path.string().c_str());
        return -1;
    }

    // Convert to RGB
    cv::Mat rgb_image;
    cv::cvtColor(orig_image, rgb_image, cv::COLOR_BGR2RGB);

    // Resize
    cv::Mat resized_image;
    cv::resize(rgb_image, resized_image, cv::Size(IMAGE_SIZE, IMAGE_SIZE));

    // Normalize to [0, 1]
    cv::Mat input_mat;
    resized_image.convertTo(input_mat, CV_32FC3, 1.0 / 255.0);

    // Convert to model input format
    xt::xarray<float> input_tensor = ConvertTensor(input_mat, 1);

    // Allocate input buffer
    uint32_t size = RGB_IMAGE_SIZE_F32(IMAGE_SIZE, IMAGE_SIZE);
    float* input_buffer = new float[size / 4];
    std::copy(input_tensor.begin(), input_tensor.end(), input_buffer);

    // ============================================
    // 5. Execute Inference
    // ============================================
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    std::string perfProfile = "burst";

    inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_buffer));

    // Set global performance mode
    SetPerfProfileGlobal("burst");

    std::cout << "Executing inference..." << std::endl;
    TimerHelper timer;

    ret = libAppBuilder.ModelInference(
        MODEL_NAME,
        inputBuffers,
        outputBuffers,
        outputSize,
        perfProfile
    );

    timer.Print("Inference time: ", false);

    // Release performance mode
    RelPerfProfileGlobal();

    if (ret < 0) {
        std::cout << "Inference failed" << std::endl;
        delete[] input_buffer;
        return -1;
    }
    std::cout << "Inference complete" << std::endl;

    // ============================================
    // 6. Post-processing
    // ============================================
    float* output_data = reinterpret_cast<float*>(outputBuffers[0]);

    int output_width = IMAGE_SIZE * SCALE;
    int output_height = IMAGE_SIZE * SCALE;
    int output_channels = 3;
    int output_tensor_size = output_width * output_height * output_channels;

    // Denormalize and convert to uint8
    char* buffer = new char[output_tensor_size];
    for (int i = 0; i < output_tensor_size; i++) {
        float val = output_data[i];
        buffer[i] = std::max(0.0, std::min(255.0, val * 255.0));
    }

    // Create output image
    cv::Mat output_mat(output_height, output_width, CV_8UC3, buffer);
    cv::Mat output_mat_bgr;
    cv::cvtColor(output_mat, output_mat_bgr, cv::COLOR_RGB2BGR);

    // Save image
    cv::imwrite(output_path.string(), output_mat_bgr);
    std::cout << "Output image saved to: " << output_path.string() << std::endl;

    // Display image (optional)
    cv::imshow("Output Image", output_mat_bgr);
    cv::waitKey(0);

    // ============================================
    // 7. Clean Up Resources
    // ============================================
    delete[] input_buffer;
    delete[] buffer;

    // Free output buffers (important!)
    for (size_t j = 0; j < outputBuffers.size(); j++) {
        free(outputBuffers[j]);
    }
    outputBuffers.clear();
    outputSize.clear();

    // Destroy model
    libAppBuilder.ModelDestroy(MODEL_NAME);

    return 0;
}
```

#### Example 2: Image Classification (BEiT)

Based on real example code: `samples/C++/beit/beit.cpp`

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
// Softmax Function
// ============================================
xt::xarray<float> softmax(const xt::xarray<float>& x, std::size_t dim) {
    xt::xarray<float> exp_x = xt::exp(x);
    xt::xarray<float> sum_exp = xt::sum(exp_x, {dim}, xt::keep_dims);
    return exp_x / sum_exp;
}

// ============================================
// Convert OpenCV Mat to xtensor (NCHW format)
// ============================================
xt::xarray<float> ConvertTensor(cv::Mat &img, int scale) {
    int b = 1;
    int ch = img.channels();
    int hh = img.rows;
    int hw = img.cols;
    int out_channel = ch * (scale * scale);
    int h = hh / scale;
    int w = hw / scale;

    // Input img is in HWC format
    size_t size = img.total();
    size_t channels = img.channels();
    std::vector<int> shape = {img.cols, img.rows, img.channels()};

    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, out_channel, h, w};

    // Convert to xarray
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
// Preprocess Image (ImageNet Standard)
// ============================================
xt::xarray<float> preprocess_image(const cv::Mat& image) {
    cv::Mat rgb_image;
    int scale = 1;

    // Convert to RGB
    if (image.channels() == 3) {
        cv::cvtColor(image, rgb_image, cv::COLOR_BGR2RGB);
    } else {
        cv::cvtColor(image, rgb_image, cv::COLOR_GRAY2RGB);
    }

    // 1. Resize to 256x256
    cv::Mat resized_image;
    cv::resize(rgb_image, resized_image, cv::Size(256, 256), 0, 0, cv::INTER_LINEAR);

    // 2. Center crop to 224x224
    int crop_x = (256 - IMAGENET_DIM) / 2;
    int crop_y = (256 - IMAGENET_DIM) / 2;
    cv::Rect roi(crop_x, crop_y, IMAGENET_DIM, IMAGENET_DIM);
    cv::Mat cropped_image = resized_image(roi).clone();

    // 3. Normalize to [0, 1]
    cv::Mat input_mat;
    cropped_image.convertTo(input_mat, CV_32FC3, 1.0 / 255.0);

    // 4. Convert to NCHW format
    xt::xarray<float> input_tensor = ConvertTensor(input_mat, scale);

    return input_tensor;
}

// ============================================
// BEiT Classifier Class
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
        std::cout << "Loading model..." << std::endl;
        int ret = libAppBuilder.ModelInitialize(
            model_name,
            model_path,
            backend_lib,
            system_lib,
            false  // async
        );
        std::cout << "Model loading complete" << std::endl;
        return ret;
    }

    int DestroyModel() {
        std::cout << "Destroying model..." << std::endl;
        int ret = libAppBuilder.ModelDestroy(model_name);
        return ret;
    }

    xt::xarray<float> predict(const cv::Mat& image) {
        std::cout << "Predicting..." << std::endl;

        int size = 3 * IMAGENET_DIM * IMAGENET_DIM;
        std::unique_ptr<float[]> input_buffer(new float[size]);

        // Preprocess image
        xt::xarray<float> input_tensor = preprocess_image(image);
        std::copy(input_tensor.begin(), input_tensor.end(), input_buffer.get());

        // Prepare input/output buffers
        std::vector<uint8_t*> inputBuffers;
        std::vector<uint8_t*> outputBuffers;
        std::vector<size_t> outputSize;
        std::string perfProfile = "burst";
        int graphIndex = 0;

        inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_buffer.get()));

        // Execute inference
        std::cout << "Executing inference..." << std::endl;
        int ret = libAppBuilder.ModelInference(
            model_name,
            inputBuffers,
            outputBuffers,
            outputSize,
            perfProfile,
            graphIndex
        );
        std::cout << "Inference complete" << std::endl;

        if (ret < 0) {
            std::cout << "Inference failed" << std::endl;
            return xt::zeros<float>({1000});
        }

        // Process output
        float* pred_output = reinterpret_cast<float*>(outputBuffers[0]);
        size_t output_elements = 1000;  // ImageNet class count

        xt::xarray<float> output = xt::zeros<float>({output_elements});
        std::copy(pred_output, pred_output + output_elements, output.begin());

        // Free output buffers
        for (auto buffer : outputBuffers) {
            free(buffer);
        }

        // Apply softmax
        return softmax(output, 0);
    }
};

// ============================================
// Load ImageNet Labels
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
// Main Function
// ============================================
int main() {
    // Set paths
    std::string image_path = "../input.jpg";
    std::string json_path = "../models/imagenet_labels.json";
    std::string model_path = "../models/beit.bin";
    std::string backend_lib = "../qai_libs/QnnHtp.dll";
    std::string system_lib = "../qai_libs/QnnSystem.dll";

    // Read image
    cv::Mat image = cv::imread(image_path);
    if (image.empty()) {
        std::cout << "Cannot read image" << std::endl;
        return -1;
    }

    // Create classifier
    BEIT beit(model_path, backend_lib, system_lib);

    // Set log level
    SetLogLevel(QNN_LOG_LEVEL_WARN);

    // Load model
    int ret = beit.LoadModel();
    if (ret < 0) {
        std::cout << "Model loading failed" << std::endl;
        return -1;
    }

    // Execute prediction
    xt::xarray<float> probabilities = beit.predict(image);
    std::cout << "Prediction complete, probability array size: " << probabilities.size() << std::endl;

    // Find Top-5 predictions
    std::vector<std::pair<float, int>> indexed_probs;
    for (size_t i = 0; i < probabilities.size(); ++i) {
        indexed_probs.emplace_back(probabilities[i], static_cast<int>(i));
    }
    std::sort(indexed_probs.begin(), indexed_probs.end(),
              std::greater<std::pair<float, int>>());

    // Load labels
    std::vector<std::string> labels = load_labels(json_path);

    // Print Top-5 results
    std::cout << "\nTop 5 Prediction Results:" << std::endl;
    for (int i = 0; i < 5; ++i) {
        int class_idx = indexed_probs[i].second;
        float prob = indexed_probs[i].first;
        std::string label = (class_idx < labels.size()) ? labels[class_idx] : "Unknown";
        std::cout << (i + 1) << ". " << label << ": " 
                  << (100 * prob) << "%" << std::endl;
    }

    // Destroy model
    ret = beit.DestroyModel();
    if (ret < 0) {
        std::cout << "Model destruction failed" << std::endl;
        return -1;
    }

    return 0;
}
```

## 5. Advanced Features

### 5.1 LoRA Adapter Support

LoRA (Low-Rank Adaptation) allows dynamic loading and switching of model adapters.

#### Python Example

```python
from qai_appbuilder import QNNLoraContext, LoraAdapter, QNNConfig, Runtime, LogLevel

# Configure environment
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# ============================================
# Create LoRA Adapters
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
# Initialize Model with LoRA
# ============================================
model = QNNLoraContext(
    model_name="llm_with_lora",
    model_path="models/base_llm.bin",
    lora_adapters=[adapter1]  # Initially load adapter1
)

# Execute inference
output1 = model.Inference([input_data])
print(f"Output using adapter1: {output1[0].shape}")

# ============================================
# Dynamically Switch LoRA Adapter
# ============================================
model.apply_binary_update([adapter2])

output2 = model.Inference([input_data])
print(f"Output using adapter2: {output2[0].shape}")

# Clean up
del model
```

### 5.2 Multi-Graph Model Support

Some models contain multiple computation graphs, which can be selected via the `graphIndex` parameter.

```python
from qai_appbuilder import QNNContext

model = QNNContext(
    model_name="multi_graph_model",
    model_path="models/multi_graph_model.bin"
)

# Use first graph
output1 = model.Inference([input_data], graphIndex=0)

# Use second graph
output2 = model.Inference([input_data], graphIndex=1)

del model
```

### 5.3 Supported Model Formats

QAI AppBuilder supports multiple model formats for different runtimes and scenarios.

#### 5.3.1 Supported Model Format List

| Format | Runtime | Description |
| --- | --- | --- |
| `.bin` | HTP/CPU | Precompiled binary format, fastest loading (recommended) |
| `.dlc` | HTP/CPU | Direct loading supported from QAIRT 2.41.0 |
| `.so` | CPU | Shared library format, runs only on CPU |

#### 5.3.2 Direct DLC Model Loading

Starting from QAIRT 2.41.0, direct loading of `.dlc` model files is supported.

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel

QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# ============================================
# Directly Load .dlc File
# ============================================
model = QNNContext(
    model_name="my_model",
    model_path="models/my_model.dlc"  # Use .dlc file
)

# On first run, my_model.dlc.bin file will be automatically generated
# Subsequent runs will directly load .dlc.bin for faster speed

output = model.Inference([input_data])

del model
```

**Notes**:

- First loading of `.dlc` file will automatically convert to `.dlc.bin`
- Converted `.dlc.bin` file will be saved in the same directory
- Subsequent runs will directly load `.dlc.bin` file for faster speed
- To reconvert, delete the `.dlc.bin` file

#### 5.3.3 SO Model Format (CPU Runtime)

For models that need to run on CPU, you can use `.so` format:

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel

# Configure for CPU runtime
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.CPU,  # Use CPU runtime
    log_level=LogLevel.INFO
)

# Load .so model file
model = QNNContext(
    model_name="cpu_model",
    model_path="models/my_model.so"  # Use .so file
)

output = model.Inference([input_data])

del model
```

---

## 6. Performance Optimization

### 6.1 Use Native Mode (Recommended)

**Performance Improvement**: Native mode can reduce 10%-200% data conversion overhead.

```python
# ❌ Float mode (default) - has conversion overhead
model_float = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.FLOAT,
    output_data_type=DataType.FLOAT
)

# ✅ Native mode - no conversion overhead, better performance
model_native = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)
```

### 6.2 Use Burst Performance Mode

Use Burst mode in scenarios requiring highest performance.

```python
from qai_appbuilder import PerfProfile

# ✅ Global setting (for batch inference)
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

for i in range(100):
    output = model.Inference([input_data], perf_profile=PerfProfile.DEFAULT)

PerfProfile.RelPerfProfileGlobal()
```

### 6.3 Batch Inference Optimization

```python
# ❌ Not recommended: Initialize model each time
for data in dataset:
    model = QNNContext(...)
    output = model.Inference([data])
    del model

# ✅ Recommended: Initialize only once
model = QNNContext(...)

PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
for data in dataset:
    output = model.Inference([data])
PerfProfile.RelPerfProfileGlobal()

del model
```

### 6.4 Use ARM64 Python (Windows)

On Windows on Snapdragon platform, ARM64 Python performs better than x64 Python.

```bash
# Download ARM64 Python
https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe

# Install ARM64 version of QAI AppBuilder
pip install qai_appbuilder-{version}-cp312-cp312-win_arm64.whl
```

## 7. FAQ

### 7.1 Model Loading Failure

**Problem**: `ModelInitialize` returns `False` or fails

**Solution**:

```python
from qai_appbuilder import QNNConfig, LogLevel, Runtime
from pathlib import Path

# 1. Enable verbose logging
QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.DEBUG,  # Use DEBUG level
    log_path="./debug.log"     # Output to file
)

# 2. Check if file exists
model_path = Path("models/my_model.bin")
if not model_path.exists():
    print(f"Error: Model file does not exist: {model_path}")
    exit()
```

### 7.2 Incorrect Inference Results

**Problem**: Inference output does not match expectations

**Solution**:

```python
# 1. Verify model information
print(f"Input shapes: {model.getInputShapes()}")
print(f"Output shapes: {model.getOutputShapes()}")
print(f"Input data types: {model.getInputDataType()}")
print(f"Output data types: {model.getOutputDataType()}")

# 2. Verify input data
expected_shape = model.getInputShapes()[0]
print(f"Expected shape: {expected_shape}")
print(f"Actual shape: {input_data.shape}")
print(f"Actual data type: {input_data.dtype}")

# 3. Check data range
print(f"Input data range: [{input_data.min()}, {input_data.max()}]")

# 4. Check output
output = model.Inference([input_data])
print(f"Output shape: {output[0].shape}")
print(f"Output data type: {output[0].dtype}")
print(f"Output data range: [{output[0].min()}, {output[0].max()}]")
```

### 7.3 Memory Leaks

**Python Solution**:

```python
import gc

# Explicitly delete model
model = QNNContext(...)
# ... use model ...
del model
gc.collect()  # Force garbage collection
```

**C++ Solution**:

```cpp
// Must manually free output buffers
for (auto buffer : outputBuffers) {
    free(buffer);  // Free each output buffer
}
outputBuffers.clear();
outputSize.clear();

// Destroy model
appBuilder.ModelDestroy(model_name);
```

### 7.4 Native Mode Data Type Mismatch

**Problem**: Data type error in Native mode causes inference failure

**Solution**:

```python
from qai_appbuilder import QNNContext, DataType
import numpy as np

# 1. Initialize using Native mode
model = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 2. Query required data types for the model
input_dtypes = model.getInputDataType()
input_shapes = model.getInputShapes()

print(f"Model required input data types: {input_dtypes}")
print(f"Model required input shapes: {input_shapes}")

# 3. Create data type mapping
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

# 4. Prepare data according to model requirements
input_dtype_str = input_dtypes[0].lower()
input_dtype = dtype_map.get(input_dtype_str, np.float32)

print(f"Using data type: {input_dtype}")

# 5. Create input data with correct type
input_data = np.random.randn(*input_shapes[0]).astype(input_dtype)

print(f"Input data type: {input_data.dtype}")
print(f"Input data shape: {input_data.shape}")

# 6. Execute inference
output = model.Inference([input_data])

print(f"Output data type: {output[0].dtype}")
print(f"Output data shape: {output[0].shape}")
```

### 7.5 C++ Linking Errors

**Problem**: LNK2038, LNK2001, or other linking errors

**Solution**:

Ensure Visual Studio project configuration is correct:

1. **Runtime Library** (Most common issue)
   
   - Project Properties → C/C++ → Code Generation → Runtime Library
   - Must be set to: **Multi-threaded DLL (/MD)**

2. **Platform**
   
   - Project Properties → General → Platform
   - Set to: **ARM64** (for WoS platform)

3. **Configuration**
   
   - Use **Release** configuration (not Debug)

4. **C++ Standard**
   
   - Project Properties → C/C++ → Language → C++ Language Standard
   - Set to: **ISO C++17** or higher

5. **Character Set**
   
   - Project Properties → Advanced → Character Set
   - Set to: **Use Unicode Character Set**

### 7.6 Poor Performance

**Problem**: Inference speed slower than expected

**Diagnosis and Solution**:

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel, DataType, PerfProfile
import time

# 1. Ensure using HTP (NPU) not CPU
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,  # Ensure it's HTP
    log_level=LogLevel.INFO,
    profiling_level=ProfilingLevel.BASIC  # Enable performance profiling
)

# 2. Use Native mode
model = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 3. Use Burst mode
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 4. Test performance
start_time = time.time()
for _ in range(100):
    output = model.Inference([input_data])
end_time = time.time()

avg_time = (end_time - start_time) / 100
print(f"Average inference time: {avg_time * 1000:.2f} ms")

PerfProfile.RelPerfProfileGlobal()
```

---

## 8. Reference Resources

### 8.1 Official Documentation and Resources

- **GitHub Repository**: https://github.com/quic/ai-engine-direct-helper
- **Qualcomm AI Hub**: https://aihub.qualcomm.com/
- **AI Dev Home**: https://www.aidevhome.com/
- **Qualcomm® AI Runtime SDK**: https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

### 8.2 Tutorials and Blogs

- [QAI_AppBuilder: Making Local AI Deployment Accessible!](https://docs.qualcomm.com/bundle/publicresource/80-94755-1_REV_AA_QAI_AppBuilder_-_WoS.pdf)
- [Large Language Model Series (1): Get Started in 3 Minutes, Deploy DeepSeek on Snapdragon AI PC!](https://blog.csdn.net/csdnsqst0050/article/details/149425691)
- [Large Language Model Series (2): Configuration and Deployment of Local OpenAI Compatible API Service](https://blog.csdn.net/csdnsqst0050/article/details/150208814)
- [Qualcomm Platform Large Language Model Selection](https://www.aidevhome.com/?id=51)
- [QAI AppBuilder on Linux (QCS6490)](https://docs.radxa.com/en/dragon/q6a/app-dev/npu-dev/qai-appbuilder)

### 8.3 Sample Code

- **Python Examples**: https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python
  
  - Real-ESRGAN (Image Super-Resolution)
  - YOLOv8 (Object Detection)
  - Whisper (Speech Recognition)
  - Stable Diffusion (Text-to-Image)
  - BEiT (Image Classification)
  - OpenPose (Pose Estimation)
  - Depth Anything (Depth Estimation)
  - 20+ examples

- **C++ Examples**: https://github.com/quic/ai-engine-direct-helper/tree/main/samples/c++
  
  - Real-ESRGAN
  - BEiT (Image Classification)

- **WebUI Applications**: https://github.com/quic/ai-engine-direct-helper/tree/main/samples/webui
  
  - ImageRepairApp (Image Repair)
  - StableDiffusionApp (Text-to-Image)
  - GenieWebUI (LLM Conversation)

### 8.4 Model Resources

- **AI Hub Model Library**: https://aihub.qualcomm.com/compute/models
- **AI Dev Home Model Library**: https://www.aidevhome.com/data/models/
- **Qwen2 7B SSD**: https://www.aidevhome.com/data/adh2/models/8380/qwen2_7b_ssd_250702.html
- **DeepSeek-R1-Distill-Qwen-7B**: https://aiot.aidlux.com/zh/models/detail/78

---

## 9. Quick Start Guide

### 9.1 First Python Program

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel
import numpy as np

# 1. Configure environment (required)
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# 2. Load model
model = QNNContext(
    model_name="my_first_model",
    model_path="models/my_model.bin"
)

# 3. Prepare input
input_shape = model.getInputShapes()[0]
input_data = np.random.randn(*input_shape).astype(np.float32)

# 4. Execute inference
output = model.Inference([input_data])

# 5. View results
print(f"Output shape: {output[0].shape}")
print(f"Output data type: {output[0].dtype}")

# 6. Clean up
del model
```

### 9.2 First C++ Program

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>

int main() {
    // 1. Set logging
    SetLogLevel(QNN_LOG_LEVEL_INFO);

    // 2. Create AppBuilder
    LibAppBuilder appBuilder;

    // 3. Initialize model
    bool success = appBuilder.ModelInitialize(
        "my_first_model",
        "models/my_model.bin",
        "qai_libs/QnnHtp.dll",
        "qai_libs/QnnSystem.dll"
    );

    if (!success) {
        QNN_ERR("Model initialization failed");
        return -1;
    }

    // 4. Prepare input
    auto inputShapes = appBuilder.getInputShapes("my_first_model");
    size_t input_size = 1;
    for (auto dim : inputShapes[0]) {
        input_size *= dim;
    }

    float* input_data = new float[input_size];
    // ... fill input data ...

    std::vector<uint8_t*> inputBuffers;
    inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_data));

    // 5. Execute inference
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
        QNN_ERR("Inference failed");
        delete[] input_data;
        return -1;
    }

    // 6. Process output
    float* output_data = reinterpret_cast<float*>(outputBuffers[0]);
    // ... process output ...

    // 7. Clean up
    delete[] input_data;
    for (auto buffer : outputBuffers) {
        free(buffer);
    }
    appBuilder.ModelDestroy("my_first_model");

    return 0;
}
```

---

## 10. Version History

### v2.0.0 (January 2025 - Major Update)

**Major New Features**:

- ✅ **Simplified Deployment**: Python extension includes all necessary dependency libraries (including QAIRT SDK runtime)
- ✅ **Multimodal Support**: Support for multimodal models (Qwen2.5-3B-VL / Qwen2.5-3B-omini)
- ✅ **DLC Support**: Support for direct loading of `.dlc` model files (QAIRT 2.41.0+)
- ✅ **LLM Optimization**: New `GenieContext` class optimized for large language models
- ✅ **Performance Improvement**: Improved Native mode, reduced data conversion overhead
- ✅ **Enhanced Profiling**: Improved performance profiling functionality with more detailed performance data

**API Changes**:

- `QNNConfig.Config()` `qnn_lib_path` parameter now optional (uses built-in libraries by default)
- `QNNContext` `backend_lib_path` and `system_lib_path` parameters now optional
- New `GenieContext` class for LLM inference

**Known Issues**:

- Some ARM64 Python extensions may require manual compilation
- Some models on Linux platform may require setting `ADSP_LIBRARY_PATH`

### v1.x (Historical Versions)

**v1.5.0** (December 2024):
- Added LoRA adapter support
- Improved multi-graph model support
- Optimized memory management

**v1.0.0** (October 2024):
- First official release
- Support for Python and C++ APIs
- Support for Windows and Linux platforms
- Support for HTP and CPU runtimes

---

## 11. License

QAI AppBuilder is licensed under **BSD 3-Clause "New" or "Revised" License**.

See: https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE

---

## 12. Disclaimer

This software is provided "as is" without any express or implied warranties. The authors and contributors are not liable for any damages arising from the use of this software. The code may be incomplete or insufficiently tested. Users must evaluate its suitability and assume all related risks.

**Note**: Contributions are welcome. Ensure thorough testing before deploying in critical systems.

---

## 13. Contribution and Support

### Report Issues

If you encounter problems, please visit:

- **GitHub Issues**: https://github.com/quic/ai-engine-direct-helper/issues
- **GitHub Discussions**: https://github.com/quic/ai-engine-direct-helper/discussions

### Contribute Code

Pull Requests are welcome! Please refer to:

- **Contributing Guide**: https://github.com/quic/ai-engine-direct-helper/blob/main/CONTRIBUTING.md
- **Code of Conduct**: https://github.com/quic/ai-engine-direct-helper/blob/main/CODE-OF-CONDUCT.md

---

<div align="center">
  <p>⭐ If this project helps you, please give us a Star!</p>
  <p>📧 Questions or suggestions? Visit <a href="https://github.com/quic/ai-engine-direct-helper">GitHub Repository</a></p>
</div>

---

**Documentation Version**: 2.1  
**Last Updated**: January 26, 2025  
**Applicable to**: QAI AppBuilder v2.0.0+
