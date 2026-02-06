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

3. If you want to use the [GenieChat](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/android/GenieChat), please build and install. And open it and click the menu at top right-hand corner then select settings.
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

Run c++ version for model Qwen2.0-7B-SSD

`GenieAPIClient.exe --prompt "how to fish?" --stream --model Qwen2.0-7B-SSD --ip 127.0.0.1`

#### image models

For VLM models, you should provide your question and the image for client

Run c++ version for model qwen2.5vl3b

`GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b`

## Python example

You can run Python version with same c++ Client params.

The sample is located at [GenieAPIClient.py](../Service/examples/GenieAPIClient/GenieAPIClient.py)

Install the dependence: openai

```
pip install openai
```

Run python version for qwen2.5vl3b

```
python GenieAPIClient.py --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b
```

## Tools

There is many useful tools can help you develop and use Service, please move steps to [Tools](TOOLS.md)