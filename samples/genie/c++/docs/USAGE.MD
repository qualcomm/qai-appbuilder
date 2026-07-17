# GenieService Usage

GenieService is a pair of HTTP client/server processes.

Launch the server first, then use the client to ask questions.

## Use For Android

1. Find the GenieAPI in your launcher after installing the APK.<br><br>

2. Click GenieAPI, then click `START SERVICE`. It loads the model files and shows "Genie API
   Service IS Running." once loading succeeds. Click `STOP SERVICE` to stop the service.<br><br>

    - Note: Some Android devices freeze background processes. Check Settings-Battery-Power saving
      settings-App battery management-GenieAPI and select Allow background activity.<br><br>

      To view the log file, click the menu at the top right, then select Log Files->Log
      1.<br><br>
      To set the log level, click the menu at the top right, then select Log Level.<br><br>

3. To use [GenieChat](https://github.com/qualcomm/qai-appbuilder/tree/main/samples/android/GenieChat), build and install it. Open it, click the menu at the top right, and select settings.
   Select the model and go back to the main window. Now you can ask questions.<br><br>

   ![img.png](img/6.png)

   ![img.png](img/5.png)

## Use For Windows

### Service start:

The service loads the model config file on startup.

`GenieAPIService.exe -c "genie\python\models\Qwen2.0-7B-SSD\config.json" -l`

VLM example:

`GenieAPIService.exe -c "genie\python\models\qwen2.5vl3b\config.json" -l`

Run `GenieAPIService.exe -h` for the full list of options and parameters.

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

  Input length must not exceed the tokens reserved for input — the model's maximum context length minus the
  value set for `--min_output_num`. Use the Text Splitter to segment long input text, then pass each segment
  to the LLM sequentially to complete the question-answering process.<br>


- Note 2:

  Disable thinking mode when using the tools call function (recommended).<br>


- Note 3:

  See [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py) for how to use tools call.<br>

### Client Start

Client options:

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

Ask a question directly.

Run c++ version for model Qwen2.0-7B-SSD

`GenieAPIClient.exe --prompt "how to fish?" --stream --model Qwen2.0-7B-SSD --host 127.0.0.1:8910`

#### image models

For VLM models, provide both the question and the image.

Run c++ version for model qwen2.5vl3b

`GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`

## Python example

`GenieAPIClient` is a C++ CLI sample only; this repository has no equivalent standalone Python CLI. To call the
service from Python instead, see [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py)
(also referenced in Note 3 above), or call the REST endpoints directly — see [API.md](API.md) for the
`/v1/chat/completions` request/response format (compatible with any OpenAI-compatible client library, including the
`openai` Python package).

## Tools

Several tools help with developing and using the service. See [Tools](TOOLS.md).