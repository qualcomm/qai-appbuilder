# GenieAPIService 使用指南

<div align="center">
  <h2>在本地 NPU 上运行大语言模型</h2>
  <p><i>OpenAI 兼容 API 服务 (C++)</i></p>
</div>

---

## 目录

1. [📘 简介](#简介)
2. [⚙️ 系统要求](#系统要求)
3. [✨ 功能特性](#功能特性)
4. [🚀 平台部署](#平台部署)
   - [Windows 平台部署](#windows-平台部署)
   - [Android 平台部署](#android-平台部署)
5. [🧠 模型配置](#模型配置)
   - [配置文件结构](#配置文件结构)
   - [文本模型部署](#文本模型部署)
   - [多模态 Qwen2.5-VL-3B 模型](#qwen25-vl-3b-模型)
   - [多模态 Phi-4 模型](#phi-4-多模态模型)
6. [🛠️ 服务使用](#服务使用)
7. [📱 客户端使用](#客户端使用)
   - [C++ 客户端](#c-客户端)
   - [Python 客户端](#python-客户端)
   - [其他语言客户端](#其他语言客户端)
8. [🔨 从源码构建](#从源码构建)
   - [源码与依赖准备](#源码与依赖准备)
   - [Windows 构建](#windows-构建)
   - [Android 构建](#android-构建)
9. [🔧 其他工具](#其他工具)
   - [encode.exe](#encodeexe)
   - [decode.exe](#decodeexe)
   - [wav.exe](#wavexe)
10. [📡 API 接口说明](#api-接口说明)
    - [聊天完成接口](#1-聊天完成接口)
    - [模型列表接口](#2-模型列表接口)
    - [文本分割接口](#3-文本分割接口)
    - [停止输出接口](#4-停止输出接口)
    - [清除历史记录接口](#5-清除历史记录接口)
    - [重新加载历史记录接口](#6-重新加载历史记录接口)
    - [获取历史记录接口](#7-获取历史记录接口)
    - [获取模型上下文大小接口](#8-获取模型上下文大小接口)
    - [获取模型性能信息接口](#9-获取模型性能信息接口)
    - [停止服务接口](#10-停止服务接口)
11. [📄 进阶示例](#进阶示例)
    - [Python 基本对话示例](#python-基本对话示例)
    - [工具调用示例](#工具调用示例)
    - [视觉语言模型示例](#视觉语言模型示例)
    - [其他语言示例](#其他语言示例)
12. [❓ 常见问题](#常见问题)
13. [📞 技术支持](#技术支持)

---

## 简介

GenieAPIService 是一个基于 C++ 开发的 OpenAI 兼容 API 服务，可以在 Windows on Snapdragon (WoS)、移动设备和 Linux
平台上运行。该服务允许开发者在本地设备的 NPU（神经处理单元）或 CPU 上运行大语言模型，无需依赖云端服务。

### 主要优势

- **本地运行**：所有推理在本地设备上完成，保护数据隐私
- **OpenAI 兼容**：使用标准的 OpenAI API 格式，易于集成
- **多平台支持**：支持 Windows、Android 和 Linux 平台
- **高性能**：利用 Qualcomm® AI Runtime SDK 实现 NPU 加速

---

## 系统要求

### Windows 平台

- **操作系统**：Windows 11 或更高版本
- **处理器**：支持 Qualcomm Snapdragon 的设备
- **内存**：至少 16GB RAM（推荐 32GB 或更多）
- **存储空间**：至少 10GB 可用空间（用于模型文件）
- **软件依赖**：
    - Qualcomm® AI Runtime SDK (QAIRT) 2.42.0 或更高版本(软件包自带，不需额外安装)
    - [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-160)

### Android 平台

- **操作系统**：Android 10 或更高版本
- **处理器**：Qualcomm Snapdragon 芯片（支持 NPU）
- **内存**：至少 8GB RAM（推荐 16GB 或更多）
- **存储空间**：至少 10GB 可用空间
- **权限**：需要存储访问权限和后台运行权限

### Linux 平台

- **操作系统**：Ubuntu 20.04 或更高版本
- **处理器**：ARM64
- **内存**：至少 16GB RAM
- **存储空间**：至少 10GB 可用空间

---

## 功能特性

GenieAPIService 提供了丰富的功能特性：

### 核心功能

- ✅ **CPU & NPU 支持**：支持在 CPU 和 NPU 上运行 LLM
- ✅ **流式和非流式模式**：支持流式输出和完整响应两种模式
- ✅ **模型切换**：支持在运行时切换不同的模型
- ✅ **多模态支持**：支持视觉语言模型（VLM）
- ✅ **自定义模型**：支持用户自定义模型配置
- ✅ **文本分割**：内置文本分割功能，处理长文本输入
- ✅ **工具调用**：支持 Function Calling 功能
- ✅ **思考模式**：支持启用/禁用思考模式
- ✅ **LoRA 支持**：支持 LoRA 适配器
- ✅ **历史记录**：支持对话历史记录功能

### 支持的模型格式

- **BIN 格式**：Qualcomm QNN 格式模型（默认）
- **MNN 格式**：阿里巴巴 MNN 框架模型（需编译时启用）
- **GGUF 格式**：llama.cpp 格式模型（需编译时启用）

---

## 平台部署

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

---

## 模型配置

### 配置文件结构

每个模型需要一个 `config.json` 配置文件，[参考示例](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python/models)。

基本配置文件结构：

其中用到的 [htp_backend_ext_config.json](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/python/config/htp_backend_ext_config.json) 可直接下载使用。

```json
{
  "dialog" : {
    "version" : 1,
    "type" : "basic",
    "context" : {
      "version" : 1,
      "size": 4096,
      "n-vocab":   152064,
      "bos-token": 151643,
      "eos-token": 151645,
      "eot-token": 128009
    },
    "sampler" : {
      "version" : 1,
      "seed" : 42,
      "temp" : 1,
      "top-k" : 1,
      "top-p" : 1
    },
    "tokenizer" : {
      "version" : 1,
      "path" : "models\\Qwen2.0-7B\\tokenizer.json"
    },
    "engine" : {
      "version" : 1,
      "n-threads" : 3,
      "backend" : {
        "version" : 1,
        "type" : "QnnHtp",
        "QnnHtp" : {
          "version" : 1,
          "use-mmap" : false,
          "spill-fill-bufsize" : 0,
          "mmap-budget" : 0,
          "poll" :false,
          "pos-id-dim" : 64,
          "cpu-mask" : "0xe0",
          "kv-dim" : 128,
          "rope-theta": 1000000
        },
        "extensions" : "models\\htp_backend_ext_config.json"
      },
      "model" : {
        "version" : 1,
        "type" : "binary",
        "binary" : {
          "version" : 1,
          "ctx-bins" : [
            "models\\Qwen2.0-7B\\model-1.bin",
            "models\\Qwen2.0-7B\\model-2.bin",
            "models\\Qwen2.0-7B\\model-3.bin",
            "models\\Qwen2.0-7B\\model-4.bin",
            "models\\Qwen2.0-7B\\model-5.bin"
          ]
        }
      }
    }
  }
}
```

### 文本模型部署

文本模型的标准目录结构：

```
models/Qwen2.0-7B-SSD/
├── config.json           # 模型配置文件
├── prompt.json           # 提示词模板
├── tokenizer.json        # 分词器
├── model-0.bin           # 模型文件
└── model-1.bin           # 模型文件
```

### Qwen2.5-VL-3B 模型

Qwen2.5-VL-3B 是一个多模态视觉语言模型，支持图像理解和文本生成。

#### 模型目录结构

```
models/qwen2.5vl3b/
├── config.json
├── embedding_weights.raw
├── htp_backend_ext_config.json
├── llm_model-0.bin
├── llm_model-1.bin
├── prompt.json
├── tokenizer.json
├── veg.serialized.bin
└── raw/
    ├── full_attention_mask.raw
    ├── position_ids_cos.raw
    ├── position_ids_sin.raw
    └── window_attention_mask.raw
```

#### Qwen2.5-VL-3B 模型部署

##### Windows 部署

1) 将模型目录 `qwen2.5vl3b` 放入：`models\`目录。

2) 在 `samples` 目录启动服务：

```bat
GenieAPIService.exe -c "models\qwen2.5vl3b\config.json" -l
```

3) 在当前目录准备测试用图片 `test.png`，运行客户端：

```bat
GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b
```

##### Android 部署

- 模型放置到：`/sdcard/GenieModels/qwen2.5vl3b/`
- 安装并打开 GenieAPIService APK，点击 **START SERVICE** 加载模型

##### Python 客户端示例

> GenieAPIService 提供 OpenAI 兼容接口；对于 VLM（视觉语言模型），`messages` 里需要传 `{question, image}`，其中 `image` 为 Base64 字符串。

下面示例同时支持传入本地图片路径或图片 URL（URL 会先下载再编码）：

```python
import argparse
import base64
import os
import requests
from openai import OpenAI

IP_ADDR = "127.0.0.1:8910"

def encode_image(image_input: str) -> str:
    if image_input.startswith(("http://", "https://")):
        r = requests.get(image_input, timeout=10)
        r.raise_for_status()
        return base64.b64encode(r.content).decode("utf-8")
    if not os.path.exists(image_input):
        raise FileNotFoundError(f"Local file not found: {image_input}")
    with open(image_input, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="Describe this image")
parser.add_argument("--image", required=True, help="Local image path or URL")
args = parser.parse_args()

base64_image = encode_image(args.image)
client = OpenAI(base_url="http://" + IP_ADDR + "/v1", api_key="123")

custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": {"question": args.prompt, "image": base64_image}},
]
extra_body = {
    "size": 4096,
    "temp": 1.5,
    "top_k": 13,
    "top_p": 0.6,
    "messages": custom_messages,
}
model_name = "qwen2.5vl3b-8380-2.42"
placeholder_msgs = [{"role": "user", "content": "placeholder"}]

resp = client.chat.completions.create(
    model=model_name,
    messages=placeholder_msgs,
    stream=args.stream,
    extra_body=extra_body,
)

if args.stream:
    for chunk in resp:
        if chunk.choices and chunk.choices[0].delta.content is not None:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()
else:
    print(resp.choices[0].message.content)
```

### 提示词模板配置

`prompt.json` 文件定义了模型的提示词格式：

```json
{
  "prompt_system": "<|im_start|>system\n string <|im_end|>\n",
  "prompt_user": "<|im_start|>user\n string <|im_end|>\n",
  "prompt_assistant": "<|im_start|>assistant\n string <|im_end|>\n",
  "prompt_tool": "<|im_start|>tool\n string <|im_end|>\n",
  "prompt_start": "<|im_start|>assistant\n"
}
```

不同模型可能使用不同的提示词格式，请根据模型文档配置相应的模板。参考 [模板](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python/models)。

---

## 服务使用

### 启动服务

基本启动命令：

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

### 完整参数列表

```
GenieAPIService.exe [OPTIONS]

必需参数：
  -c, --config_file <path>    模型配置文件路径

可选参数：
  -l, --load_model            启动时自动加载模型
  -p, --port <port>           服务端口（默认：8910）
  -d, --loglevel <level>      日志级别（1-5）
                              1: Error
                              2: Warning
                              3: Info
                              4: Debug
                              5: Verbose
  -n, --num_response <num>    保存的历史对话轮数（默认：10）
  -o, --min_output_num <num>  最小输出 token 数量（默认：1024）
  -t, --enable_thinking       启用思考模式
  -a, --all_text              输出所有文本（包括工具调用）
  --adapter <name>            LoRA 适配器名称
  --lora_alpha <value>        LoRA Alpha 值（默认：1.0）
```

### 使用示例

#### 基本启动

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

#### 自定义端口和日志级别

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -p 9000 -d 4
```

#### 启用思考模式

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -t
```

#### 使用 LoRA 适配器

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l --adapter my_lora --lora_alpha 0.8
```

### 服务状态检查

服务启动后，可以通过以下方式检查状态：

1. **查看控制台输出**
   ```
   GenieAPIService: 2.1.4, Genie Library: 1.14.0
   Server listening on port 8910
   Model loaded successfully
   ```

2. **访问健康检查端点**
   ```bash
   curl http://localhost:8910/health
   ```

3. **查看模型列表**
   ```bash
   curl http://localhost:8910/v1/models
   ```

---

## 客户端使用

### C++ 客户端

GenieAPIClient 是一个 C++ 命令行客户端，用于与 GenieAPIService 交互。

#### 基本用法

```cmd
GenieAPIClient.exe --prompt "你好，请介绍一下自己" --stream
```

#### 完整参数列表

```
GenieAPIClient.exe [OPTIONS]

必需参数：
  --prompt <text>             输入提示词

可选参数：
  --stream                    启用流式输出
  --model <name>              指定模型名称
  --img <path>                图像文件路径（用于多模态模型）
  --host <address>            服务器地址（默认：127.0.0.1）
  --port <port>               服务器端口（默认：8910）
  --temperature <value>       温度参数（默认：0.7）
  --top_p <value>             Top-p 参数（默认：0.9）
  --top_k <value>             Top-k 参数（默认：40）
  --max_tokens <num>          最大输出 token 数（默认：2048）
```

#### C++ 客户端使用
[参考代码](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient/GenieAPIClient.cpp)<br>
##### 文本对话

```cmd
GenieAPIClient.exe --prompt "解释一下量子计算的原理" --stream
```

##### 多模态对话

```cmd
GenieAPIClient.exe --prompt "这张图片里有什么？" --img test.png --stream --model qwen2.5vl3b
```

##### 自定义参数

```cmd
GenieAPIClient.exe --prompt "写一首诗" --stream --temperature 0.9 --max_tokens 500
```

### Python 客户端使用
[参考代码](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient/GenieAPIClient.py)<br>
Python 客户端使用 OpenAI SDK，提供更灵活的集成方式。

#### 安装依赖

```bash
pip install openai
```

#### 基本示例

```python
from openai import OpenAI

# 创建客户端
client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"  # 任意字符串即可
)

# 发送请求
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你好，请介绍一下自己"}
    ],
    stream=True
)

# 处理流式响应
for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

#### 非流式模式

```python
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "user", "content": "什么是人工智能？"}
    ],
    stream=False
)

print(response.choices[0].message.content)
```

#### 自定义参数

```python
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "user", "content": "写一首关于春天的诗"}
    ],
    temperature=0.9,
    top_p=0.95,
    max_tokens=500,
    stream=True
)
```

### 其他语言客户端

GenieAPIService 兼容 OpenAI API，因此可以使用任何支持 OpenAI API 的客户端库。

#### JavaScript/Node.js

```javascript
const OpenAI = require('openai');

const client = new OpenAI({
    baseURL: 'http://127.0.0.1:8910/v1',
    apiKey: '123'
});

async function chat() {
    const response = await client.chat.completions.create({
        model: 'Qwen2.0-7B',
        messages: [
            { role: 'user', content: '你好' }
        ],
        stream: true
    });

    for await (const chunk of response) {
        if (chunk.choices[0]?.delta?.content) {
            process.stdout.write(chunk.choices[0].delta.content);
        }
    }
}

chat();
```

#### Java

```java
import com.theokanning.openai.OpenAiService;
import com.theokanning.openai.completion.chat.*;

public class GenieClient {
    public static void main(String[] args) {
        OpenAiService service = new OpenAiService("123", Duration.ofSeconds(30));
        service.setBaseUrl("http://127.0.0.1:8910/v1");

        ChatCompletionRequest request = ChatCompletionRequest.builder()
            .model("Qwen2.0-7B")
            .messages(Arrays.asList(
                new ChatMessage("user", "你好")
            ))
            .build();

        service.createChatCompletion(request)
            .getChoices()
            .forEach(choice -> System.out.println(choice.getMessage().getContent()));
    }
}
```

#### Go

```go
package main

import (
    "context"
    "fmt"
    openai "github.com/sashabaranov/go-openai"
)

func main() {
    config := openai.DefaultConfig("123")
    config.BaseURL = "http://127.0.0.1:8910/v1"
    client := openai.NewClientWithConfig(config)

    resp, err := client.CreateChatCompletion(
        context.Background(),
        openai.ChatCompletionRequest{
            Model: "Qwen2.0-7B",
            Messages: []openai.ChatCompletionMessage{
                {
                    Role:    openai.ChatMessageRoleUser,
                    Content: "你好",
                },
            },
        },
    )

    if err != nil {
        fmt.Printf("Error: %v\n", err)
        return
    }

    fmt.Println(resp.Choices[0].Message.Content)
}
```
---

## 从源码构建

> 适用于需要自行编译 C++ Service / Client（Windows / Android / Linux）的开发者。若仅体验或集成 API，建议直接使用 [Release 包](https://github.com/quic/ai-engine-direct-helper/releases)。

### 源码与依赖准备

1. **克隆仓库（含子模块）**

```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

2. **VLM 额外依赖（stb）**

视觉语言模型（VLM）依赖 `stb`，请在以下目录拉取：

```bash
cd samples\genie\c++\External
git clone https://github.com/nothings/stb.git
```

### Windows 构建

#### 环境准备

- Qualcomm® AI Runtime SDK（QAIRT）
- CMake
- Visual Studio Build Tools 2022（clang、v143）
- Ninja

> 建议使用 **Command Prompt（CMD）** 而非 PowerShell 执行构建命令。

#### 设置 QAIRT SDK 路径

安装 QAIRT 后通常位于 `C:\Qualcomm\AIStack\QAIRT\2.42.0.251225`，可设置环境变量：

```bat
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225
```

#### 构建命令（ARM64 示例）

```bat
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 ..
cmake --build . --config Release --parallel 4
```

构建完成后，发布目录通常位于：`Service\GenieSerivce_v2.1.4`。

#### 可选编译开关（模型格式支持）

GenieService 默认支持 Qualcomm QNN BIN 模型；如需额外支持：

- `USE_MNN=ON`：支持 MNN 格式模型
- `USE_GGUF=ON`：支持 GGUF（llama.cpp）格式模型

在 CMake 命令末尾追加 `-D<OPTION>=ON` 启用，例如：

```bat
cmake -S .. -B . -A ARM64 -DUSE_GGUF=ON -DUSE_MNN=ON
```

### Android 构建

#### 环境准备

- Qualcomm® AI Runtime SDK（QAIRT）
- Android NDK（示例：r26d）
- Android Studio（用于构建 APK）

#### 设置环境变量与路径（Windows 主机构建 Android）

```bat
cd ai-engine-direct-helper\samples\genie\c++\Service
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%
```

#### 先构建 libappbuilder

Android 构建前需要先编译 `libappbuilder`（详见项目根目录的 [BUILD](https://github.com/quic/ai-engine-direct-helper/blob/main/BUILD.md) 说明），并将生成的 `libappbuilder.so` 放到：

`ai-engine-direct-helper\samples\genie\c++\Service`

#### 构建 GenieAPIService（Android）

```bat
"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android -j4
```

构建完成后，将依赖的 `.so` 拷贝到 `libs/arm64-v8a`：

```bat
copy "%QNN_SDK_ROOT%lib\aarch64-android\*.so" "libs\arm64-v8a" /Y
copy "obj\local\arm64-v8a\*.so" "libs\arm64-v8a" /Y
```

#### 构建 Android App（Android Studio）

1. 使用 Android Studio 打开：`ai-engine-direct-helper\samples\genie\c++\Android`
2. 菜单 **Build** → **Generate Signed App Bundle / APK** → 选择 **APK**
3. 选择签名密钥并完成构建
4. 在 `...\Android\app\release` 目录获取 `app-release.apk`
5. 安装：`adb install app-release.apk`

---

## 其他工具

这些 Windows 工具随着 [Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0) 一起发布

### encode.exe

它可以帮助你将图片或任何文件编码为 base64 格式的文件。

**示例**：将 cat.png 编码为 base64 格式数据并写入 cat.txt。

```cmd
encode.exe cat.png cat.txt
```

### decode.exe

它可以帮助您将 base64 编码的文件解码为二进制文件。

**示例**：将 base64 格式的 cat.txt 解码为二进制并写入 cat.png。

```cmd
decode.exe cat.txt cat.png
```

### wav.exe

您可能需要向 `omini` 模型输入 `.wav` 格式的音频文件，`wav.exe` 可以帮助录制您的声音！
我们采用一些额外的算法和技术来增强您的声音强度。

**示例**：输入命令后，长按 [空格] 键进行录音...

```cmd
wav.exe test.wav
```

---

## API 接口说明

GenieAPIService 提供了一套完整的 RESTful API 接口，兼容 OpenAI API 标准。

### 1. 聊天完成接口

**端点**：`POST /v1/chat/completions`

**描述**：发送聊天消息并获取模型响应。

#### 请求参数

```json
{
  "model": "Qwen2.0-7B",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "你好"}
  ],
  "stream": true,
  "temperature": 0.7,
  "top_p": 0.9,
  "top_k": 40,
  "max_tokens": 2048,
  "stop": ["</s>"],
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0
}
```

#### 参数说明

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| model | string | 是 | 模型名称 |
| messages | array | 是 | 消息列表 |
| stream | boolean | 否 | 是否流式输出（默认：false） |
| temperature | float | 否 | 温度参数（0.0-2.0，默认：0.7） |
| top_p | float | 否 | Top-p 采样（0.0-1.0，默认：0.9） |
| top_k | integer | 否 | Top-k 采样（默认：40） |
| max_tokens | integer | 否 | 最大输出 token 数（默认：2048） |
| stop | array | 否 | 停止序列 |
| presence_penalty | float | 否 | 存在惩罚（-2.0-2.0，默认：0.0） |
| frequency_penalty | float | 否 | 频率惩罚（-2.0-2.0，默认：0.0） |

#### 响应示例（非流式）

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1677652288,
  "model": "Qwen2.0-7B",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！我是一个AI助手，很高兴为您服务。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 15,
    "total_tokens": 25
  }
}
```

#### 响应示例（流式）

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}

data: [DONE]
```

#### cURL 示例

```bash
curl -X POST http://localhost:8910/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen2.0-7B",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

### 2. 模型列表接口

**端点**：`GET /v1/models`

**描述**：获取可用模型列表。

#### 请求示例

```bash
curl http://localhost:8910/v1/models
```

#### 响应示例

```json
{
  "object": "list",
  "data": [
    {
      "id": "Qwen2.0-7B",
      "object": "model",
      "created": 1677652288,
      "owned_by": "genie",
      "permission": [],
      "root": "Qwen2.0-7B",
      "parent": null
    }
  ]
}
```

### 3. 文本分割接口

**端点**：`POST /v1/textsplitter`

**描述**：将文本按照指定 `separators` 优先级进行分割。

#### 调用示例

```python
import argparse
from openai import OpenAI
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="Qwen2.0-7B-SSD", type=str)  
args = parser.parse_args()

url = "http://127.0.0.1:8910/v1/textsplitter"
text = ""   # Please enter the text to be split.
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
body = {"text": text, "max_length": 128, "separators": separators}
response = requests.post(url, json=body)
result = response.json()
result = result["content"]
print("result length:", len(result))
count = 0
for item in result:
    count += 1
    print("No.", count)
    print("text:", item["text"])
    print("length: Tokens", item["length"], "string", len(item["text"]))
    print()
```

### 4. 停止输出接口

**端点**：`POST /v1/stop`

**描述**：停止当前正在进行的生成。

#### 请求示例

```python
import requests
url = "http://127.0.0.1:8910/stop"
params = {"text": "stop"}  
response = requests.post(url, json=params)
```

### 5. 清除历史记录接口

**端点**：`POST /v1/clear`

**描述**：清除对话历史记录。

#### 请求示例

```python
import requests

url = "http://127.0.0.1:8910/clear"
params = {"text": "clear"}
response = requests.post(url, json=params)
```

### 6. 重新加载历史记录接口

**端点**：`POST /v1/reload`

**描述**：从文件重新加载历史记录。

#### 调用示例

```python
import requests
url = "http://127.0.0.1:8910/reload"
history_data = {
    "action": "import_history",
    "history": [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
    ]
}

response = requests.post(url, json=history_data)
```

### 7. 获取历史记录接口

**端点**：`GET /v1/fetch`

**描述**：获取当前对话历史记录。

#### 请求示例

```python
import requests
BASE_URL = "http://127.0.0.1:8910/fetch"
response = requests.post(BASE_URL)
print(response.text)
return response
```

### 8. 获取模型上下文大小接口

**端点**：`GET /v1/contextsize`

**描述**：获取当前模型的上下文大小。

#### 请求示例

```python
url = "http://127.0.0.1:8910/contextsize"
params = {"modelName": model_name}  #Llama2.0-7B-SSD
response = requests.post(url, json=params)
if response.status_code == 200:
    result = response.json()
    print("context大小:",result["contextsize"])
```

### 9. 获取模型性能信息接口

**端点**：`GET /v1/performance`

**描述**：获取模型推理性能信息。

#### 请求示例

```python
import requests
BASE_URL = "http://127.0.0.1:8910/profile"
response = requests.get(BASE_URL)
return response
```

### 10. 停止服务接口

**端点**：`POST /v1/shutdown`

**描述**：停止 GenieAPIService 服务。

#### 请求示例
```bash
import requests
BASE_URL = "http://127.0.0.1:8910/shutdown"
response = requests.get(BASE_URL)
return response
```
---

## 进阶示例

### Python 基本对话示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

# 流式对话
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "介绍一下量子计算"}
    ],
    stream=True,
    temperature=0.7,
    max_tokens=2048
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### 工具调用示例

GenieAPIService 支持 Function Calling，允许模型调用外部工具。

```python
from openai import OpenAI
import json

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

# 定义工具
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市的天气信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，例如：北京、上海"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "温度单位"
                    }
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "执行数学计算",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，例如：2+2、10*5"
                    }
                },
                "required": ["expression"]
            }
        }
    }
]

# 发送请求
messages = [
    {"role": "system", "content": "You are a helpful assistant with access to tools."},
    {"role": "user", "content": "北京今天天气怎么样？"}
]

response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=messages,
    tools=tools,
    tool_choice="auto"
)

# 处理工具调用
message = response.choices[0].message
if message.tool_calls:
    for tool_call in message.tool_calls:
        function_name = tool_call.function.name
        function_args = json.loads(tool_call.function.arguments)
        
        print(f"调用工具: {function_name}")
        print(f"参数: {function_args}")
        
        # 模拟工具执行
        if function_name == "get_weather":
            result = {
                "city": function_args["city"],
                "temperature": 22,
                "condition": "晴朗",
                "humidity": 45
            }
        
        # 将工具结果添加到消息中
        messages.append(message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result, ensure_ascii=False)
        })
    
    # 获取最终响应
    final_response = client.chat.completions.create(
        model="Qwen2.0-7B",
        messages=messages
    )
    
    print(f"\n最终回复: {final_response.choices[0].message.content}")
else:
    print(message.content)
```

### 视觉语言模型示例

```python
from openai import OpenAI
import base64
import os

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

def encode_image(image_path):
    """将图像编码为 base64"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# 准备图像
image_path = "test.png"
base64_image = encode_image(image_path)

# 发送请求（使用 extra_body 传递自定义消息格式）
custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": {
            "question": "请详细描述这张图片的内容",
            "image": base64_image
        }
    }
]

response = client.chat.completions.create(
    model="qwen2.5vl3b",
    messages=[{"role": "user", "content": "placeholder"}],  # 占位符
    stream=True,
    extra_body={
        "messages": custom_messages,
        "size": 4096,
        "temp": 1.5,
        "top_k": 13,
        "top_p": 0.6
    }
)

# 处理流式响应
print("模型回复：")
for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### 其他语言示例

#### JavaScript/TypeScript

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
    baseURL: 'http://127.0.0.1:8910/v1',
    apiKey: '123'
});

async function chat() {
    const response = await client.chat.completions.create({
        model: 'Qwen2.0-7B',
        messages: [
            { role: 'system', content: 'You are a helpful assistant.' },
            { role: 'user', content: '你好' }
        ],
        stream: true
    });

    for await (const chunk of response) {
        if (chunk.choices[0]?.delta?.content) {
            process.stdout.write(chunk.choices[0].delta.content);
        }
    }
    console.log();
}

chat();
```

#### C#

```csharp
using OpenAI;
using OpenAI.Chat;

var client = new OpenAIClient("123", new OpenAIClientOptions
{
    Endpoint = new Uri("http://127.0.0.1:8910/v1")
});

var chatClient = client.GetChatClient("Qwen2.0-7B");

var response = await chatClient.CompleteChatAsync(
    new ChatMessage[]
    {
        new SystemChatMessage("You are a helpful assistant."),
        new UserChatMessage("你好")
    }
);

Console.WriteLine(response.Value.Content[0].Text);
```

---

## 常见问题

### 1. 服务启动失败

**问题**：运行 `GenieAPIService.exe` 时提示找不到 DLL 文件。

**解决方案**：
- 确保已安装 [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-160)
- 检查 QAIRT SDK 是否正确安装
- 确认所有依赖的 `.dll` 文件都在同一目录下

### 2. 模型加载失败

**问题**：服务启动后提示 "Failed to load model"。

**解决方案**：
- 检查 `config.json` 文件路径是否正确
- 确认模型文件完整且未损坏
- 检查系统内存是否足够（至少 16GB）
- 查看日志文件获取详细错误信息

### 3. NPU 不可用

**问题**：服务运行在 CPU 上而不是 NPU。

**解决方案**：
- 确认设备支持 Qualcomm Snapdragon NPU
- 检查 QAIRT SDK 版本是否正确
- 在 `config.json` 中设置 `"device": "npu"`
- 更新设备驱动程序

### 4. 推理速度慢

**问题**：模型推理速度比预期慢。

**解决方案**：
- 确认模型运行在 NPU 而不是 CPU
- 检查系统资源占用情况
- 尝试减小 `context_size` 或 `max_tokens`
- 关闭不必要的后台应用程序

### 5. 流式输出不工作

**问题**：设置 `stream=true` 但没有流式输出。

**解决方案**：
- 确认客户端正确处理 SSE（Server-Sent Events）
- 检查网络连接和防火墙设置
- 尝试使用非流式模式测试服务是否正常

### 6. 多模态模型无法识别图像

**问题**：发送图像后模型无响应或报错。

**解决方案**：
- 确认图像已正确编码为 base64
- 检查图像格式是否支持（PNG、JPEG）
- 确认使用的是多模态模型（如 qwen2.5vl3b）
- 检查消息格式是否正确（需要 `{question, image}` 格式）

### 7. Android 服务自动停止

**问题**：Android 上的服务运行一段时间后自动停止。

**解决方案**：
- 在设置中允许应用后台运行
- 关闭电池优化
- 将应用添加到白名单
- 确保有足够的存储空间

### 8. 工具调用不生效

**问题**：发送 `tools` 参数但模型不调用工具。

**解决方案**：
- 确认模型支持 Function Calling
- 检查工具定义格式是否正确
- 尝试设置 `tool_choice="auto"` 或指定工具名称
- 查看模型是否需要特定的提示词格式

### 9. 历史记录丢失

**问题**：重启服务后对话历史丢失。

**解决方案**：
- 使用 `-n` 参数设置保存的历史轮数
- 定期调用 `/v1/history` 接口备份历史

### 10. 端口被占用

**问题**：启动服务时提示端口 8910 已被占用。

**解决方案**：
- 使用 `-p` 参数指定其他端口
- 检查是否有其他 GenieAPIService 实例在运行
- 使用 `netstat -ano | findstr 8910` 查找占用端口的进程

### 11. LoRA 适配器加载失败

**问题**：使用 `--adapter` 参数后服务报错。

**解决方案**：
- 确认 LoRA 文件路径正确
- 检查 LoRA 文件与基础模型兼容
- 尝试调整 `--lora_alpha` 参数
- 查看日志获取详细错误信息

### 12. 输出文本乱码

**问题**：模型输出包含乱码或特殊字符。

**解决方案**：
- 确认终端支持 UTF-8 编码
- 检查 tokenizer 文件是否正确
- 尝试使用不同的客户端测试
- 更新模型文件到最新版本

---

## 技术支持

### 获取帮助

如果您在使用 GenieAPIService 时遇到问题，可以通过以下方式获取帮助：

1. **查看文档**
   - [GitHub 仓库](https://github.com/quic/ai-engine-direct-helper)
   - [API 文档](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/docs/API.md)
   - [示例代码](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient)

2. **提交问题**
   - [GitHub Issues](https://github.com/quic/ai-engine-direct-helper/issues)

### 报告 Bug

报告 Bug 时，请提供以下信息：

1. **环境信息**
   ```
   - 操作系统：Windows 11 / Android 13 / Ubuntu 22.04
   - 设备型号：Surface Pro X / Samsung Galaxy S23
   - GenieAPIService 版本：2.1.4
   - QAIRT 版本：2.42.0
   ```

2. **问题描述**
   - 详细描述问题现象
   - 预期行为和实际行为
   - 复现步骤

3. **日志信息**
   - 启动日志
   - 错误日志
   - 相关的 API 请求和响应

4. **配置文件**
   - `config.json` 内容
   - 启动参数

### 贡献代码

欢迎为 GenieAPIService 贡献代码：

1. Fork 仓库
2. 创建特性分支
3. 提交更改
4. 推送到分支
5. 创建 Pull Request

### 许可证

GenieAPIService 使用 BSD-3-Clause 许可证。详见 [LICENSE](https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE) 文件。

### 联系方式

- **项目主页**：https://github.com/quic/ai-engine-direct-helper
- **问题反馈**：https://github.com/quic/ai-engine-direct-helper/issues

---

<div align="center">
  <p>感谢使用 GenieAPIService！</p>
  <p>如有问题或建议，欢迎在 GitHub 上与我们交流。</p>
</div>
