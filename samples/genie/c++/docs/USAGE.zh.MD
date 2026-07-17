# GenieService 使用说明

GenieService 是一对 HTTP 客户端/服务器（client and server）进程。

你应该先启动服务器，然后再使用客户端来提出问题。

## 在 Android 上使用

1. 安装 apk 后，你可以在启动器（launcher）中找到 GenieAPI。<br><br>

2. 点击 GenieAPI，然后点击 `START SERVICE` 按钮。它会加载模型文件。如果加载模型成功，你会看到 "Genie API
   Service IS Running."。如果你想停止此服务，请点击 `STOP SERVICE`
   按钮。<br><br>

    - 注意：由于某些 Android 设备会冻结后台进程，请检查 设置-电池-省电
      设置-应用电池管理-GenieAPI，并选择允许后台活动。<br><br>

      如果你想查看日志文件，请点击右上角的菜单。然后选择 Log Files->Log:
      1。<br><br>
      如果你想设置日志级别，请点击右上角的菜单，然后选择 Log Level。<br><br>

3. 如果你想使用 [GenieChat](https://github.com/qualcomm/qai-appbuilder/tree/main/samples/android/GenieChat)，请构建并安装它。打开它后点击右上角的菜单，然后选择 settings。
   你可以选择模型，然后返回主窗口。现在你可以查询你的问题了。<br><br>

   ![img.png](img/6.png)

   ![img.png](img/5.png)

## 在 Windows 上使用

### 服务启动：

Genie 启动时会加载模型配置文件。

`GenieAPIService.exe -c "genie\python\models\Qwen2.0-7B-SSD\config.json" -l`

VLM 示例：

`GenieAPIService.exe -c "genie\python\models\qwen2.5vl3b\config.json" -l`

它还有一些选项，你也可以输入 `GenieService.exe -h` 来查看更多用法。
并为其设置参数。

```
Options:
  -h,--help                   Print this help message and exit
  -c,--config_file TEXT       Path to the config file.
  --adapter TEXT              the adapter of lora
  -l,--load_model             Load the model and also load additional models listed in service_config.json.
  -a,--all_text                Output all text includes tool calls text.
  -t,--enable_thinking        Enable thinking mode.
  -g,--prompt_debug           Enable prompt compression optimization debug logs. (Repeat for level 2: -g -g)
  -v,--version                Print version info and exit.
  -n,--num_response INT       The number of rounds saved in the historical record. Passing -1 has special
                              semantics beyond history size: it switches the server to a stateless
                              client-managed-history mode, enables prompt compression/optimization, and
                              enables long-text Map-Reduce summarization — it is not simply "save -1 rounds".
  -o,--min_output_num INT     The minimum number of tokens output
  -d,--loglevel INT           log level setting for record. 1: Error, 2: Warning, 3: Info, 4: Debug, 5: Verbose
  -f,--logfile TEXT           log file path, it's a option
  --lora_alpha FLOAT          lora Alpha Value
  -p,--port INT               Port used for running
```

- 注意 1：

  请注意，输入长度不得超过为输入预留的最大 token 数，也就是说，
  它不能超过模型的最大上下文长度减去 `--min_output_num` 所设置的值。你可以调用
  文本分割器（Text
  Splitter）将输入文本发送到服务器进行切分，然后依次将切分后的片段传给
  LLM
  以完成问答过程。<br>


- 注意 2：

  建议在使用工具调用（tools call）功能时禁用思考模式（thinking mode）。<br>


- 注意 3：

  你可以参考 [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py) 了解如何使用工具调用。<br>

### 客户端启动

客户端也有一些选项：

```
Options:
  --prompt                    Your questions for ask. Default is "hi!" or "What is it descript"
  --system                    Model system prompt. Default is "You are a helpful assistant."   
  --stream                    Answer in stream mode. This is an optional boolean flag (omit it for a
                              non-streaming response); it is not required.
  --model                     The model will be loaded. It's should located at ./models/xxxx
  --host                      The server address for communication (host, or host:port). Default is
                              "127.0.0.1:8910" — note that the default already includes the port, not just an IP.
  --img                       the client image path for VLM models.
  --audio                     the client audio path for multimodal (audio input) models.
  --key                       API key to send as "api_key" in the request body.
  --raw_file                  Path to a file containing a complete, ready-to-send request body (JSON); when
                              set, it is sent as-is instead of building the body from --prompt/--system/etc.
  --bench                     Enable a lightweight benchmark/stress mode (forcibly truncates the response
                              after a short prefix instead of waiting for the full output).
```

#### 文本模型

直接向你提出一个问题。

运行 c++ 版本，模型为 Qwen2.0-7B-SSD

`GenieAPIClient.exe --prompt "how to fish?" --stream --model Qwen2.0-7B-SSD --host 127.0.0.1:8910`

#### 图像模型

对于 VLM 模型，你需要为客户端提供你的问题以及图片

运行 c++ 版本，模型为 qwen2.5vl3b

`GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`

## Python 示例

`GenieAPIClient` 是一个 C++ CLI 示例；本仓库中没有与之等效的独立 Python CLI。如果你
想改用 Python 调用该服务，请参见 [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py)
（在上文的注意 3 中也有引用），或者直接调用 REST 接口——关于
`/v1/chat/completions` 的请求/响应格式，请参见 [API.zh.md](API.zh.md)（它可以配合任何与 OpenAI 兼容的客户端库使用，包括
`openai` Python 包）。

## 工具

有许多有用的工具可以帮助你开发和使用 Service，请移步 [Tools](TOOLS.zh.md)
