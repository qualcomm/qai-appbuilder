<br>

<div align="center">
  <h3>Run the large language model on the local NPU.</h3>
  <p><i> OpenAI Compatible API Service (C++) </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
This sample helps developers use C++ to build Genie based Open AI compatibility API service on Windows on Snapdragon (WoS), Mobile and Linux platforms.

## Features
• Support both stream and none stream mode <br> 
• Support tools call [*NEW!*] <br> 
• Support enable/disable thinking mode [*NEW!*] <br> 
• Support text splitter feature [*NEW!*] <br> 
• Support switching between models <br>
• Support to extend the use of your own model <br>

## GenieAPIService
Genie OpenAI Compatible API Service.

This is an OpenAI compatible API service that can be used to access the Genie AI model.
This service can be used on multiple platforms such as Android, Windows, Linux, etc.

### Run the service on WoS: 
You can also run the batch file from [QAI AppBuilder Launcher](../../../tools/launcher/) to setup the environment automatically. <br>
1. [Setup LLM models](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python#step-3-download-models-and-tokenizer-files) first before running this service. <br>
2. Download [GenieAPIService](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/GenieAPIService_2.34.zip) and copy the subdirectory "GenieAPIService" to path "ai-engine-direct-helper\samples".<br>
3. Run the following commands to launch the Service (Do *not* close this terminal window while service is running). 

```
cd ai-engine-direct-helper\samples
GenieAPIService\GenieAPIService.exe -c "genie\python\models\IBM-Granite-v3.1-8B\config.json"  -l
```
The service prints the following log, indicating that GenieAPIService started successfully.
```
process_arguments c: genie\python\models\IBM-Granite-v3.1-8B\config.json
process_arguments l: load model
model path: genie\python\models\IBM-Granite-v3.1-8B
model name: IBM-Granite-v3.1-8B
INFO: loading model <<< IBM-Granite-v3.1-8B >>>
[INFO]  "Using create From Binary"
[INFO]  "Allocated total size = 353404160 across 10 buffers"
SetStopSequence: {
  "stop-sequence" : ["<|end_of_text|>", "<|end_of_role|>", "<|start_of_role|>"]
}
INFO: model <<< IBM-Granite-v3.1-8B >>> is ready!
INFO: [TIME] | model load time >> 8103.10 ms
INFO: Service Is Ready Now!
```

#### More parameters: <br>
--all_text: Output all text includes tool calls text. Disabled by default.<br>
--enable_thinking: Enable thinking model. Disabled by default.<br>
*. It is recommended to disable thinking mode when using the tools call function.<br>
*. You can refer to [GenieAPIClientTools.py](../python/GenieAPIClientTools.py) on how to use tools call.<br>
```
GenieAPIService\GenieAPIService.exe -c "genie\python\models\Qwen3.0-8B-v31\config.json" -l --all_text --enable_thinking
```

#### Text Splitter
This function can divide a long text into multiple paragraphs according to the priority order of the specified delimiter and the maximum length of each paragraph. Length is counted by token number instead of text length. You can also use this function to calculate the token number of text. <br>
You can get the sample code on how to use Text Splitter [here](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/python/GenieAPIClient.py#L26).

### Run the service on Mobile(Snapdragon® 8 Elite Mobile device): <br>
1. Copy the subdirectory "GenieModels" in the folder "Android" in [GenieAPIService binary](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/GenieAPIService_2.34.zip) to the root path of mobile sdcard.<br>
2. Copy your QWen QNN model & tokenizer.json to "/sdcard/GenieModels/qwen2.0_7b"<br>
3. Modify the config file "/sdcard/GenieModels/qwen2.0_7b/config.json" if necessary.<br>
4. Install the GenieAPIService.apk to mobile and start it.<br>
* You can also try other models such [IBM-Granite-v3.1-8B-Instruct](https://aihub.qualcomm.com/mobile/models/ibm_granite_v3_1_8b_instruct?domain=Generative+AI&useCase=Text+Generation) which is for "Snapdragon® 8 Elite Mobile" device. You can create a subdirectory in the path "/sdcard/GenieModels/" for your model and customize the "config.json" for your model.

## Client Usage:
  The service can be access through the ip address 'localhost:8910', it's compatible with OpenAI API.

### C++ Client Sample Code:
  Here is a C++ client sample: [GenieAPIClient.cpp](Service/GenieAPIClient.cpp). You can get the compiled 'GenieAPIClient.exe' from [GenieAPIService](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/GenieAPIService_2.34.zip). The sample app depends on the dynamical library 'libcurl.dll' which also included in the 'GenieAPIService' package.

  We can run it with the command below in a new terminal window:
```
GenieAPIService\GenieAPIClient.exe --prompt "How to fish?" --stream
```

### Python Client Sample Code:
  Here is a Python client sample (You can save it to 'GenieAPIClient.py'):

```
import argparse
from openai import OpenAI

IP_ADDR = "localhost:8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="Hello", type=str)
args = parser.parse_args()

client = OpenAI(base_url="http://" + IP_ADDR + "/v1", api_key="123")

model_lst = client.models.list()
print(model_lst)

messages = [{"role": "system", "content": "You are a math teacher who teaches algebra."}, {"role": "user", "content": args.prompt}]
extra_body = {"size": 4096, "temp": 1.5, "top_k": 13, "top_p": 0.6}

model_name = "IBM-Granite-v3.1-8B"

if args.stream:
    response = client.chat.completions.create(model=model_name, stream=True, messages=messages, extra_body=extra_body)

    for chunk in response:
        content = chunk.choices[0].delta.content
        if content is not None:
            print(content, end="", flush=True)
else:
    response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body)
    print(response.choices[0].message.content)
```

  We can run it with the command below in a new terminal window:
```
python GenieAPIClient.py --prompt "How to fish?" --stream
```

### More Sample Code
You can find more client sample code [here](../python/README.md#sample-list).
