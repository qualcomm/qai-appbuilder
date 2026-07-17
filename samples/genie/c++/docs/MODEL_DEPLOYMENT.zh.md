# 在设备上部署 GenieAPIService 和客户端

请先通过此[下载链接](https://www.aidevhome.com/?id=51)选择并下载模型
（外部链接 — 请自行验证该链接是否仍然有效）。

使用路径 `models/[MODEL_NAME]/config.json`。对于 VLM 模型，请遵循
[VLM 模型布局](#Deployment)。

- Windows：`qai-appbuilder\samples\genie\python\models` 是 `git clone` 源代码仓库后得到的目录，将模型移动到该目录下。
  若使用预构建发布包，则不存在 `qai-appbuilder` 文件夹；等效路径是相对于解压位置的 `samples\genie\python\models`。

- Android：将模型文件推送至设备的 `/sdcard/GenieModels` 目录。

## 部署

VLM 模型必须遵循以下布局：

- [qwen2.5vl3b](#qwen2.5vl3b)
- [phi4mm](#phi4mm)
- [qwen2.5_omini_3b](#qwen2.5_omini_3b)

> **注意：** 以下文件布局是下载的模型包外观的示例说明。具体文件名（例如
> `veg.serialized.bin`、`embedding_weights.raw`）是底层 QNN Genie SDK 模型导出流程的私有约定 — C++ 服务代码从不解析或依赖这些
> 文件名。请以此仅作参考；确切内容可能因模型版本而异，请始终以实际下载的
> 软件包中的内容为准。

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

## 多模型部署（可选）

除上文描述的单模型 `config.json` 外，服务器还支持从单个正在运行的 `GenieAPIService` 进程中
**并发加载并服务多个模型** — 每个后端/设备（QNN/NPU、MNN/CPU、GGUF/GPU）各加载一个。

### 如何被发现

将 `service_config.json` 文件放置在与 `GenieAPIService` 可执行文件**相同的目录**中（自动发现，没有命令行参数可以指定其他位置）。这种额外的加载仅在服务器
以 **`-l`/`--load_model`** 启动时发生（参见 [USAGE.MD](USAGE.zh.MD)）；不带 `-l` 时，
服务器只加载 `-c`/`--config_file` 所指向的单个模型，忽略 `service_config.json` 中的任何 `models` 列表。

### `models[]` 数组 — 关键字段

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

- **`name`**：该模型的唯一标识符；客户端通过 `/v1/chat/completions` 请求中的 `"model"` 字段来选择它
  （参见 [API.md](API.zh.md)）。
- **`path`**：模型目录，相对于从您的 `-c` 配置文件路径推导出的模型根目录进行解析
  （不是相对于可执行文件自身所在的目录）。
- **`backend`**：`"qnn"`（Genie SDK / NPU）、`"mnn"`（CPU）或 `"GGUF"`（llama.cpp，通常为 GPU，并带有 CPU
  回退）。
- **`device`**：`"npu"` / `"cpu"` / `"gpu"`。每个设备一次只能驻留**一个**模型 — 第二条
  指向已被占用设备的条目会被跳过，而不会被排队等待。
- **`context_size`**：`0` 表示使用模型自身的默认上下文长度。
- **`enabled`**：设为 `false` 可临时禁用某个模型（例如内存占用较大的 MNN 模型），无需移除
  其条目，也不影响其他模型的加载。
- **`default_model`**（顶层字段，与 `models` 同级）：请求省略 `"model"` 字段时，
  由该模型名称处理请求。

### 客户端路由

加载多个模型后，可以通过在请求体的 `"model"` 字段中设置其名称来选择由哪个模型处理给定的聊天请求 — 完整的路由语义（包括当您请求一个尚未加载的模型时的自动按需加载，以及当 `"model"`
被省略时会发生什么）请参见 [API.md](API.zh.md) 中的 "Chat Completions" 章节。
