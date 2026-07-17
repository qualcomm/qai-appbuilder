# GenieAPIService API <br>

## Chat Completions
The **core inference endpoint**, OpenAI-compatible, registered under four equivalent aliases. Any of the following paths works the same way:
```
POST /completions
POST /v1/completions
POST /chat/completions
POST /v1/chat/completions
```

### Model routing via the `model` field
The request body's `model` field selects which loaded model handles the request:
- If `model` matches the name of an already-loaded model (see [Get modelname list](#get-modelname-list)), that model is used directly.
- If `model` is omitted, the server's current default model is used.
- If `model` is set but does not match any currently loaded model, the server attempts a **dynamic switch**: it unloads whatever model currently occupies the same device (NPU/GPU/CPU) and loads the requested one on demand, then serves the request. This only works when the model can actually be found on disk under the configured models root; otherwise the request fails.
- A `local::` prefix on `model` (added by some agent front-ends) is stripped automatically before routing.

This routing only matters when the server has more than one model available — see [MODEL_DEPLOYMENT.md](MODEL_DEPLOYMENT.md) for how to configure multiple models via `service_config.json`.

### `messages` — two supported formats
Each message's `content` field accepts **either** of the following shapes. Different requests may mix styles, but a single message's `content` must use one or the other:

1. **Standard OpenAI array format** (recommended, best compatibility):
   ```json
   {"role": "user", "content": [
       {"type": "text", "text": "What is in this image?"},
       {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<...>"}},
       {"type": "input_audio", "input_audio": {"data": "<base64-encoded audio>"}}
   ]}
   ```
2. **GenieAPIClient-style flat object** (a simpler, non-standard shorthand also accepted by the server):
   ```json
   {"role": "user", "content": {"question": "What is in this image?", "image": "<base64 or data URL>", "audio": "<base64>"}}
   ```

Both `image_url`/`image` and `input_audio`/`audio` are optional — omit them for plain text chat. Multimodal input is only meaningful for models that support it (the `qwen2.5vl*`, `qwen2.5_omini*`, and `phi4*` model families); MNN and GGUF models do not support multimodal input.

### `stream` — optional, defaults to `false`
Set `"stream": true` to receive the response as `text/event-stream` Server-Sent Events, in the same incremental `delta` chunk format used by OpenAI's streaming API. Omit it (or set it to `false`) for a single, complete JSON response.

### Minimal example
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

## Text Splitter
Splits a long text into multiple paragraphs, following the priority order of the given delimiters and a maximum length per paragraph. Length is measured in token count, not character count. The same endpoint also works as a standalone token counter.<br>
Registered under two equivalent aliases: `POST /textsplitter` and `POST /v1/textsplitter`.

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

## Terminate output
Terminates the model's current output. An optional `model` field targets a specific loaded model; if omitted, the current default model is targeted.
```
import requests
url = "http://127.0.0.1:8910/stop"
params = {"text": "stop", "model": "qwen3-8b-8480"}  # "model" is optional
response = requests.post(url, json=params)
```

## Chat history endpoints — `/clear`, `/reload`, `/fetch`
> **Note on current behavior:** chat history is now built fresh from the `messages` array of each individual `/v1/chat/completions` request (the server keeps no server-side conversation state between requests). As a result, these three legacy endpoints no longer do what their names suggest — see each one below for its exact current behavior. If you need multi-turn context, send the full conversation as the `messages` array on every request instead.

### `POST /clear`
Currently a **no-op**: always returns `200 OK` with an empty body and clears nothing server-side, since the server retains no history to clear.
```
import requests

url = "http://127.0.0.1:8910/clear"
params = {"text": "clear"}
response = requests.post(url, json=params)
```

### `POST /reload`
Only **validates** the JSON structure of the given `history` array (each item must have string `role` — one of `user`/`assistant`/`tool` — and string `content`); it returns `200 OK` with `{"status": "ok"}` if valid, or `400` with a descriptive `error` otherwise. **It does not persist or actually load the history anywhere** — the validated data is discarded once the request completes. To use conversation history, include it directly in the `messages` array of your `/v1/chat/completions` request instead.
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
Always returns a **hardcoded empty history**, regardless of what was previously sent to `/reload` or any chat request: `{"history": []}`.
```
import requests
BASE_URL = "http://127.0.0.1:8910/fetch"
response = requests.post(BASE_URL)
print(response.text)
```

## Get modelname list
Get the list of models the server currently knows about — every subdirectory containing a `config.json` under the models root, whether or not it is currently loaded.<br>
Each item in `data` includes `id` (model name) and, only for models that are **currently loaded**, additional fields: `is_loaded` (always `true` in that case), `backend` (`qnn`/`mnn`/`GGUF`), `device` (`npu`/`cpu`/`gpu`), and `context_length` (the model's actual running context size). This is especially useful in a multi-model deployment (see [MODEL_DEPLOYMENT.md](MODEL_DEPLOYMENT.md)) to see which models are loaded and on which backend/device.<br>
This endpoint is registered under two equivalent aliases: `GET /models` and `GET /v1/models`.
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

## Get model status
Query whether the current default model is still loading (useful right after starting the service, since the HTTP server starts accepting connections before model loading finishes).
```
GET /status
```
```
import requests
response = requests.get("http://127.0.0.1:8910/status")
print(response.json())   # {"loading": "0"} once loaded, {"loading": "1"} while still loading
```

## Get model profile
Obtain the performance information of the currently loaded default model.<br>
Returns `503` with an `error` field if no model is loaded yet.
```
import requests
BASE_URL = "http://127.0.0.1:8910/profile"
response = requests.get(BASE_URL)
print(response.json())
```

## Unload model
Unload the currently loaded default model, freeing its NPU/GPU/CPU resources.
```
POST /unload
```
```
import requests
response = requests.post("http://127.0.0.1:8910/unload")
print(response.status_code)
```

## Stop service
Terminate the server process.<br>
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

## Get model context size
Pass a model name in `model` to obtain that model's maximum context length. If `model` is omitted or does not match a loaded model, the current default model's context size is returned instead, with no error.<br>
```
url = "http://127.0.0.1:8910/contextsize"
params = {"model": model_name}  #Llama2.0-7B-SSD
response = requests.post(url, json=params)
if response.status_code == 200:
    result = response.json()
    print("context大小:",result["contextsize"])
```

## Image generation (not implemented)
Registered under two aliases, `POST /images/generations` and `POST /v1/images/generations`, for OpenAI API compatibility. This is currently a **placeholder** — every request unconditionally returns HTTP `501 Not Implemented` with an empty body, regardless of the payload sent.
