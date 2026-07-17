## Service

该文件夹下是该服务的 C++ 实现，可编译到 Windows、Android 和 Linux 目标平台。

构建完成后，参见 [USAGE](USAGE.zh.MD) 了解如何使用。

### 获取源码

克隆整个仓库及其第三方依赖：

```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```

## 面向 Windows 构建

### 准备环境

编译前先安装：

- Qualcomm® AI Runtime SDK
- CMake
- Visual Studio Build Tools 2022（clang, v143）
- Ninja

用命令提示符（Command Prompt）而不是 PowerShell 来编译。

### 设置 QAIRT SDK 版本

安装 Qualcomm® AI Runtime SDK 后，它通常位于 `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`
（使用你实际安装的版本——这里只是示例）。将其导出为环境变量：

`Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`

**`QNN_SDK_ROOT` 必须在配置 CMake 之前设置**，因为构建过程会在配置阶段读取它。

### 构建 GenieAPIService 与 GenieAPIClient

GenieServices 可以将 BIN/MNN/GGUF 格式的 AI 模型作为能力来访问。
**Windows/MSVC 是唯一同时支持多种后端的平台**——在 CMake 配置命令末尾添加
`-DOption=ON` 即可选择。Linux 和 Android 构建固定为仅支持 QNN/Genie，
不接受这些选项（参见 [BUILD_LINUX.zh.md](BUILD_LINUX.zh.md) 和
[BUILD_ANDROID_README.zh.md](BUILD_ANDROID_README.zh.md)）。

| 选项            | 作用                                                                    | 默认值 |
|----------------|:---------------------------------------------------------------------|---------|
| `USE_MNN`      | 支持 mnn 格式模型（CPU 后端）                                            | OFF     |
| `USE_GGUF`     | 支持 gguf 格式模型（通过 llama.cpp 使用 GPU 后端，失败时回退到 CPU）           | OFF     |
| `BUILD_AS_DLL` | 只构建 `GenieAPILibrary.dll` 目标。默认 OFF 会构建完整产物集（exe + dll + 全部示例：`GenieAPIClient`、`SampleApp`、`examples/tools`）。 | OFF |

```
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 -DUSE_MNN=ON -DUSE_GGUF=ON
cmake --build . --config Release --parallel 4
```

之后完整的发布产物位于 `Service\GenieService-win-arm64\`（固定名称，不带版本号；
由 `Service/CMakeLists.txt` 中的 `BUILD_PATH` 设定）。

## 面向 Android 构建

一个自动化脚本会处理全部依赖并生成可直接安装的 APK。

**详细说明参见 [Android 构建指南](BUILD_ANDROID_README.zh.md)。**

`build_android.bat` 会：
- 构建 `libappbuilder.so` 及其依赖
- 构建 `GenieAPIService`
- 复制所需的 QNN SDK 库
- 生成包含全部库文件的已签名 Android APK

```cmd
cd qai-appbuilder\samples\genie\c++\Service
build_android.bat
```

手动配置与故障排查参见 [BUILD_ANDROID_README.zh.md](BUILD_ANDROID_README.zh.md)。

## 面向 Linux（ARM64）构建

在基于 Qualcomm 的 ARM64 Linux 设备上构建该服务，使用的工具链和 SDK
与 QAI AppBuilder 的 Linux 构建完全相同。如果你的机器已经能构建
`qai_appbuilder` Python wheel，就能直接构建 GenieAPIService，无需任何额外配置。

**一行命令构建：**

```bash
cd samples/genie/c++
export QNN_SDK_ROOT=/absolute/path/to/QAIRT_SDK
chmod +x build_linux.sh
./build_linux.sh
```

输出会放在 `samples/genie/c++/Service/build-linux/GenieService-linux-arm64/`
（固定名称，不带版本号；Linux 构建固定为仅支持 QNN/Genie，`USE_MNN`/`USE_GGUF`
在这里不可用——详情参见 [BUILD_LINUX.zh.md](BUILD_LINUX.zh.md)）。

有关全部选项、运行时设置以及故障排查，请参见
[BUILD_LINUX.zh.md](BUILD_LINUX.zh.md)。
