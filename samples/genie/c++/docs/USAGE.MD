# GenieService Usage

GenieService is a pair of HTTP CS(client and server) process.

You should launch the server first, then use client for ask the questions.

## Use For Android

1. You can find the GenieAPI in your launcher after installing apk.<br><br>

2. Click the GenieAPI then click the `START SERVICE` button. It will load the model files. You will see "Genie API
   Service IS Running." if loading model successfully. If you want to stop this service, please click the `STOP SERVICE`
   button.<br><br>

    - Note: Because some android devices will freeze the background process, please check Settings-Battery-Power saving
      settings-App battery management-GenieAPI and select Allow background activity.<br><br>

      If you want to check the log file, please click the menu at top right-hand corner. Then select Log Files->Log:
      1.<br><br>
      If you want to set the log level, please click the menu at top right-hand corner then select Log Level.<br><br>

3. If you want to use the [GenieChat](https://github.com/qualcomm/qai-appbuilder/tree/main/samples/android/GenieChat), please build and install. And open it and click the menu at top right-hand corner then select settings.
   you can select the model and go back to main window. Now you can query your questions.<br><br>

   ![img.png](img/6.png)

   ![img.png](img/5.png)

## Use For Windows

### Service start:

The Genie will start with loading the model config file.

`GenieAPIService.exe -c "genie\python\models\Qwen2.0-7B-SSD\config.json" -l`

VLM example:

`GenieAPIService.exe -c "genie\python\models\qwen2.5vl3b\config.json" -l`

There is some options, You can also type `GenieService.exe -h` to look up the more usage.
And set param for it.

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

- Note 1:

  Please note that the input length must not exceed the maximum number of tokens reserved for the input, which means
  it cannot exceed the model's maximum context length minus the value set for `--min_output_num`. You can invoke the
  Text
  Splitter to send the input text to the server for segmentation, and then sequentially pass the split segments to the
  LLM
  to complete the question-answering process.<br>


- Note 2:

  It is recommended to disable thinking mode when using the tools call function.<br>


- Note 3:

  You can refer to [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py) on how to use tools call.<br>

### Client Start

Client also have some options:

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

#### text models

Asking you a question directly.

Run c++ version for model Qwen2.0-7B-SSD

`GenieAPIClient.exe --prompt "how to fish?" --stream --model Qwen2.0-7B-SSD --host 127.0.0.1:8910`

#### image models

For VLM models, you should provide your question and the image for client

Run c++ version for model qwen2.5vl3b

`GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`

## Python example

`GenieAPIClient` is a C++ CLI sample; there is no equivalent standalone Python CLI for it in this repository. If you
want to call the service from Python instead, see [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py)
(also referenced in Note 3 above), or call the REST endpoints directly — see [API.md](API.md) for the
`/v1/chat/completions` request/response format (it works with any OpenAI-compatible client library, including the
`openai` Python package).

## Tools

There is many useful tools can help you develop and use Service, please move steps to [Tools](TOOLS.md)