# 源代码

## Service:

该文件夹下的代码是该服务的 C++ 实现。它可以被编译到 Windows、Android 和 Linux
目标平台。

完成构建任务后，请前往 [USAGE](USAGE.zh.MD) 了解如何使用它。


### 准备仓库

使用下面的命令克隆整个仓库以及依赖的第三方库。

```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```

## 面向 Windows 构建：

### 准备环境：<br>

在编译该服务之前，请安装以下内容。<br>

- Qualcomm® AI Runtime SDK
- CMake
- Visual Studio Build Toos 2022(clang, v143)
- Ninja

打开一个 “Command Prompt” 窗口（不是 PowerShell）来编译这些库。

### 设置 QAIRT SDK 版本：<br>

安装 Qualcomm® AI Runtime SDK 之后，它通常位于 `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`
（请使用你实际安装的版本——这里只是一个示例）。我们可以把它设置为一个
环境变量。

`Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`

**`QNN_SDK_ROOT` 必须在配置 CMake 之前设置**，因为构建过程会在配置阶段读取它。

### 构建 GenieAPIServer 与 GenieAPIClient：<br>

GenieServices 可以将 BIN/MNN/GGUF 格式的 AI 模型作为能力来访问。
**Windows/MSVC 是唯一同时支持多种后端的平台**——你可以在 CMake 配置命令末尾添加
`-DOption=ON` 来选择它。Linux 和 Android 构建固定为仅支持 QNN/Genie，
不接受这些选项（参见 [BUILD_LINUX.zh.md](BUILD_LINUX.zh.md) 和
[BUILD_ANDROID_README.zh.md](BUILD_ANDROID_README.zh.md)）。

| Option         | Function                                                            | Default |
|----------------|:---------------------------------------------------------------------|---------|
| `USE_MNN`      | Support mnn format model (CPU backend)                             | OFF     |
| `USE_GGUF`     | Support gguf format model (GPU backend via llama.cpp, falls back to CPU) | OFF     |
| `BUILD_AS_DLL` | Build only the `GenieAPILibrary.dll` target. Default OFF builds the full set (exe + dll + all examples: `GenieAPIClient`, `SampleApp`, `examples/tools`). | OFF |

```
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 -DUSE_MNN=ON -DUSE_GGUF=ON
cmake --build . --config Release --parallel 4
```

之后完整的发布产物会位于 `Service\GenieService_v<VERSION>`（当前为 `Service\GenieService_v2.3.7`，
权威版本号请参见 `scripts/version.cmake`）。

## 面向 Android 构建： <br>

对于 Android 构建，我们提供了一个自动化构建脚本，可以处理所有依赖项并生成一个可直接安装的 APK。

**详细说明请参见 [Android 构建指南](BUILD_ANDROID_README.zh.md)。**

自动化构建脚本（`build_android.bat`）会：
- 构建 libappbuilder.so 及其所有依赖项
- 构建 GenieAPIService
- 复制所有所需的 QNN SDK 库
- 生成一个包含全部库文件的已签名 Android APK

只需运行：
```cmd
cd qai-appbuilder\samples\genie\c++\Service
build_android.bat
```

有关手动配置与故障排查，请参见 [BUILD_ANDROID_README.zh.md](BUILD_ANDROID_README.zh.md)。

## 面向 Linux (ARM64) 构建：

在基于 Qualcomm 的 ARM64 Linux 设备上构建该服务，使用的**工具链和 SDK**
与 QAI AppBuilder 的 Linux 构建**完全相同**。如果你的机器已经能够构建
`qai_appbuilder` Python wheel，那么它可以在不需要任何额外配置的情况下构建
GenieAPIService。

**一行命令构建：**

```bash
cd samples/genie/c++
export QNN_SDK_ROOT=/absolute/path/to/QAIRT_SDK
chmod +x build_linux.sh
./build_linux.sh
```

输出会放在 `samples/genie/c++/Service/build-linux/GenieService_v<VERSION>_qnn<SDK_DIR_NAME>/`
（Linux 构建固定为仅支持 QNN/Genie；`USE_MNN`/`USE_GGUF` 在这里不可用——详情参见
[BUILD_LINUX.zh.md](BUILD_LINUX.zh.md)）。

有关全部选项、运行时设置以及故障排查，请参见
[BUILD_LINUX.zh.md](BUILD_LINUX.zh.md)。
