# GenieAPIService 服务使用指南

## 启动服务

基本启动命令：

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

## 完整参数列表

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

## 使用示例

### 基本启动

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

### 自定义端口和日志级别

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -p 9000 -d 4
```

### 启用思考模式

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -t
```

### 使用 LoRA 适配器

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l --adapter my_lora --lora_alpha 0.8
```

## 服务状态检查

服务启动后，可以通过以下方式检查状态：

1. **查看控制台输出**
   ```
   GenieAPIService: 2.1.4, Genie Library: 1.14.0
   Server listening on port 8910
   Model loaded successfully
   ```

2. **查看模型列表**
   ```bash
   curl http://localhost:8910/v1/models
   ```

---

## C++ 客户端使用

GenieAPIClient 是一个 C++ 命令行客户端，用于与 GenieAPIService 交互。

### 基本用法

```cmd
GenieAPIClient.exe --model "qwen2.5vl3b" --prompt "你好，请介绍一下自己" --stream
```

### 完整参数列表

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

### 使用示例

[参考代码](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient/GenieAPIClient.cpp)

#### 文本对话

```cmd
GenieAPIClient.exe --prompt "解释一下量子计算的原理" --stream
```

#### 多模态对话

```cmd
GenieAPIClient.exe --prompt "这张图片里有什么？" --img test.png --stream --model qwen2.5vl3b
```

#### 自定义参数

```cmd
GenieAPIClient.exe --prompt "写一首诗" --stream --temperature 0.9 --max_tokens 500
```

---

## Python 客户端使用

[参考代码](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient/GenieAPIClient.py)

Python 客户端使用 OpenAI SDK，提供更灵活的集成方式。

### 安装依赖

```bash
pip install openai
```

### 基本示例

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

### 非流式模式

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

### 自定义参数

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

GitHub: https://github.com/quic/ai-engine-direct-helper