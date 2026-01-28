# GenieAPIService 用户使用手册

<div align="center">
  <h2>在本地 NPU 上运行大语言模型</h2>
  <p><i>OpenAI 兼容 API 服务 (C++)</i></p>
</div>

---

## 目录

1. [简介](#简介)
2. [系统要求](#系统要求)
3. [功能特性](#功能特性)
4. [Windows 平台部署](#windows-平台部署)
5. [Android 平台部署](#android-平台部署)
6. [模型配置](#模型配置)
7. [服务使用](#服务使用)
8. [客户端使用](#客户端使用)
9. [API 接口说明](#api-接口说明)
10. [Python 客户端示例](#python-客户端示例)
11. [常见问题](#常见问题)
12. [技术支持](#技术支持)

---

## 简介

GenieAPIService 是一个基于 C++ 开发的 OpenAI 兼容 API 服务，可以在 Windows on Snapdragon (WoS)、移动设备和 Linux 平台上运行。该服务允许开发者在本地设备的 NPU（神经处理单元）或 CPU 上运行大语言模型，无需依赖云端服务。

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
  - Visual C++ Redistributable

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
- ✅ **自定义模型**：支持用户自定义模型配置
- ✅ **文本分割**：内置文本分割功能，处理长文本输入
- ✅ **工具调用**：支持 Function Calling 功能
- ✅ **思考模式**：支持启用/禁用思考模式
- ✅ **LoRA 支持**：支持 LoRA 适配器
- ✅ **历史记录**：支持对话历史记录功能
- ✅ **多模态支持**：支持视觉语言模型（VLM）

### 支持的模型格式

- **BIN 格式**：Qualcomm QNN 格式模型（默认）
- **MNN 格式**：阿里巴巴 MNN 框架模型（需编译时启用）
- **GGUF 格式**：llama.cpp 格式模型（需编译时启用）

---

## Windows 平台部署

### 步骤 1：下载资源

1. **下载 GenieAPIService**
   - 访问 [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases)
   - 下载 `GenieAPIService_v2.1.3_QAIRT_v2.42.0_v73.zip`

2. **下载模型文件**
   - 根据需要下载对应的模型文件
   - 常见模型：Qwen2.0-7B、IBM-Granite-v3.1-8B、Qwen2.5-VL-3B 等

3. **安装 Qualcomm® AI Runtime SDK**
   - 下载并安装 QAIRT SDK
   - 默认安装路径：`C:\Qualcomm\AIStack\QAIRT\2.42.0.251225\`

### 步骤 2：解压和配置

1. **解压 GenieAPIService**
   ```
   解压 GenieAPIService_v2.1.3_QAIRT_v2.42.0_v73.zip 到目标目录
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

参数说明：
- `-c, --config_file`：配置文件路径（必需）
- `-l, --load_model`：启动时加载模型
- `-d, --loglevel`：日志级别（1:Error, 2:Warning, 3:Info, 4:Debug, 5:Verbose）
- `-n, --num_response`：保存的历史对话轮数
- `-o, --min_output_num`：最小输出 token 数量
- `-p, --port`：服务端口（默认 8910）
- `-t, --enable_thinking`：启用思考模式
- `-a, --all_text`：输出所有文本（包括工具调用文本）
- `--adapter`：LoRA 适配器名称
- `--lora_alpha`：LoRA Alpha 值

### 步骤 4：验证服务

服务启动成功后，会显示类似以下信息：

```
GenieAPIService: 2.1.3, Genie Library: x.x.x
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
   - 访问 [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases)
   - 下载 `GenieAPIService.apk`

2. **安装 APK**
   ```bash
   adb install GenieAPIService.apk
   ```
   或直接在设备上安装

### 步骤 2：准备模型文件

1. **创建模型目录**
   ```bash
   adb shell mkdir -p /sdcard/GenieModels
   ```

2. **推送模型文件**
   ```bash
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

1. **打开 GenieAPI 应用**
   - 在设备上找到并打开 GenieAPI 应用

2. **启动服务**
   - 点击 `START SERVICE` 按钮
   - 等待模型加载完成
   - 看到 "Genie API Service IS Running." 表示服务已启动

3. **配置后台运行**（重要）
   - 进入设备设置 → 电池 → 省电设置 → 应用电池管理
   - 找到 GenieAPI 应用
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

2. **GenieFletUI**
   - 源码位置：`samples/fletui/GenieFletUI/android`
   - 使用 Android Studio 编译安装

---

## 模型配置

### 配置文件结构

每个模型需要一个 `config.json` 配置文件，示例：

```json
{
  "model_name": "Qwen2.0-7B-SSD",
  "model_path": "models/Qwen2.0-7B-SSD",
  "context_size": 4096,
  "query_type": "qnn",
  "prompt_file": "prompt.json"
}
```

### 文本模型部署

文本模型的标准目录结构：

```
models/Qwen2.0-7B-SSD/
├── config.json           # 模型配置文件
├── prompt.json          # 提示词模板
├── tokenizer.json       # 分词器
├── model-0.bin          # 模型文件
├── model-1.bin          # 模型文件（如有）
└── embedding_weights.raw # 嵌入权重（如需要）
```

### 视觉语言模型部署

#### Qwen2.5-VL-3B 模型

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

#### Phi-4 多模态模型

```
models/phi4mm/
├── config.json
├── embedding_weights_200064x3072.raw
├── prompt.json
├── tokenizer.json
├── veg.serialized.bin
├── weights_sharing_model_1_of_2.serialized.bin
├── weights_sharing_model_2_of_2.serialized.bin
└── raw/
    ├── attention_mask.bin
    └── position_ids.bin
```

### 提示词模板配置

`prompt.json` 文件定义了模型的提示词格式：

```json
{
  "system_prompt": "<|im_start|>system\n{system}<|im_end|>\n",
  "user_prompt": "<|im_start|>user\n{prompt}<|im_end|>\n",
  "assistant_prompt": "<|im_start|>assistant\n",
  "stop_tokens": ["<|im_end|>", "<|endoftext|>"]
}
```

---

## 服务使用

### 启动服务

#### 基本启动

```bash
# Windows
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l

# Linux
./GenieAPIService -c models/Qwen2.0-7B-SSD/config.json -l
```

#### 高级配置启动

```bash
GenieAPIService.exe \
  -c models/Qwen2.0-7B-SSD/config.json \
  -l \
  -d 3 \
  -n 10 \
  -o 1024 \
  -p 8910 \
  -f service.log
```

### 服务端口

默认端口：`8910`

可以通过 `-p` 参数修改：
```bash
GenieAPIService.exe -c config.json -l -p 9000
```

### 日志配置

#### 日志级别

- `1`：Error（仅错误）
- `2`：Warning（警告及以上）
- `3`：Info（信息及以上）
- `4`：Debug（调试及以上）
- `5`：Verbose（详细信息）

#### 日志文件

```bash
# 指定日志文件
GenieAPIService.exe -c config.json -l -d 3 -f service.log
```

### 历史记录功能

启用历史记录功能可以让模型记住之前的对话：

```bash
# 保存最近 10 轮对话
GenieAPIService.exe -c config.json -l -n 10
```

**注意事项**：
- 历史记录会占用上下文长度
- 输入长度 + 历史记录长度 + 输出长度 不能超过模型的最大上下文长度
- 可以通过 API 清除或重新加载历史记录

### 思考模式

某些模型支持思考模式（如 DeepSeek-R1）：

```bash
# 启用思考模式
GenieAPIService.exe -c config.json -l -t
```

**建议**：使用工具调用功能时，建议禁用思考模式。

### LoRA 支持

使用 LoRA 适配器：

```bash
GenieAPIService.exe -c config.json -l --adapter my_adapter --lora_alpha 0.5
```

---

## 客户端使用

### C++ 客户端

GenieAPIService 提供了 C++ 客户端示例。

#### 文本模型查询

```bash
GenieAPIClient.exe \
  --prompt "如何学习编程？" \
  --stream \
  --model Qwen2.0-7B-SSD \
  --ip 127.0.0.1
```

#### 视觉语言模型查询

```bash
GenieAPIClient.exe \
  --prompt "这张图片描述了什么？" \
  --img test.png \
  --stream \
  --model qwen2.5vl3b \
  --ip 127.0.0.1
```

#### 客户端参数说明

- `--prompt`：用户问题（必需）
- `--system`：系统提示词（默认："You are a helpful assistant."）
- `--stream`：启用流式输出
- `--model`：模型名称（必需）
- `--ip`：服务器 IP 地址（默认：127.0.0.1）
- `--img`：图片路径（仅用于 VLM 模型）

### Python 客户端

使用 OpenAI Python SDK 连接服务：

```python
from openai import OpenAI

# 创建客户端
client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"  # 任意字符串即可
)

# 发送请求
response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    messages=[
        {"role": "system", "content": "你是一个有帮助的助手。"},
        {"role": "user", "content": "如何学习编程？"}
    ],
    stream=True
)

# 处理流式响应
for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

### 其他语言客户端

由于 GenieAPIService 兼容 OpenAI API，您可以使用任何支持 OpenAI API 的客户端库：

- **JavaScript/TypeScript**：`openai` npm 包
- **Java**：OpenAI Java SDK
- **Go**：go-openai
- **Rust**：async-openai
- **PHP**：openai-php

只需将 `base_url` 设置为 GenieAPIService 的地址即可。

---

## API 接口说明

GenieAPIService 提供了多个 HTTP API 接口。

### 1. 聊天完成接口

**端点**：`POST /v1/chat/completions`

**请求示例**：

```json
{
  "model": "Qwen2.0-7B-SSD",
  "messages": [
    {"role": "system", "content": "你是一个有帮助的助手。"},
    {"role": "user", "content": "如何学习编程？"}
  ],
  "stream": true,
  "size": 4096,
  "temp": 1.5,
  "top_k": 13,
  "top_p": 0.6,
  "seed": 146
}
```

**参数说明**：

- `model`：模型名称（必需）
- `messages`：消息列表（必需）
- `stream`：是否流式输出（默认：false）
- `size`：最大输出 token 数（默认：4096）
- `temp`：温度参数（默认：1.0）
- `top_k`：Top-K 采样（默认：40）
- `top_p`：Top-P 采样（默认：0.9）
- `seed`：随机种子（可选）

**响应示例（非流式）**：

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1677652288,
  "model": "Qwen2.0-7B-SSD",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "学习编程的建议..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 100,
    "total_tokens": 120
  }
}
```

**响应示例（流式）**：

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B-SSD","choices":[{"index":0,"delta":{"content":"学"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B-SSD","choices":[{"index":0,"delta":{"content":"习"},"finish_reason":null}]}

data: [DONE]
```

### 2. 模型列表接口

**端点**：`GET /v1/models`

**响应示例**：

```json
{
  "object": "list",
  "data": [
    {
      "id": "Qwen2.0-7B-SSD",
      "object": "model",
      "created": 1677652288,
      "owned_by": "system"
    },
    {
      "id": "qwen2.5vl3b",
      "object": "model",
      "created": 1677652288,
      "owned_by": "system"
    }
  ]
}
```

### 3. 文本分割接口

**端点**：`POST /v1/textsplitter`

**请求示例**：

```json
{
  "text": "这是一段很长的文本...",
  "max_length": 128,
  "separators": ["\n\n", "\n", "。", "！", "？", "，", " ", ""]
}
```

**参数说明**：

- `text`：要分割的文本（必需）
- `max_length`：每段的最大 token 数（必需）
- `separators`：分隔符优先级列表（必需）

**响应示例**：

```json
{
  "content": [
    {
      "text": "第一段文本...",
      "length": 120
    },
    {
      "text": "第二段文本...",
      "length": 115
    }
  ]
}
```

### 4. 停止输出接口

**端点**：`POST /stop`

**请求示例**：

```json
{
  "text": "stop"
}
```

**功能**：立即停止当前的模型输出。

### 5. 清除历史记录接口

**端点**：`POST /clear`

**请求示例**：

```json
{
  "text": "clear"
}
```

**功能**：清除服务器保存的对话历史记录。

### 6. 重新加载历史记录接口

**端点**：`POST /reload`

**请求示例**：

```json
{
  "action": "import_history",
  "history": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮助你的吗？"},
    {"role": "user", "content": "介绍一下北京"},
    {"role": "assistant", "content": "北京是中国的首都..."}
  ]
}
```

**功能**：从客户端上传历史对话记录到服务器。

### 7. 获取历史记录接口

**端点**：`POST /fetch`

**响应示例**：

```json
{
  "history": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮助你的吗？"}
  ]
}
```

### 8. 获取模型上下文大小接口

**端点**：`POST /contextsize`

**请求示例**：

```json
{
  "modelName": "Qwen2.0-7B-SSD"
}
```

**响应示例**：

```json
{
  "contextsize": 4096
}
```

### 9. 获取模型性能信息接口

**端点**：`GET /profile`

**响应示例**：

```json
{
  "model": "Qwen2.0-7B-SSD",
  "tokens_per_second": 25.5,
  "memory_usage": "4.2GB"
}
```

### 10. 停止服务接口

**端点**：`POST /servicestop`

**请求示例**：

```json
{
  "text": "stop"
}
```

**功能**：终止 GenieAPIService 服务进程。

---

## Python 客户端示例

### 基本聊天示例

```python
from openai import OpenAI

# 配置客户端
BASE_URL = "http://127.0.0.1:8910/v1"
client = OpenAI(base_url=BASE_URL, api_key="123")

# 发送消息
messages = [
    {"role": "system", "content": "你是一个有帮助的助手。"},
    {"role": "user", "content": "如何学习编程？"}
]

# 流式输出
response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    stream=True,
    messages=messages
)

for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

### 非流式输出示例

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8910/v1", api_key="123")

response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    messages=[
        {"role": "system", "content": "你是一个数学老师。"},
        {"role": "user", "content": "什么是勾股定理？"}
    ]
)

print(response.choices[0].message.content)
```

### 工具调用示例

```python
from openai import OpenAI
import json

client = OpenAI(base_url="http://127.0.0.1:8910/v1", api_key="123")

# 定义工具
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "获取指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名称，例如：北京"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "温度单位"
                    }
                },
                "required": ["location", "unit"]
            }
        }
    }
]

# 发送请求
response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    messages=[
        {"role": "user", "content": "北京今天天气怎么样？"}
    ],
    tools=tools,
    tool_choice="auto"
)

# 处理工具调用
message = response.choices[0].message
if message.tool_calls:
    for tool_call in message.tool_calls:
        function_name = tool_call.function.name
        function_args = json.loads(tool_call.function.arguments)
        
        # 调用实际的函数
        if function_name == "get_current_weather":
            result = get_current_weather(
                location=function_args["location"],
                unit=function_args["unit"]
            )
            
            # 将结果返回给模型
            messages.append(message)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            })
            
            # 获取最终响应
            final_response = client.chat.completions.create(
                model="Qwen2.0-7B-SSD",
                messages=messages
            )
            print(final_response.choices[0].message.content)
```

### 文本分割示例

```python
import requests

url = "http://127.0.0.1:8910/v1/textsplitter"

text = """
这是一段很长的文本内容...
需要根据 token 数量进行分割...
"""

separators = ["\n\n", "\n", "。", "！", "？", "，", " ", ""]

body = {
    "text": text,
    "max_length": 128,
    "separators": separators
}

response = requests.post(url, json=body)
result = response.json()

for i, item in enumerate(result["content"], 1):
    print(f"段落 {i}:")
    print(f"文本: {item['text']}")
    print(f"Token 数: {item['length']}")
    print()
```

### 历史记录管理示例

```python
import requests

BASE_URL = "http://127.0.0.1:8910"

# 清除历史记录
def clear_history():
    url = f"{BASE_URL}/clear"
    response = requests.post(url, json={"text": "clear"})
    return response.status_code == 200

# 重新加载历史记录
def reload_history(history_list):
    url = f"{BASE_URL}/reload"
    history_data = {
        "action": "import_history",
        "history": history_list
    }
    response = requests.post(url, json=history_data)
    return response.status_code == 200

# 获取历史记录
def fetch_history():
    url = f"{BASE_URL}/fetch"
    response = requests.post(url)
    return response.json()

# 使用示例
if __name__ == "__main__":
    # 清除历史
    clear_history()
    
    # 加载新的历史
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么可以帮助你的吗？"},
        {"role": "user", "content": "介绍一下北京"},
        {"role": "assistant", "content": "北京是中国的首都..."}
    ]
    reload_history(history)
    
    # 获取当前历史
    current_history = fetch_history()
    print(current_history)
```

### 视觉语言模型示例

```python
from openai import OpenAI
import base64

client = OpenAI(base_url="http://127.0.0.1:8910/v1", api_key="123")

# 读取并编码图片
def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# 编码图片
image_base64 = encode_image("test.png")

# 构建消息
messages = [
    {
        "role": "user",
        "content": {
            "question": "这张图片描述了什么？",
            "image": image_base64
        }
    }
]

# 发送请求
response = client.chat.completions.create(
    model="qwen2.5vl3b",
    messages=messages,
    stream=True
)

# 处理响应
for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

---

## 常见问题

### 1. 服务启动失败

**问题**：运行 `GenieAPIService.exe` 时提示找不到 DLL 文件。

**解决方案**：
- 确保已安装 Qualcomm® AI Runtime SDK
- 检查系统环境变量 `QNN_SDK_ROOT` 是否正确设置
- 安装 Visual C++ Redistributable

### 2. 模型加载失败

**问题**：服务启动后提示 "Failed to load model"。

**解决方案**：
- 检查配置文件路径是否正确
- 确认模型文件完整且未损坏
- 检查模型目录结构是否符合要求
- 查看日志文件获取详细错误信息

### 3. 端口被占用

**问题**：启动服务时提示 "service already exist"。

**解决方案**：
- 检查是否已有 GenieAPIService 实例在运行
- 使用 `-p` 参数指定其他端口
- Windows: 使用 `netstat -ano | findstr 8910` 查找占用端口的进程
- 终止占用端口的进程或重启系统

### 4. 输入长度超限

**问题**：发送请求时提示输入长度超过限制。

**解决方案**：
- 使用文本分割接口将长文本分段
- 增加 `--min_output_num` 参数值以减少输出预留空间
- 使用支持更大上下文的模型
- 公式：输入长度 + 历史记录长度 + 最小输出长度 ≤ 模型上下文大小

### 5. Android 服务被杀死

**问题**：Android 设备上服务运行一段时间后自动停止。

**解决方案**：
- 进入设置 → 电池 → 省电设置 → 应用电池管理
- 找到 GenieAPI 应用
- 选择 "允许后台活动"
- 关闭电池优化

### 6. 响应速度慢

**问题**：模型推理速度很慢。

**解决方案**：
- 确认设备支持 NPU 且已正确配置
- 检查是否使用了正确的模型格式（BIN 格式性能最佳）
- 减小 `size` 参数值以限制输出长度
- 关闭不必要的后台应用释放资源
- 使用更小的模型（如 3B 而非 7B）

### 7. 工具调用不工作

**问题**：模型不调用定义的工具。

**解决方案**：
- 确认使用的模型支持工具调用功能
- 禁用思考模式（不要使用 `-t` 参数）
- 检查工具定义格式是否正确
- 使用 `-a` 参数输出所有文本以调试

### 8. 历史记录不生效

**问题**：启用历史记录后模型仍然不记得之前的对话。

**解决方案**：
- 确认启动时使用了 `-n` 参数
- 检查历史记录是否被清除
- 使用 `/fetch` 接口查看当前历史记录
- 确保历史记录格式正确

### 9. 视觉语言模型无法识别图片

**问题**：VLM 模型返回错误或无法理解图片内容。

**解决方案**：
- 确认图片已正确 Base64 编码
- 检查图片格式是否支持（推荐 PNG、JPEG）
- 确认模型文件完整，特别是 `veg.serialized.bin`
- 检查图片大小是否合理（建议不超过 2MB）

### 10. 内存不足

**问题**：运行时提示内存不足或系统变慢。

**解决方案**：
- 使用更小的模型
- 减少 `--num_response` 参数值
- 关闭其他占用内存的应用
- 增加设备物理内存
- 使用量化程度更高的模型

---

## 技术支持

### 官方资源

- **GitHub 仓库**：https://github.com/quic/ai-engine-direct-helper
- **问题反馈**：https://github.com/quic/ai-engine-direct-helper/issues
- **发布页面**：https://github.com/quic/ai-engine-direct-helper/releases

### 文档资源

- **API 文档**：[docs/API.md](docs/API.md)
- **构建文档**：[docs/BUILD.md](docs/BUILD.md)
- **使用文档**：[docs/USAGE.MD](docs/USAGE.MD)
- **VLM 部署文档**：[docs/VLM_DEPLOYMENT.MD](docs/VLM_DEPLOYMENT.MD)

### 社区支持

- 在 GitHub Issues 中搜索类似问题
- 查看示例代码：`samples/python/` 目录
- 参考测试代码：`GenieAPIService/test/genietest.py`

### 联系方式

如需技术支持，请：
1. 查看本手册的常见问题部分
2. 搜索 GitHub Issues
3. 在 GitHub 上创建新的 Issue，提供详细的错误信息和日志

---

## 免责声明

本软件按"原样"提供，不提供任何明示或暗示的保证。作者和贡献者不对因使用本软件而产生的任何损害承担责任。代码可能不完整或测试不充分。用户需自行评估其适用性并承担所有相关风险。

**注意**：欢迎贡献代码。在关键系统中部署前请确保进行充分测试。

---

## 版本信息

- **GenieAPIService 版本**：2.1.3
- **QAIRT SDK 版本**：2.42.0
- **文档版本**：1.0
- **最后更新**：2026-01-28

---

## 附录

### A. 支持的模型列表

- Qwen2.0-7B-SSD
- Qwen2.5-VL-3B
- IBM-Granite-v3.1-8B
- Phi-4 多模态
- Llama 系列（需 GGUF 格式支持）
- 其他兼容模型

### B. 性能优化建议

1. **使用 NPU 加速**：确保使用 QNN 格式模型
2. **合理设置上下文长度**：不要超过模型支持的最大值
3. **优化批处理**：对于多个请求，考虑批量处理
4. **缓存管理**：合理使用历史记录功能
5. **资源监控**：定期检查内存和 CPU 使用情况

### C. 安全建议

1. **网络安全**：不要将服务直接暴露到公网
2. **访问控制**：在生产环境中添加身份验证
3. **数据隐私**：敏感数据在本地处理，不上传云端
4. **定期更新**：及时更新到最新版本以获得安全修复

### D. 开发建议

1. **错误处理**：客户端应实现完善的错误处理机制
2. **超时设置**：设置合理的请求超时时间
3. **重试机制**：实现指数退避的重试策略
4. **日志记录**：记录关键操作和错误信息
5. **测试**：在部署前进行充分的功能和性能测试

---

**感谢使用 GenieAPIService！**

如有任何问题或建议，欢迎访问我们的 GitHub 仓库。
