# Android 构建指南

## 概述

`build_android.bat` 用于构建 Android 版本的 `libappbuilder.so` 和 `GenieAPIService`，并打包成已签名的 APK。所有构建产物都落在 `build-android/`（连字符，不是下划线）下，源代码目录保持整洁。

**脚本位置**：`qai-appbuilder/samples/genie/c++/Service/build_android.bat`

## 前置条件

在运行脚本之前，请将以下内容导出为环境变量——脚本没有任何硬编码的默认路径，缺少必需变量时会立即报错退出。

1. **Qualcomm® AI Runtime SDK（QAIRT）**
   `QNN_SDK_ROOT`，例如 `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`（使用你实际安装的版本）。脚本要求 `%QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so` 必须存在。
2. **Android NDK**
   `NDK_ROOT`（或 `ANDROID_NDK_ROOT`），推荐版本 r26d，例如 `C:\work\android-ndk-r26d\`。要求 `%NDK_ROOT%\build\cmake\android.toolchain.cmake` 以及 `NDK_ROOT` 目录下的 `ndk-build.cmd`/`.bat` 存在。
3. **CMake** 和 **Ninja** —— 必须能通过 `PATH` 解析到（用于构建 `libsamplerate`）。
4. **Git** —— 用于克隆仓库及其子模块。
5. **JDK** —— 必须设置 `JAVA_HOME`，或者 `java` 能通过 `PATH` 解析到（供 Gradle 使用）。JDK 17 是本项目已验证的版本。

## 配置

在调用脚本之前设置以下环境变量（命令提示符语法；PowerShell 下用 `$env:NAME = "value"`）：

```batch
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"
set "NDK_ROOT=C:\work\android-ndk-r26d\"
set "JOBS=4"
```

| 变量 | 是否必需 | 说明 |
|------|---------|------|
| `QNN_SDK_ROOT` | 是 | 你的 QAIRT SDK 安装路径。 |
| `NDK_ROOT` / `ANDROID_NDK_ROOT` | 是 | 你的 Android NDK 安装路径。 |
| `JOBS` | 否 | 并行编译作业数，默认 `4`。 |
| `GRADLE_HOME` | 否 | 已解压的 Gradle 安装目录路径。设置后脚本直接调用其 `bin\gradle.bat`，完全跳过 wrapper 及 `GRADLE_DISTRIBUTION_ZIP`。 |
| `GRADLE_DISTRIBUTION_ZIP` | 否 | 本地 Gradle 分发包 zip 的路径。若已设置 `GRADLE_HOME` 则忽略此项；否则脚本会把 `distributionUrl` 改写为指向该文件，不再从 services.gradle.org 下载。 |

### Gradle 配置

Gradle、Android Gradle 插件（AGP）以及 app 模块自身原生构建（`app/src/main/cpp`，与 QAIRT 库无关）所用的 NDK，均自动解析，无需手动配置。已经装好 Gradle？设置 `GRADLE_HOME` 指向它，脚本会直接调用，完全跳过 wrapper。否则，离线或防火墙环境下，设置 `GRADLE_DISTRIBUTION_ZIP`（见上文"配置"）即可，不需要手动编辑 `gradle-wrapper.properties`。两者都不设置时，wrapper 仍会自动下载，但下载目标改为 `build-android\gradle-home`，不再落到默认的 `~/.gradle`，保持构建产物自包含。如果 Gradle 找不到 NDK，在 `Android/local.properties` 里设置 `ndk.dir`，或在 `android {}` 中加一行 `ndkVersion`。

### 发布签名

Android 系统拒绝安装任何未签名的 APK，本地侧载同样不例外——这是操作系统层面的强制规则，不是本项目自己加的限制。`build_android.bat` 始终执行 `assembleRelease`，必须有签名配置才能构建成功。

`Android/app/build.gradle.kts` 自带一份占位用的 `release` 签名配置，指向 `C:\work\Android\genieapiservice`（密码 `123456`）。不要直接复用这份配置，生成自己的密钥库，并替换 `storeFile`/`storePassword`/`keyAlias`/`keyPassword`：

```cmd
keytool -genkey -v -keystore C:\work\Android\genieapiservice.jks -alias key0 -keyalg RSA -keysize 2048 -validity 10000 -storepass 123456 -keypass 123456 -dname "CN=GenieAPIService, OU=Dev, O=Dev, L=Unknown, ST=Unknown, C=US"
```

加上 `-storepass`/`-keypass`/`-dname` 后这条命令可以直接无交互执行完成；密码和 DN 换成你自己的值即可。

不需要发布签名？直接在 `Android/` 下运行 `gradlew.bat assembleDebug`——AGP 会自动用一份临时调试密钥库给 debug 构建签名，不需要任何配置。`build_android.bat` 只跑 `assembleRelease`；要出 debug 包，先执行下方“构建步骤”里的第 1-5 步把原生库准备好，再自己直接调用 Gradle。

## 使用方法

```cmd
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder\samples\genie\c++\Service
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"
set "NDK_ROOT=C:\work\android-ndk-r26d\"
build_android.bat
```

请在命令提示符（CMD）中运行。脚本本身是普通的 `.bat` 文件；如果先在 PowerShell 里设置环境变量，PowerShell 也可以直接调用它。

### 构建步骤

脚本按顺序执行以下步骤，任意一步失败即停止：

1. 验证环境（必需的环境变量、`PATH` 中的 `ninja`/`cmake`、QNN Android 运行时库、NDK 的 `android.toolchain.cmake`、`gradlew.bat`、可用的 JDK）。
2. 通过 CMake + Ninja 针对 Android NDK 工具链构建 `libsamplerate.so`，带 `-DBUILD_SHARED_LIBS=ON`（vendored 源码默认是静态构建）。
3. 通过 `ndk-build` 构建 `libappbuilder.so`，使用仓库顶层的 `make/Android.mk` 和 `make/Application.mk`——这是仓库顶层 `Build.md`/`Makefile` 记录的官方路径，**不是**对 `src/` 的 CMake 构建。
4. 通过 `ndk-build` 构建原生的 `GenieAPIService`/JNI 包装层，使用 `scripts/Android.mk` 和 `scripts/Application.mk`。
5. 将 QNN SDK 运行时库（`libGenie.so`、`libQnnHtp*.so`、`libQnnSystem.so`，以及对应 `QNN_STUB_VERSION` 的 Hexagon stub/skel/`.cat` 文件）连同刚构建出的 `.so` 文件复制到 `build-android\output\libs\arm64-v8a`。
6. 运行 `gradlew.bat assembleRelease -PqnnStubVersion=<QNN_STUB_VERSION>` 生成已签名的 APK。
7. 将生成的 APK 复制到 `build-android\output\GenieAPIService.apk`。

## 构建产物

```
build-android/
├── libsamplerate/            # libsamplerate 的 CMake+Ninja 构建目录
├── libsamplerate.so          # 从 libsamplerate/ 拷贝出来,方便使用
├── libappbuilder-obj/        # libappbuilder 的 ndk-build 中间产物(make/Android.mk)
├── libappbuilder-libs/       # libappbuilder 的 ndk-build 输出
├── libappbuilder.so          # 从 libappbuilder-libs/arm64-v8a/ 拷贝出来,方便使用
├── obj/                      # 原生 service 的 ndk-build 中间产物
├── libs/
│   └── arm64-v8a/            # 原生 service + JNI 包装层的 ndk-build 输出
└── output/
    ├── libs/
    │   └── arm64-v8a/         # 全部可部署的 .so 文件汇总于此
    └── GenieAPIService.apk    # Android APK 安装包
```

- **`build-android/output/libs/arm64-v8a/`**：Android 部署所需的全部 `.so` 文件。
- **`build-android/output/GenieAPIService.apk`**：构建出的 Android APK 安装包。

## 安装 APK

```cmd
adb install build-android/output/GenieAPIService.apk
```

## 故障排查

**`[ERROR] QNN_SDK_ROOT is not set.`**
运行脚本前导出 `QNN_SDK_ROOT`，例如 `set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"`。

**`[ERROR] NDK_ROOT or ANDROID_NDK_ROOT is not set.`**
运行脚本前导出 `NDK_ROOT`（或 `ANDROID_NDK_ROOT`）。

**构建失败，提示 "cannot find header files"**
克隆时加上 `--recursive`，或运行 `git submodule update --init --recursive` 补齐缺失的子模块。

**Gradle 构建失败**
如果 Gradle 无法下载分发包，运行脚本前设置 `GRADLE_HOME` 指向已有的 Gradle 安装，或设置 `GRADLE_DISTRIBUTION_ZIP` 指向本地 zip（见上文"Gradle 配置"），不需要手动编辑 `gradle-wrapper.properties`。如果改过 `libs.versions.toml` 中的 Android Gradle 插件版本，核实该值是否仍然有效。确认 JDK 已安装并正确配置。

**`Could not resolve all files for configuration ':app:androidJdkImage'`**

```
Execution failed for task ':app:compileReleaseJavaWithJavac'.
> Could not resolve all files for configuration ':app:androidJdkImage'.
   > Failed to transform core-for-system-modules.jar...
```

这是 Android Studio 内置 JDK 与 Gradle 版本不兼容导致的。用以下任一方式修复：

- **使用 JDK 17（推荐）**：在 Android Studio 中打开 **File → Settings → Build, Execution, Deployment → Build Tools → Gradle**，把 "Gradle JDK" 设为 **JDK 17**，应用后清理并重新构建。
- **手动设置 `JAVA_HOME`**：
  ```cmd
  set JAVA_HOME=C:\Program Files\Java\jdk-17
  cd qai-appbuilder\samples\genie\c++\Android
  gradlew.bat clean assembleRelease
  ```

**清理构建产物**
删除 `build-android` 目录：`rmdir /s /q build-android`。

**修改并行作业数量**
运行脚本前设置 `JOBS`，例如 `set "JOBS=8"`（默认 `4`）。建议设为 CPU 核心数或略少。

## 版本信息

- 支持的 NDK 版本：r26d
- 支持的目标架构：arm64-v8a
- 支持的 QAIRT 版本：2.44.0.260225（使用你实际安装的版本——这只是本项目已验证的版本）
- Gradle：8.5 · Android Gradle 插件：8.1.4（见上文"Gradle 配置"）

## 许可证

该脚本遵循与 qai-appbuilder 项目相同的许可证（BSD-3-Clause）。
