# GenieAPIService 模型配置详解

## 配置文件结构

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

---

## 文本模型部署

文本模型的标准目录结构：

```
models/Qwen2.0-7B-SSD/
├── config.json           # 模型配置文件
├── prompt.json           # 提示词模板
├── tokenizer.json        # 分词器
├── model-0.bin           # 模型文件
└── model-1.bin           # 模型文件
```

---

## Qwen2.5-VL-3B 多模态模型

Qwen2.5-VL-3B 是一个多模态视觉语言模型，支持图像理解和文本生成。

### 模型目录结构

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

### Windows 部署

1) 将模型目录 `qwen2.5vl3b` 放入：`models\`目录。

2) 在 `samples` 目录启动服务：

```bat
GenieAPIService.exe -c "models\qwen2.5vl3b\config.json" -l
```

3) 在当前目录准备测试用图片 `test.png`，运行客户端：

```bat
GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b
```

### Android 部署

- 模型放置到：`/sdcard/GenieModels/qwen2.5vl3b/`
- 安装并打开 GenieAPIService APK，点击 **START SERVICE** 加载模型

### Python 客户端示例

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

---

## 提示词模板配置

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
