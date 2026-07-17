# GenieService 使用说明

GenieService 是一对 HTTP 客户端/服务器进程。

先启动服务器，再使用客户端提问。

## 在 Android 上使用

1. 安装 APK 后，在启动器中找到 GenieAPI。<br><br>

2. 点击 GenieAPI，再点击 `START SERVICE`。加载模型成功后会显示 "Genie API
   Service IS Running."。点击 `STOP SERVICE` 停止服务。<br><br>

    - 注意：部分 Android 设备会冻结后台进程，需在 设置-电池-省电
      设置-应用电池管理-GenieAPI 中选择允许后台活动。<br><br>

      查看日志文件：点击右上角菜单，选择 Log Files->Log
      1。<br><br>
      设置日志级别：点击右上角菜单，选择 Log Level。<br><br>

3. 使用 [GenieChat](https://github.com/qualcomm/qai-appbuilder/tree/main/samples/android/GenieChat)：构建并安装后打开，点击右上角菜单选择 settings，
   选择模型并返回主窗口，即可开始提问。<br><br>

   ![img.png](img/6.png)

   ![img.png](img/5.png)

## 在 Windows 上使用

### 服务启动：

服务启动时加载模型配置文件。

`GenieAPIService.exe -c "genie\python\models\Qwen2.0-7B-SSD\config.json" -l`

VLM 示例：

`GenieAPIService.exe -c "genie\python\models\qwen2.5vl3b\config.json" -l`

完整选项与参数列表见 `GenieAPIService.exe -h`。

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

  输入长度不得超过为输入预留的最大 token 数——即模型的最大上下文长度减去
  `--min_output_num` 设置的值。可用文本分割器（Text Splitter）将长文本切分，
  再依次将各片段传给 LLM 完成问答。<br>


- 注意 2：

  使用工具调用（tools call）功能时建议禁用思考模式（thinking mode）。<br>


- 注意 3：

  工具调用的使用方法参见 [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py)。<br>

### 客户端启动

客户端选项：

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

直接提出问题。

运行 c++ 版本，模型为 Qwen2.0-7B-SSD

`GenieAPIClient.exe --prompt "how to fish?" --stream --model Qwen2.0-7B-SSD --host 127.0.0.1:8910`

#### 图像模型

VLM 模型需要同时提供问题和图片。

运行 c++ 版本，模型为 qwen2.5vl3b

`GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`

## Python 示例

`GenieAPIClient` 仅是 C++ CLI 示例，本仓库没有与之等效的独立 Python CLI。如需从 Python
调用该服务，参见 [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py)
（同注意 3），或直接调用 REST 接口——`/v1/chat/completions` 的请求/响应格式见
[API.zh.md](API.zh.md)（兼容任何 OpenAI 客户端库，包括
`openai` Python 包）。

## 工具

开发和使用 Service 的常用工具见 [Tools](TOOLS.zh.md)。
