# GenieAPIService 进阶示例

## Python 基本对话示例

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

---

## 工具调用示例

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

---

## 视觉语言模型示例

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

---

## 其他语言示例

### JavaScript/TypeScript

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

### C#

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

GitHub: https://github.com/quic/ai-engine-direct-helper