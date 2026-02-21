# GenieAPIService 从源码构建

> 适用于需要自行编译 C++ Service / Client（Windows / Android / Linux）的开发者。若仅体验或集成 API，建议直接使用 [Release 包](https://github.com/quic/ai-engine-direct-helper/releases)。

## 源码与依赖准备

### 1. 克隆仓库（含子模块）

```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

### 2. VLM 额外依赖（stb）

视觉语言模型（VLM）依赖 `stb`，请在以下目录拉取：

```bash
cd samples\genie\c++\External
git clone https://github.com/nothings/stb.git
```

---

## Windows 构建

### 环境准备

- Qualcomm® AI Runtime SDK（QAIRT）
- CMake
- Visual Studio Build Tools 2022（clang、v143）
- Ninja

> 建议使用 **Command Prompt（CMD）** 而非 PowerShell 执行构建命令。

### 设置 QAIRT SDK 路径

安装 QAIRT 后通常位于 `C:\Qualcomm\AIStack\QAIRT\2.42.0.251225`，可设置环境变量：

```bat
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225
```

### 构建命令（ARM64 示例）

```bat
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 ..
cmake --build . --config Release --parallel 4
```

构建完成后，发布目录通常位于：`Service\GenieSerivce_v2.1.4`。

### 可选编译开关（模型格式支持）

GenieService 默认支持 Qualcomm QNN BIN 模型；如需额外支持：

- `USE_MNN=ON`：支持 MNN 格式模型
- `USE_GGUF=ON`：支持 GGUF（llama.cpp）格式模型

在 CMake 命令末尾追加 `-D<OPTION>=ON` 启用，例如：

```bat
cmake -S .. -B . -A ARM64 -DUSE_GGUF=ON -DUSE_MNN=ON
```

---

## Android 构建

### 环境准备

- Qualcomm® AI Runtime SDK（QAIRT）
- Android NDK（示例：r26d）
- Android Studio（用于构建 APK）

### 设置环境变量与路径（Windows 主机构建 Android）

```bat
cd ai-engine-direct-helper\samples\genie\c++\Service
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%
```

### 先构建 libappbuilder

Android 构建前需要先编译 `libappbuilder`（详见项目根目录的 [BUILD](https://github.com/quic/ai-engine-direct-helper/blob/main/BUILD.md) 说明），并将生成的 `libappbuilder.so` 放到：

`ai-engine-direct-helper\samples\genie\c++\Service`

### 构建 GenieAPIService（Android）

```bat
"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android -j4
```

构建完成后，将依赖的 `.so` 拷贝到 `libs/arm64-v8a`：

```bat
copy "%QNN_SDK_ROOT%lib\aarch64-android\*.so" "libs\arm64-v8a" /Y
copy "obj\local\arm64-v8a\*.so" "libs\arm64-v8a" /Y
```

### 构建 Android App（Android Studio）

1. 使用 Android Studio 打开：`ai-engine-direct-helper\samples\genie\c++\Android`
2. 菜单 **Build** → **Generate Signed App Bundle / APK** → 选择 **APK**
3. 选择签名密钥并完成构建
4. 在 `...\Android\app\release` 目录获取 `app-release.apk`
5. 安装：`adb install app-release.apk`

GitHub: https://github.com/quic/ai-engine-direct-helper