# GenieAPIService API接口说明和工具

GenieAPIService 提供了一套完整的 RESTful API 接口，兼容 OpenAI API 标准。

## 1. 聊天完成接口

**端点**：`POST /v1/chat/completions`

**描述**：发送聊天消息并获取模型响应。

### 请求参数

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
  "max_tokens": 2048
}
```

### 参数说明

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| model | string | 是 | 模型名称 |
| messages | array | 是 | 消息列表 |
| stream | boolean | 否 | 是否流式输出（默认：false） |
| temperature | float | 否 | 温度参数（0.0-2.0，默认：0.7） |
| top_p | float | 否 | Top-p 采样（0.0-1.0，默认：0.9） |
| top_k | integer | 否 | Top-k 采样（默认：40） |
| max_tokens | integer | 否 | 最大输出 token 数（默认：2048） |

### 响应示例（非流式）

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

### cURL 示例

```bash
curl -X POST http://localhost:8910/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen2.0-7B",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

---

## 2. 模型列表接口

**端点**：`GET /v1/models`

**描述**：获取可用模型列表。

### 请求示例

```bash
curl http://localhost:8910/v1/models
```

---

## 3. 文本分割接口

**端点**：`POST /v1/textsplitter`

**描述**：将文本按照指定 `separators` 优先级进行分割。

### 调用示例

```python
import requests

url = "http://127.0.0.1:8910/v1/textsplitter"
text = ""   # 要分割的文本
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
body = {"text": text, "max_length": 128, "separators": separators}
response = requests.post(url, json=body)
```

---

## 4. 停止输出接口

**端点**：`POST /v1/stop`

**描述**：停止当前正在进行的生成。

---

## 5. 清除历史记录接口

**端点**：`POST /v1/clear`

**描述**：清除对话历史记录。

---

## 6. 重新加载历史记录接口

**端点**：`POST /v1/reload`

**描述**：从文件重新加载历史记录。

---

## 7. 获取历史记录接口

**端点**：`GET /v1/fetch`

**描述**：获取当前对话历史记录。

---

## 8. 获取模型上下文大小接口

**端点**：`GET /v1/contextsize`

**描述**：获取当前模型的上下文大小。

---

## 9. 获取模型性能信息接口

**端点**：`GET /v1/performance`

**描述**：获取模型推理性能信息。

---

## 10. 停止服务接口

**端点**：`POST /v1/shutdown`

**描述**：停止 GenieAPIService 服务。

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