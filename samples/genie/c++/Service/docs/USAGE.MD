# GenieService Usage

GenieService is a pair of HTTP CS(client and server) process.

You should launch the server first, then use client for ask questions.

## Service start:

The Genie will start with loading the model config file. if you need to load VLM models, please follow
the [VLM model layout](VLM_Deployment.MD)

`GenieService.exe -c modles/Qwen2.0-7B-SSD/config.json -l`

or VLM example

`GenieService.exe -c modles/qwen2.5vl3b/config.json -l`

There is some options, You can also type `GenieService.exe -h` to look up the more usage.

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

Note: Please note that the input length must not exceed the maximum number of tokens reserved for the input, which means it cannot exceed the model's maximum context length minus the value set for '--min_output_num'. You can invoke the Text Splitter to send the input text to the server for segmentation, and then sequentially pass the split segments to the LLM to complete the question-answering process.<br>
Note: It is recommended to disable thinking mode when using the tools call function.<br>
Note: You can refer to [GenieAPIClientTools.py](../../python/GenieAPIClientTools.py) on how to use tools call.<br>

## Client Start

### text models

Asking you a question directly.

`GenieClient.exe --promopt "how to fish?" --stream`

### image models

You should provide your question and the image for client
`GenieClient.exe --promopt "what is the image descript?" --img test.png --stream`
