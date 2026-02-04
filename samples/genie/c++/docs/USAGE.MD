# GenieService Usage

GenieService is a pair of HTTP CS(client and server) process.

You should launch the server first, then use client for ask questions.

## Use For Android

1. You can find the GenieAPI in your luancher after installing apk.


2. Please push the model files into your device. The model files should be pushed to `/sdcard/GenieModels`.


3. Click the GenieAPI then click the `START SERVICE` button. It will load the model files. You will see "Genie API
   Service
   IS Running." if loading model successfully. If you want to stop this service, please click the `STOP SERVICE` button.
   If you want to check the log file, please
   click the menu at top right-hand corner. Then select Log Files->Log:1.
   If you want to set the log level, please click the menu at top right-hand corner then select Log Level.


- Note: Because some android devices will freeze the background process, please check Settings-Battery-Power saving
  settings-App battery management-GenieAPI and select Allow background activity.


4. Please also build and install
   the [flet-chat/GenieChat](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/fletui/GenieFletUI/android/BUILD.md)
   or [GenieChat](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/android/GenieChat) apk for client.


5. Open the flet-chat and click the menu at top right-hand corner and select Server Settings. Then you can click the
   item list in LLM to select a model then click the Verify button and ok button.


6. Now you can query your questions.


7. If you want to use the GenieChat, please open it and click the menu at top right-hand corner then select settings.
   you can select the model and go back to main window. Now you can query your questions.

## Use For Windows

### Service start:

The Genie will start with loading the model config file.
the path `models/[MODEL_NAME]/config.json` is recommended.

`GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l`

or VLM example, Please follow the [VLM model layout](VLM_DEPLOYMENT.MD)

`GenieAPIService.exe -c models/qwen2.5vl3b/config.json -l`

There is some options, You can also type `GenieService.exe -h` to look up the more usage.
And set param for it.

```
Options:
  -h,--help                   Print this help message and exit
  -c,--config_file TEXT       Path to the config file.
  --adapter TEXT              the adapter of lora
  -l,--load_model             Load the model.
  -a,--all_text               Output all text includes tool calls text.
  -t,--enable_thinking        Enable thinking mode.
  -v,--version                Print version info and exit.
  -n,--num_response INT       The number of rounds saved in the historical record
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
  --stream                    Answer in stream mode. It is required
  --model                     The model will be loaded. It's should located at ./models/xxxx
  --ip                        The server ip for communication. Default is "127.0.0.1"   
  --img                       the client image path for VLM models.
```

#### text models

Asking you a question directly.

`GenieAPIClient.exe --prompt "how to fish?" --stream --model Qwen2.0-7B-SSD --ip 127.0.0.1`

#### image models

For VLM models, you should provide your question and the image for client

`GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`

## Python example

You can run Python version with same c++ Client params.

The sample is located at [GenieAPIClient.py](../Service/examples/GenieAPIClient/GenieAPIClient.py)

```
python GenieAPIClient.py --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`
```
