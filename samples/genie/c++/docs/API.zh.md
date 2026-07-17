# GenieAPIService API <br>

## 聊天补全（Chat Completions）
这是**核心推理接口**，与 OpenAI 兼容。它注册了四个等效的别名，因此以下任意路径都可以正常工作：
```
POST /completions
POST /v1/completions
POST /chat/completions
POST /v1/chat/completions
```

### 通过 `model` 字段进行模型路由
请求体中的 `model` 字段用于选择由哪个已加载的模型来处理该请求：
- 如果 `model` 与某个已加载模型的名称匹配（参见[获取模型名称列表](#获取模型名称列表get-modelname-list)），则直接使用该模型。
- 如果省略 `model`，则使用服务器当前的默认模型。
- 如果设置了 `model` 但它与当前任何已加载的模型都不匹配，服务器会尝试进行**动态切换**：它会卸载当前占用同一设备（NPU/GPU/CPU）的模型，并按需加载所请求的模型，然后处理该请求。这只有在该模型确实能在配置的模型根目录下的磁盘中找到时才有效；否则请求会失败。
- 一些代理前端会在 `model` 前添加 `local::` 前缀，路由时会自动去除该前缀。

只有当服务器上有多个可用模型时，这种路由才有意义——关于如何通过 `service_config.json` 配置多个模型，请参见 [MODEL_DEPLOYMENT.zh.md](MODEL_DEPLOYMENT.zh.md)。

### `messages` —— 支持两种格式
每条消息的 `content` 字段可以接受**以下两种形式之一**——不同请求之间可以混用不同的风格，但单条消息的 `content` 必须使用其中一种：

1. **标准 OpenAI 数组格式**（推荐，兼容性最好）：
   ```json
   {"role": "user", "content": [
       {"type": "text", "text": "What is in this image?"},
       {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<...>"}},
       {"type": "input_audio", "input_audio": {"data": "<base64-encoded audio>"}}
   ]}
   ```
2. **GenieAPIClient 风格的扁平对象**（一种更简单的非标准简写形式，服务器同样支持）：
   ```json
   {"role": "user", "content": {"question": "What is in this image?", "image": "<base64 or data URL>", "audio": "<base64>"}}
   ```

`image_url`/`image` 和 `input_audio`/`audio` 都是可选的——纯文本聊天时可以省略它们。多模态输入只对支持该功能的模型有意义（`qwen2.5vl*`、`qwen2.5_omini*` 以及 `phi4*` 系列模型）；MNN 和 GGUF 模型不支持多模态输入。

### `stream` —— 可选，默认为 `false`
将 `"stream"` 设为 `true` 可以以 `text/event-stream` 服务器推送事件（Server-Sent Events）的形式接收响应，其增量 `delta` 分块格式与 OpenAI 流式 API 所使用的格式相同。省略该字段（或设为 `false`）则会得到一个完整的单条 JSON 响应。

### 最简示例
```python
import requests

url = "http://127.0.0.1:8910/v1/chat/completions"
body = {
    "model": "qwen3-8b-8480",   # optional; omit to use the current default model
    "messages": [
        {"role": "user", "content": "Hello, who are you?"}
    ],
    "stream": False,
}
response = requests.post(url, json=body)
print(response.json())
```

---

## 文本分割器（Text Splitter）
该功能可以按照指定分隔符的优先顺序以及每个段落的最大长度，将一段长文本切分为多个段落。长度按 token 数量而非文本长度计算。你也可以使用该功能来计算文本的 token 数量。 <br>
该接口注册了两个等效别名：`POST /textsplitter` 和 `POST /v1/textsplitter`。<br>
你可以获取如何使用文本分割器的示例代码

```
import argparse
from openai import OpenAI
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="Qwen2.0-7B-SSD", type=str)  
args = parser.parse_args()

url = "http://127.0.0.1:8910/v1/textsplitter"
text = ""   # Please enter the text to be split.
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
body = {"text": text, "max_length": 128, "separators": separators, "model": args.model}
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

## 终止输出（Terminate output）
该功能用于终止模型当前的输出。<br>
你可以选择传入 `model` 字段以指定某个已加载的模型；如果省略，则针对当前默认模型执行操作。<br>
你可以获取如何使用该功能的示例代码。
```
import requests
url = "http://127.0.0.1:8910/stop"
params = {"text": "stop", "model": "qwen3-8b-8480"}  # "model" is optional
response = requests.post(url, json=params)
```

## 聊天历史相关接口 —— `/clear`、`/reload`、`/fetch`
> **关于当前行为的说明：** 聊天历史现在是由每个 `/v1/chat/completions` 请求中的 `messages` 数组即时重新构建的（服务器不会在多次请求之间保留任何服务器端会话状态）。因此，这三个历史遗留接口不再具有其名称所暗示的功能——具体的当前行为请参见下面各自的说明。如果你需要多轮上下文，请在每次请求中把完整的对话内容作为 `messages` 数组发送，而不要依赖这些接口。

### `POST /clear`
目前是一个**空操作（no-op）**：它总是返回 `200 OK` 和空的响应体，且不会在服务器端清除任何内容（因为服务器端本来就没有需要清除的历史记录）。
```
import requests

url = "http://127.0.0.1:8910/clear"
params = {"text": "clear"}
response = requests.post(url, json=params)
```

### `POST /reload`
仅**校验**给定的 `history` 数组的 JSON 结构（每一项必须包含字符串类型的 `role`——取值为 `user`/`assistant`/`tool` 之一——以及字符串类型的 `content`）；如果有效则返回 `200 OK` 及 `{"status": "ok"}`，否则返回 `400` 及描述性的 `error`。**它并不会将历史记录持久化或实际加载到任何地方**——一旦请求处理完成，已校验的数据就会被丢弃。如果你想使用对话历史，请直接将其包含在 `/v1/chat/completions` 请求的 `messages` 数组中。
```
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

### `POST /fetch`
无论此前向 `/reload` 或任何聊天请求发送过什么内容，都始终返回**硬编码的空历史记录**：`{"history": []}`。
```
import requests
BASE_URL = "http://127.0.0.1:8910/fetch"
response = requests.post(BASE_URL)
print(response.text)
```

## 获取模型名称列表（Get modelname list）
获取服务器当前已知的模型列表——即模型根目录下所有包含 `config.json` 的子目录，无论其当前是否已加载。<br>
`data` 中的每一项都包含 `id`（模型名称），并且只有对**当前已加载**的模型，才会包含以下额外字段：`is_loaded`（此时始终为 `true`）、`backend`（`qnn`/`mnn`/`GGUF`）、`device`（`npu`/`cpu`/`gpu`）以及 `context_length`（该模型实际运行的上下文大小）。这在多模型部署场景中（参见 [MODEL_DEPLOYMENT.zh.md](MODEL_DEPLOYMENT.zh.md)）尤其有用，可以查看哪些模型已加载以及它们所使用的后端/设备。<br>
该接口注册了两个等效别名：`GET /models` 和 `GET /v1/models`。
```
import requests
BASE_URL = "http://127.0.0.1:8910/models"
response = requests.get(BASE_URL)
modelname = []
datas = response.json()["data"]
for data in datas:
    modelname.append(data["id"])
print(modelname)
```

## 获取模型状态（Get model status）
查询当前默认模型是否仍在加载中（这在服务刚启动后特别有用，因为 HTTP 服务器会在模型加载完成之前就开始接受连接）。
```
GET /status
```
```
import requests
response = requests.get("http://127.0.0.1:8910/status")
print(response.json())   # {"loading": "0"} once loaded, {"loading": "1"} while still loading
```

## 获取模型性能信息（Get model profile）
获取当前已加载默认模型的性能信息。<br>
如果尚未加载任何模型，则返回 `503` 及 `error` 字段。
```
import requests
BASE_URL = "http://127.0.0.1:8910/profile"
response = requests.get(BASE_URL)
print(response.json())
```

## 卸载模型（Unload model）
卸载当前已加载的默认模型，释放其占用的 NPU/GPU/CPU 资源。
```
POST /unload
```
```
import requests
response = requests.post("http://127.0.0.1:8910/unload")
print(response.status_code)
```

## 停止服务（Stop service）
终止服务器进程。<br>
```
import requests
print("开始测试终止服务:")
url = "http://127.0.0.1:8910/servicestop"
params = {"text": "stop"}  # the body must be {"text": "stop"} for the process to actually exit
response = requests.post(url, json=params)
if response.status_code == 200:
    print(Fore.GREEN + "stop service success\n")
else:
    print(Fore.RED + "fail to stop sevice\n")
```

## 获取模型上下文大小（Get model context size）
输入模型名称以获取该模型的最大上下文长度。如果省略 `model` 或其与任何已加载模型都不匹配，则会返回当前默认模型的上下文大小（不会报错）。<br>
```
url = "http://127.0.0.1:8910/contextsize"
params = {"model": model_name}  #Llama2.0-7B-SSD
response = requests.post(url, json=params)
if response.status_code == 200:
    result = response.json()
    print("context大小:",result["contextsize"])
```

## 图像生成（尚未实现）
注册了两个别名，`POST /images/generations` 和 `POST /v1/images/generations`，用于兼容 OpenAI API。目前这只是一个**占位实现**——无论发送什么请求体，每次请求都会无条件地返回 HTTP `501 Not Implemented` 及空响应体。
