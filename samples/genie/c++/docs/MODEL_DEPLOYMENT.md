# Deployment GenieAPIService and Client on your device

Download models first from this [Download Link](https://www.aidevhome.com/?id=51)
(external link — verify it is still valid).

Use the path `models/[MODEL_NAME]/config.json`. For VLM models, follow
the [VLM model layout](#Deployment).

- Windows: `qai-appbuilder\samples\genie\python\models` is the directory created by `git clone` — move models
  there. For a pre-built release package, no `qai-appbuilder` folder exists; the equivalent path is
  `samples\genie\python\models` relative to wherever you extracted the package.

- Android: push the model files to `/sdcard/GenieModels` on the device.

## Deployment

VLM models must follow this layout:

- [qwen2.5vl3b](#qwen2.5vl3b)
- [phi4mm](#phi4mm)
- [qwen2.5_omini_3b](#qwen2.5_omini_3b)

> **Note:** The file layouts below illustrate what a downloaded model package looks like. Specific file names
> (e.g. `veg.serialized.bin`, `embedding_weights.raw`) are private conventions of the underlying QNN Genie SDK
> model export process — the C++ service code never parses or depends on them. Treat this as a reference only;
> contents may vary between model versions, so defer to what is actually inside the package you downloaded.

### qwen2.5vl3b

```
./models/qwen2.5vl3b
│   config.json
│   embedding_weights.raw
│   htp_backend_ext_config.json
│   llm_model-0.bin
│   llm_model-1.bin 
│   prompt.json
│   tokenizer.json
│   veg.serialized.bin
│   
└───raw
        full_attention_mask.raw
        position_ids_cos.raw
        position_ids_sin.raw
        window_attention_mask.raw
```

### phi4mm

```
./models/phi4mm
│   config.json
│   embedding_weights_200064x3072.raw
│   prompt.json
│   tokenizer.json
│   veg.serialized.bin
│   weights_sharing_model_1_of_2.serialized.bin
│   weights_sharing_model_2_of_2.serialized.bin
│
└───raw
        attention_mask.bin
        position_ids.bin
```

### qwen2.5_omini_3b

```
./models/qwen2.5_omini_3b
│   config.json
│   embedding_weights_151936x2048.raw
│   model-1.bin
│   model-2.bin
│   model-3.bin
│   prompt.json
│   tokenizer.json
│
├───qwen2.5_omini_audio
│       audio.serialized.bin
│
└───qwen2.5_omini_vision
        full_attention_mask.raw
        position_ids_cos.raw
        position_ids_sin.raw
        veg.serialized.bin
        window_attention_mask.raw
```

## Multi-model deployment (optional)

In addition to the single-model `config.json` described above, the server supports loading and serving
**multiple models concurrently** — one on each backend/device (QNN/NPU, MNN/CPU, GGUF/GPU) — from a single
running `GenieAPIService` process.

### How it is discovered

Place a `service_config.json` file in the **same directory as the `GenieAPIService` executable** (auto-discovered;
no command-line flag can point at a different location). This additional loading only happens when the server
starts **with `-l`/`--load_model`** (see [USAGE.MD](USAGE.MD)); without `-l`, the server only loads the single
model pointed to by `-c`/`--config_file` and ignores any `models` list in `service_config.json`.

### `models[]` array — key fields

```json
{
  "default_model": "qwen3-8b-8480",
  "models": [
    {
      "name": "qwen3-8b-8480",
      "path": "qwen3-8b-8480",
      "backend": "qnn",
      "device": "npu",
      "context_size": 8480,
      "enabled": true
    },
    {
      "name": "gpt-oss-20b-mnn",
      "path": "gpt-oss-20b-MNN",
      "backend": "mnn",
      "device": "cpu",
      "context_size": 0,
      "enabled": false
    },
    {
      "name": "gpt-oss-20b-gguf",
      "path": "gpt-oss-20b-GGUF",
      "backend": "GGUF",
      "device": "gpu",
      "context_size": 0,
      "enabled": true
    }
  ]
}
```

- **`name`**: unique identifier for this model; clients select it via the `"model"` field of a
  `/v1/chat/completions` request (see [API.md](API.md)).
- **`path`**: model directory, resolved relative to the models root directory derived from your `-c` config file
  path (not relative to the executable's own directory).
- **`backend`**: `"qnn"` (Genie SDK / NPU), `"mnn"` (CPU), or `"GGUF"` (llama.cpp, typically GPU with CPU
  fallback).
- **`device`**: `"npu"` / `"cpu"` / `"gpu"`. Only **one** model per device can be resident at a time — a second
  entry targeting an already-occupied device is skipped, not queued.
- **`context_size`**: `0` uses the model's own default context length.
- **`enabled`**: set to `false` to temporarily disable a model (e.g. a memory-hungry MNN model) without removing
  its entry or affecting the loading of any other model in the list.
- **`default_model`** (top-level field, sibling of `models`): the model name that serves requests omitting the
  `"model"` field entirely.

### Client-side routing

Once multiple models are loaded, pick which one handles a given chat request by setting its name in the `"model"`
field of the request body — see the "Chat Completions" section in [API.md](API.md) for the full routing
semantics (including automatic on-demand loading if you request a model that is not yet loaded, and what happens
if `"model"` is omitted).