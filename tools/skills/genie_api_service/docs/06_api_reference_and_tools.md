# GenieAPIService API接口说明和工具

GenieAPIService 提供了一套完整的 RESTful API 接口，兼容 OpenAI API 标准。

## 1. 聊天完成接口

**端点**：`POST /v1/chat/completions`

**描述**：发送聊天消息并获取模型响应。

### 请求参数

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| model | string | 是 | 模型名称 |
| messages | array | 是 | 消息列表 |
| stream | boolean | 否 | 是否流式输出（默认：false） |
| temp | float | 否 | 温度参数（默认：0.3） |
| top_p | float | 否 | Top-p 采样（默认：0.8） |
| top_k | integer | 否 | Top-k 采样（默认：20） |
| size | integer | 否 | 上下文大小限制（默认：模型上下文大小） |

### Python 示例（使用 OpenAI SDK - 非流式）

```python
from openai import OpenAI

# 初始化 OpenAI 客户端，指向 GenieAPIService
client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"  # 任意字符串，GenieAPIService 不验证 key
)

response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你好"}
    ],
    stream=False,
    # 使用 extra_body 传递 Genie 特有的参数
    extra_body={
        "size": 4096,
        "temp": 0.7,
        "top_p": 0.9,
        "top_k": 40
    }
)

print(response.choices[0].message.content)
```

### Python 示例（使用 OpenAI SDK - 流式）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "写一首诗"}
    ],
    stream=True,
    extra_body={
        "size": 4096,
        "temp": 0.7,
        "top_p": 0.9,
        "top_k": 40
    }
)

for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

---

## 2. 模型列表接口

**端点**：`GET /v1/models`

**描述**：获取可用模型列表。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/v1/models"
response = requests.get(url)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))
```

---

## 3. 文本分割接口

**端点**：`POST /v1/textsplitter`

**描述**：将文本按照指定 `separators` 优先级进行分割。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/v1/textsplitter"
text = "你好！这是一个测试文本。"
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
payload = {"text": text, "max_length": 10, "separators": separators}
response = requests.post(url, json=payload)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))
```

---

## 4. 停止输出接口

**端点**：`POST /stop`

**描述**：停止当前正在进行的生成。

### Python 示例

```python
import requests

url = "http://localhost:8910/stop"
payload = {"text": "stop"}
response = requests.post(url, json=payload)
print(f"Status Code: {response.status_code}")
```

---

## 5. 清除历史记录接口

**端点**：`POST /clear`

**描述**：清除对话历史记录。

### Python 示例

```python
import requests

url = "http://localhost:8910/clear"
payload = {"text": "clear"}
response = requests.post(url, json=payload)
print(f"Status Code: {response.status_code}")
```

---

## 6. 重新加载历史记录接口

**端点**：`POST /reload`

**描述**：从 JSON 数据重新加载历史记录。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/reload"
# 假设 history_data 是从 /fetch 接口导出的 JSON 列表
history_data = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！"}
]
response = requests.post(url, json=history_data)
print(f"Status Code: {response.status_code}")
```

---

## 7. 获取历史记录接口

**端点**：`POST /fetch`

**描述**：获取当前对话历史记录。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/fetch"
response = requests.post(url)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))
```

---

## 8. 获取模型上下文大小接口

**端点**：`POST /contextsize`

**描述**：获取当前模型的上下文大小。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/contextsize"
response = requests.post(url)
print(json.dumps(response.json(), indent=2))
```

---

## 9. 获取模型性能信息接口

**端点**：`GET /profile`

**描述**：获取模型推理性能信息。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/profile"
response = requests.get(url)
print(json.dumps(response.json(), indent=2))
```

---

## 10. 获取模型状态接口

**端点**：`GET /status`

**描述**：获取模型加载状态。

### Python 示例

```python
import requests
import json

url = "http://localhost:8910/status"
response = requests.get(url)
print(json.dumps(response.json(), indent=2))
```

---

## 11. 卸载模型接口

**端点**：`POST /unload`

**描述**：卸载当前加载的模型。

### Python 示例

```python
import requests

url = "http://localhost:8910/unload"
response = requests.post(url)
print(f"Status Code: {response.status_code}")
```

---

## 12. 停止服务接口

**端点**：`POST /servicestop`

**描述**：停止 GenieAPIService 服务。

### Python 示例

```python
import requests

url = "http://localhost:8910/servicestop"
payload = {"text": "stop"}
response = requests.post(url, json=payload)
print(f"Status Code: {response.status_code}")
```

---

# 其他工具

这些 Windows 工具随着 [Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0) 一起发布

## encode.exe

将图片或任何文件编码为 base64 格式。

**示例**：
```cmd
encode.exe cat.png cat.txt
```

## decode.exe

将 base64 编码的文件解码为二进制文件。

**示例**：
```cmd
decode.exe cat.txt cat.png
```

## wav.exe

录制音频文件（.wav 格式）。

**示例**：
```cmd
wav.exe test.wav
```
输入命令后，长按 [空格] 键进行录音。

GitHub: https://github.com/quic/ai-engine-direct-helper