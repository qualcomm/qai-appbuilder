# Android 构建指南

## 概述

`build_android.bat` 是一个自动化构建脚本，用于编译 libappbuilder.so 和 GenieAPIService 库的 Android 版本。该脚本会自动处理依赖关系，并将所有构建产物整理到 `build-android` 文件夹（注意是连字符，不是下划线）中，从而保持源代码目录的整洁。

**脚本位置**：`qai-appbuilder/samples/genie/c++/Service/build_android.bat`

## 特性

✅ 自动构建 libappbuilder.so 及其依赖项
✅ 自动构建 GenieAPIService 原生库
✅ 自动复制所有必需的 QNN SDK 库文件
✅ 将所有构建产物整理到独立的 `build-android` 目录中
✅ 提供构建日志，并在第一个失败步骤处立即停止
✅ 环境验证与错误检查（必需的环境变量、`PATH` 中的工具、预期的 SDK/NDK 文件）
✅ 构建包含所有库的 Android APK 安装包

## 前置条件

在运行该脚本之前，请确保已安装以下工具，并且——与该脚本此前的版本不同——在调用脚本之前将它们导出为**环境变量**（该脚本已不再包含任何硬编码的默认路径——如果缺少必需的变量，脚本会立即失败并给出明确的错误提示）：

1. **Qualcomm® AI Runtime SDK（QAIRT）**
   - `QNN_SDK_ROOT`，例如 `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`（使用你实际安装的版本——这里只是一个示例；脚本要求 `%QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so` 必须存在）

2. **Android NDK**
   - `NDK_ROOT`（或 `ANDROID_NDK_ROOT`），推荐版本 r26d，例如 `C:\work\android-ndk-r26d\`
   - 要求 `%NDK_ROOT%\build\cmake\android.toolchain.cmake` 以及 `NDK_ROOT` 目录下存在一个 `ndk-build.cmd`/`.bat`

3. **CMake** 和 **Ninja** —— 两者都必须能通过 `PATH` 解析到（用于构建 `libsamplerate`/`libappbuilder`）

4. **Git**（用于克隆仓库）

5. **Java Development Kit（JDK）** —— 必须设置 `JAVA_HOME`，或者 `java` 必须能通过 `PATH` 解析到（供 Gradle 构建使用）。JDK 17 是本项目已验证的版本。

## 配置

### 在运行 `build_android.bat` 之前设置环境变量

与该脚本更早的版本不同，你不再需要编辑 `build_android.bat` 本身来指向你的 SDK/NDK——该脚本会从环境变量读取所有信息，如果缺少必需的变量，会立即失败并给出明确的 `[ERROR] ... is not set` 提示信息。

在调用脚本之前设置以下内容（示例为命令提示符语法；如果你在 PowerShell 中运行，请改用 `$env:NAME = "value"`）：

```batch
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"
set "NDK_ROOT=C:\work\android-ndk-r26d\"
set "JOBS=4"
```

**配置参数：**

- **QNN_SDK_ROOT**：你的 Qualcomm AI Runtime SDK 安装路径。*（必需；使用你实际安装的版本，上面的 `2.44.0.260225` 只是一个示例）*
- **NDK_ROOT**（或 **ANDROID_NDK_ROOT**）：你的 Android NDK 安装路径。*（必需）*
- **JOBS**：并行编译作业的数量。*（可选，未设置时默认为 `4`）*

### Gradle 项目文件（仅供参考——请对照你实际的项目进行核实）

> **注意：** 这份 C++ service 源码树本身并不包含 `Android/` Gradle 项目目录，因此下面的具体数值无法针对本仓库快照进行核实。请将它们视为典型 Gradle 配置的**参考示例**，并始终以你自己的 `Android/app/build.gradle.kts`、`Android/gradle/wrapper/gradle-wrapper.properties` 和 `Android/gradle/libs.versions.toml` 中的实际值为准。

##### `samples/genie/c++/Android/gradle/wrapper/gradle-wrapper.properties`

将 Gradle 指向本地的分发 zip 文件，例如：

```properties
distributionUrl=file\:///C:/Programs/gradle-8.7-bin.zip
```

将其改为与你的 Gradle 分发包实际所在位置匹配的值。

##### `samples/genie/c++/Android/gradle/libs.versions.toml`

Android Gradle 插件版本，例如：

```toml
[versions]
agp = "8.7.3"  # Adjust to match your installed version
```

##### `samples/genie/c++/Android/app/build.gradle.kts`

**重要**：为发布版本配置 APK 签名：

```kotlin
android {
    compileSdk = 35  // Adjust if needed
    ndkVersion = "26.3.11579264"  // Match your NDK version
    
    // Configure signing for release builds
    signingConfigs {
        create("release") {
            storeFile = file("C:\\work\\Android\\genieapiservice")  // ⚠️ CHANGE THIS
            storePassword = "123456"                                 // ⚠️ CHANGE THIS
            keyAlias = "key0"                                        // ⚠️ CHANGE THIS
            keyPassword = "123456"                                   // ⚠️ CHANGE THIS
        }
    }
    
    defaultConfig {
        minSdk = 29
        targetSdk = 35  // Adjust if needed
    }
    
    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
        }
    }
}
```

**签名配置参数：**

- **storeFile**：你的密钥库文件路径（`.jks` 或 `.keystore`）
- **storePassword**：密钥库的密码
- **keyAlias**：密钥库中密钥的别名
- **keyPassword**：密钥的密码

**如何创建密钥库（如果你还没有的话）：**

```cmd
keytool -genkey -v -keystore C:\work\Android\genieapiservice.jks -alias key0 -keyalg RSA -keysize 2048 -validity 10000
```

按照提示设置密码和证书信息。

## 使用方法

### 1. 克隆仓库（如果尚未完成）

```cmd
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder\samples\genie\c++\Service
```

### 2. 设置所需的环境变量

**重要**：按照上面的“配置”一节导出 `QNN_SDK_ROOT`/`NDK_ROOT`（如有需要，再检查 Gradle 项目文件）——无需编辑 `build_android.bat` 本身。

### 3. 运行构建脚本

在命令提示符（CMD）中执行：

```cmd
build_android.bat
```

**注意**：请在命令提示符（CMD）中运行。如果你使用 PowerShell，请先用
`$env:NAME = "value"` 设置环境变量——脚本本身是一个普通的 `.bat` 文件，PowerShell 可以直接调用它。

### 4. 等待构建完成

该脚本按顺序执行以下步骤（并没有形如 `[n/9]` 的编号进度输出；
以下只是 `build_android.bat` 中实际存在的真实执行顺序）：

1. 验证环境：必需的环境变量（`QNN_SDK_ROOT`、`NDK_ROOT`/`ANDROID_NDK_ROOT`）、`PATH` 中的 `ninja`/`cmake`、QNN Android 运行时库、NDK 的 `android.toolchain.cmake`、`gradlew.bat`，以及一个可用的 JDK。
2. 创建 `build-android` 目录以及 `build-android\output\libs\arm64-v8a`。
3. 通过 **CMake + Ninja** 针对 Android NDK 工具链构建 `libsamplerate.so`，并带上 `-DBUILD_SHARED_LIBS=ON`（vendored 的 `External/libsamplerate` 默认是静态构建，因此必须显式传入这个标志）。
4. 同样通过 **CMake + Ninja**，从仓库顶层的 `src/` 目录（与 `qai_appbuilder` Python wheel 所链接的是同一份源码）构建 `libappbuilder.so`——**不是**通过 `ndk-build`。
5. 通过 `ndk-build` 构建原生的 `GenieAPIService`/JNI 包装层，使用 `scripts/Android.mk` + `scripts/Application.mk`（这是整个流程中唯一真正使用 `ndk-build` 的步骤）。
6. 将 QNN SDK 运行时库（`libGenie.so`、`libQnnHtp*.so`、`libQnnSystem.so`，以及对应 `QNN_STUB_VERSION` 的 Hexagon stub/skel/`.cat` 文件）连同刚构建出的 `.so` 文件一起复制到 `build-android\output\libs\arm64-v8a`。
7. 运行 `gradlew.bat assembleRelease -PqnnStubVersion=<QNN_STUB_VERSION>` 生成已签名的 APK。
8. 将生成的 APK 复制到 `build-android\output\GenieAPIService.apk`。
9. 打印简短的构建摘要（原生库输出目录 + APK 路径）。

## 构建产物

构建成功后，所有文件都会整理到 `build-android` 目录中（连字符，不是下划线）：

```
build-android/
├── libsamplerate/            # libsamplerate CMake+Ninja build tree
├── libappbuilder/            # libappbuilder.so CMake+Ninja build tree
├── libsamplerate.so          # copied out of libsamplerate/ for convenience
├── libappbuilder.so          # copied out of the top-level src/lib/ for convenience
├── obj/                      # ndk-build intermediates for the native service
├── libs/
│   └── arm64-v8a/            # ndk-build output for the native service + JNI wrapper
└── output/
    ├── libs/
    │   └── arm64-v8a/         # All deployable .so files, consolidated here
    │       ├── libappbuilder.so
    │       ├── libsamplerate.so
    │       ├── libGenieAPIService.so
    │       ├── libJNIGenieAPIService.so
    │       ├── libGenie.so
    │       ├── libQnnHtp*.so
    │       └── ... (other QNN SDK runtime libraries)
    └── GenieAPIService.apk    # Android APK package (directly under output/, no apk/ subfolder)
```

### 关键输出目录

- **`build-android/output/libs/arm64-v8a/`**：包含 Android 部署所需的全部 .so 文件
- **`build-android/output/GenieAPIService.apk`**：构建出的 Android APK 安装包

## 安装 APK

构建完成后，直接安装该 APK：

```cmd
adb install build-android/output/GenieAPIService.apk
```

## 故障排查

### Q1：脚本报错 "[ERROR] QNN_SDK_ROOT is not set."

**解决方法**： 
- 确认 QAIRT SDK 已正确安装
- 在运行脚本之前将 `QNN_SDK_ROOT` 导出为环境变量（例如 `set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"`）——不再需要编辑脚本本身

### Q2：脚本报错 "[ERROR] NDK_ROOT or ANDROID_NDK_ROOT is not set."

**解决方法**：
- 确认 Android NDK 已正确安装
- 在运行脚本之前将 `NDK_ROOT`（或 `ANDROID_NDK_ROOT`）导出为环境变量

### Q3：构建失败，提示 "cannot find header files"

**解决方法**：
- 确保你在克隆仓库时使用了 `--recursive` 参数以包含所有子模块
- 运行 `git submodule update --init --recursive` 更新子模块

### Q4：Gradle 构建失败

**解决方法**：
- 对照你实际的 `Android/` 项目，核实 `gradle-wrapper.properties` 中的 Gradle 分发路径以及 `libs.versions.toml` 中的 Android Gradle 插件版本（这些值无法根据本仓库快照进行核实——参见“配置”一节中的说明）
- 确保 JDK 已正确安装和配置

### Q4.1：Android Studio 构建失败，提示 "Could not resolve all files for configuration ':app:androidJdkImage'"

**错误信息：**
```
Execution failed for task ':app:compileReleaseJavaWithJavac'.
> Could not resolve all files for configuration ':app:androidJdkImage'.
   > Failed to transform core-for-system-modules.jar...
```

**解决方法**：

出现该错误的原因是 Android Studio 内置的 JDK 与 Gradle 版本不兼容。可尝试以下方案：

**方案 1：使用 JDK 17（推荐）**

1. 在 Android Studio 中，进入 **File → Settings → Build, Execution, Deployment → Build Tools → Gradle**
2. 在 "Gradle JDK" 下选择 **JDK 17**（如果没有则下载）
3. 点击 **Apply** 和 **OK**
4. 清理并重新构建项目

**方案 2：使用 build_android.bat 脚本**

该自动化构建脚本只检查 `JAVA_HOME`/`java` 是否可用；它**不会**
自动解决 JDK/Gradle 版本不兼容的问题。如果方案 1 或方案 3 不适用于你的
环境，请在运行脚本之前确保脚本所选用的任何 JDK 都与 Gradle 兼容：
```cmd
cd qai-appbuilder\samples\genie\c++\Service
build_android.bat
```

**方案 3：手动设置 JAVA_HOME**

如果你单独安装了 JDK 17：
```cmd
set JAVA_HOME=C:\Program Files\Java\jdk-17
cd qai-appbuilder\samples\genie\c++\Android
gradlew.bat clean assembleRelease
```

### Q5：如何清理构建产物？

**解决方法**：
- 删除 `build-android` 目录：`rmdir /s /q build-android`
- 源代码目录保持整洁，不含任何构建产物

### Q6：可以更改并行作业数量吗？

**解决方法**：
- 可以，在运行脚本之前将 `JOBS` 环境变量设置为你想要的值，例如 `set "JOBS=8"`（未设置时默认为 `4`）
- 建议设置为你的 CPU 核心数或略少一些

## 脚本特性

### 1. 环境验证
该脚本会在构建之前验证所有必需的环境变量、`PATH` 中的工具，以及预期的 SDK/NDK 文件，并打印出明确指出缺少了什么的 `[ERROR] ...` 提示信息。

### 2. 错误处理
每个构建步骤都会检查自身的退出码（`if errorlevel 1 exit /b 1`）；如果任何步骤失败，脚本会立即停止。

### 3. 构建产物隔离
所有构建产物都存放在 `build-android` 目录中，从而保持源代码目录的整洁。

### 4. 自动依赖处理
该脚本会先构建 `libsamplerate` 和 `libappbuilder.so`，再按正确的顺序构建依赖它们的原生 `GenieAPIService`/JNI 包装层。

### 5. 构建进度输出
将已解析的配置（SDK/NDK 路径、stub 版本、作业数、输出目录）以及各步骤的进度打印到控制台。

### 6. APK 生成
自动构建一个可供安装的 Android APK 安装包。

## 技术细节

### 构建流程

1. **libsamplerate.so 构建**
   - 针对 Android NDK 工具链文件使用 **CMake + Ninja**
   - 目标架构：arm64-v8a
   - 使用 `-DBUILD_SHARED_LIBS=ON` 构建（vendored 源码默认是静态构建）

2. **libappbuilder.so 构建**
   - 同样针对 Android NDK 工具链文件使用 **CMake + Ninja**（不是 `ndk-build`）
   - 目标架构：arm64-v8a
   - 源码：仓库顶层的 `src/` 目录（与 `qai_appbuilder` Python wheel 所链接的是同一份源码）

3. **GenieAPIService 构建**
   - 依赖 libappbuilder.so
   - 这是整个流程中唯一真正使用 Android NDK 的 `ndk-build` 工具的步骤
   - 构建配置：`scripts/Android.mk` 和 `scripts/Application.mk`

4. **Android APK 构建**
   - 使用 Gradle 构建系统（`gradlew.bat assembleRelease -PqnnStubVersion=<QNN_STUB_VERSION>`）
   - 打包所有原生库
   - 配置：`Android/app/build.gradle.kts` *（仅供参考——参见“配置”一节中的说明）*

5. **库文件收集**
   - 从 QNN SDK 复制运行时库
   - 从构建输出中复制生成的库
   - 将所有文件汇总到 `build-android/output/libs/arm64-v8a/`


## 版本信息

- **脚本版本**：2.0
- **支持的 NDK 版本**：r26d
- **支持的目标架构**：arm64-v8a
- **支持的 QAIRT 版本**：2.44.0.260225（使用你实际安装的版本——这只是本项目已验证的版本）
- **Gradle 版本 / Android Gradle 插件**：参见“配置”一节中的免责说明——无法根据本仓库快照进行核实；请检查你自己的 `Android/gradle/` 项目文件

## 许可证

该脚本遵循与 qai-appbuilder 项目相同的许可证（BSD-3-Clause）。

## 反馈与支持

如果你遇到问题：

1. 查看本文档中的“故障排查”一节
2. 核实你的环境配置是否正确
3. 查看构建日志中的错误信息

---

**最后更新时间**：2026-04-17
