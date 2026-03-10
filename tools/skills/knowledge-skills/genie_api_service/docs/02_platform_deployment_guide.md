# GenieAPIService 平台部署指南

## Windows 平台部署

### 步骤 1：下载资源

1. **下载 GenieAPIService**
    - 访问 [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0)
    - 下载 [GenieAPIService_v2.1.4_QAIRT_v2.42.0_v73.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.42.0/GenieAPIService_v2.1.4_QAIRT_v2.42.0_v73.zip)

2. **下载模型文件**
    - 根据需要 [下载](https://www.aidevhome.com/?id=51) 对应的模型文件
    - 常见模型：Qwen2.0-7B、Llama-3.1-8B、Qwen2.5-VL-3B 等

### 步骤 2：解压和配置

1. **解压 GenieAPIService**
   ```
   解压 GenieAPIService_v2.1.4_QAIRT_v2.42.0_v73.zip 到目标目录
   例如：C:\GenieAPIService\
   ```

2. **配置模型文件**
   ```
   将模型文件放置在 models 目录下
   目录结构示例：
   C:\GenieAPIService\
   ├── GenieAPIService.exe
   ├── models\
   │   ├── Qwen2.0-7B-SSD\
   │   │   ├── config.json
   │   │   ├── model files...
   │   ├── qwen2.5vl3b\
   │   │   ├── config.json
   │   │   ├── model files...
   ```

### 步骤 3：启动服务

打开命令提示符（CMD）或 PowerShell，进入 GenieAPIService 目录：

#### 启动文本模型服务

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

#### 启动视觉语言模型服务

```cmd
GenieAPIService.exe -c models/qwen2.5vl3b/config.json -l
```

#### 常用启动参数

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -d 3 -n 10 -o 1024 -p 8910
```

### 步骤 4：验证服务

服务启动成功后，会显示类似以下信息：

```
GenieAPIService: 2.1.4, Genie Library: 1.14.0
current work dir: C:\GenieAPIService
root dir: C:\GenieAPIService
Loading model...
Model loaded successfully
Server listening on port 8910
```

---

## Android 平台部署

### 步骤 1：安装 APK

1. **下载 APK**
    - 访问 [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0)
    - 下载 [GenieAPIService.apk](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.42.0/GenieAPIService.apk)

2. **安装 APK**
   ```
   adb install GenieAPIService.apk
   ```
   或直接在设备上安装

### 步骤 2：准备模型文件

1. **创建模型目录**
   ```
   adb shell mkdir -p /sdcard/GenieModels
   ```

2. **推送模型文件**
   ```
   adb push models/Qwen2.0-7B-SSD /sdcard/GenieModels/
   ```

   模型目录结构：
   ```
   /sdcard/GenieModels/
   ├── Qwen2.0-7B-SSD/
   │   ├── config.json
   │   ├── model files...
   ├── qwen2.5vl3b/
   │   ├── config.json
   │   ├── model files...
   ```

### 步骤 3：启动服务

1. **打开 GenieAPIService 应用**
    - 在设备上找到并打开 GenieAPIService 应用

2. **启动服务**
    - 点击 `START SERVICE` 按钮
    - 等待模型加载完成
    - 看到 "Genie API Service IS Running." 表示服务已启动

3. **配置后台运行**（重要）
    - 进入设备设置 → 电池 → 省电设置 → 应用电池管理
    - 找到 GenieAPIService 应用
    - 选择 "允许后台活动"

### 步骤 4：查看日志

- 点击右上角菜单
- 选择 "Log Files" → "Log:1"
- 可以查看服务运行日志

### 步骤 5：安装客户端应用

推荐使用以下客户端应用：

1. **GenieChat**
    - 源码位置：`samples/android/GenieChat`
    - 使用 Android Studio 编译安装
    - 或直接 [下载](https://www.aidevhome.com/zb_users/upload/2026/01/202601281769601771694706.apk) 编译好的 apk

2. **GenieFletUI**
    - 源码位置：`samples/fletui/GenieFletUI/android`
    - 参考 [Build.md](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/fletui/GenieFletUI/android/BUILD.md) 将 Flet 代码编译成 apk

GitHub: https://github.com/quic/ai-engine-direct-helper