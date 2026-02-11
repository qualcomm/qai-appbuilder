<br>

<div align="center">
  <h3>Run the large language model on the local NPU.</h3>
  <p><i> OpenAI Compatible API Service (Python) </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
This sample helps developers use QAI AppBuilder + Python to build Genie based Open AI compatibility API service on Windows on Snapdragon (WoS) platform. <br>
We suggest using the C++ version of [GenieAPIService](../c++/) provided by us.

## Features
• Support both stream and none stream mode <br> 
• Support tools call [*NEW!*] <br> 
• Support enable/disable thinking mode [*NEW!*] <br> 
• Support switching between models <br>
• Support to extend the use of your own model <br>

## Setting Up Environment For Service:

### Step 1: Install Dependencies
Refer to [python.md](../../../docs/python.md) on how to setup x64 version Python environment.

### Step 2: Install basic Python dependencies for service
Run following commands in Windows terminal:
```
pip install uvicorn==0.34.0 pydantic_settings==2.10.1 fastapi==0.115.8 langchain==0.3.19 langchain_core==0.3.45 langchain_community==0.3.18 sse_starlette==2.2.1 pypdf==5.3.0 python-pptx==1.0.2 docx2txt==0.8 openai==1.63.2 json-repair==0.47.4
```

### Step 3: Download models and tokenizer files
Download the files from the [AI-Hub LLM models](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python#ai-hub-llm-models) list at the end of this page, save them to following path. <br>
```
ai-engine-direct-helper\samples\genie\python\models\<model name>
```
* Select 'Snapdragon® X Elite' as the device for WoS platform while downloading the model.<br>
* You need to unzip the 'weight_sharing_model_N_of_N.serialized.bin' files from model package and copy them to following path. Download and copy the corresponding 'tokenizer.json' file to the following directory path too. 
* Please be careful not to mix 'tokenizer.json' file of different models. Ensure that the' tokenizer.json' file corresponding to the IBM Granite model is placed in the "samples\genie\python\models\IBM-Granite-v3.1-8B" directory, and the' tokenizer.json' file corresponding to the Phi 3.5 model is placed in the "samples\genie\python\models\Phi-3.5-mini" directory.<br>

If you want to modify the relative path of the directory where the model file is located, you need to modify the "config.json" file in the corresponding directory of the model to ensure that the 'tokenizer.json', 'htp_backend_ext_config.json' and model files set in the configuration file can be found correctly.
```
ai-engine-direct-helper\samples\genie\python\models\<model name>\config.json
```

* You can also use your own LLM Genie model (if you have one). Refer to [setup custom model](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python#setup-custom-model) part for detailed steps.

### Step 4: Switch to samples directory
Run following commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
```

### Step 5: Run service
Run the following commands to launch Genie API Service (Do *not* close this terminal window while service is running)
```
python genie\python\GenieAPIService.py --modelname "IBM-Granite-v3.1-8B" --loadmodel --profile
```
The service prints the following log, indicating that GenieAPIService started successfully.
```
INFO:     loading model <<< IBM-Granite-v3.1-8B >>>
[INFO]  "Using create From Binary"
[INFO]  "Allocated total size = 353404160 across 10 buffers"
INFO:     model <<< IBM-Granite-v3.1-8B >>> is ready!
INFO:     model init time: 4.71 (s)
INFO:     Started service process [7608]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8910 (Press CTRL+C to quit)
```
#### More parameters: <br>
--all-text: Output all text includes tool calls text. Disabled by default.<br>
--enable-thinking: Enable thinking mode. Disabled by default.<br>
*. It is recommended to disable thinking mode when using the tools call function.<br>
*. You can refer to [GenieAPIClientTools.py](../python/GenieAPIClientTools.py) on how to use tools call.<br>
```
python genie\python\GenieAPIService.py --modelname "Qwen3.0-8B-v31" --loadmodel --profile --all-text --enable-thinking
```

### Step 6: Now you can access the API service
The default IP address for this API is: '127.0.0.1:8910', you can access this IP address in the client app.
You can run the following Python in a new terminal window. <br>
The following command to generate text from text prompt:
```
python genie\python\GenieAPIClient.py --prompt "How to fish?" --stream
```
The following command to generate image from text prompt:
```
python genie\python\GenieAPIClientImage.py --prompt "spectacular view of northern lights from Alaska"
```
* When you run the client, you can see the current status of processing client requests from the service terminal window. 
* When you run the request of image generation for the first time, the service may have to download the Stable Diffusion model from AI-Hub, and it will take a long time.

### Sample List:
|  Sample   | Description  |
|  ----  | :----    |
| GenieAPIClient.py | A client sample code to call GenieAPIService to generate text. |
| GenieAPIClientTools.py | A client sample code to make tool call to GenieAPIService. |
| GenieAPIClientImage.py * | A client sample code to call GenieAPIService to generate image. |
| GenieSample.py  | Use GenieContext to load and run the LLM model in local process.|

*. Image generation is only supported in the Python version of GenieAPIService. <br>

### AI-Hub LLM models:

|  Model  | model file  | tokenizer file *
|  ----  | :----   | :----   |
| [IBM Granite v3.1 8B](https://aihub.qualcomm.com/compute/models/ibm_granite_v3_1_8b_instruct) | [IBM-Granite-v3.1-8B-Instruct](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct/v1/snapdragon_x_elite/models.zip) | [tokenizer.json](https://huggingface.co/ibm-granite/granite-3.1-8b-base/resolve/main/tokenizer.json?download=true) |
| [Phi 3.5 mini](https://aihub.qualcomm.com/compute/models/phi_3_5_mini_instruct) ** | [Phi-3.5-mini-instruct](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/phi_3_5_mini_instruct/v1/snapdragon_x_elite/models.zip) | [tokenizer.json](https://huggingface.co/microsoft/Phi-3.5-mini-instruct/resolve/main/tokenizer.json?download=true) |

*. Tokenizer files will be downloaded automatically while you running 'GenieAPIService.py'.<br>
**. For 'Phi-3.5-Mini-Instruct' model, to see appropriate spaces in the output, remove lines 192-197 (Strip rule) in the 'tokenizer.json' file if you download it manually. Remember to delete the extra commas on line 191. (If you download it through 'GenieAPIService.py', the Python script will modify the 'tokenizer.json' file automatically.) <br>
The following is the correct change for the 'tokenizer.json' file of 'Phi-3.5-Mini-Instruct' model:<br>
```
-      },
-      {
-        "type": "Strip",
-        "content": " ",
-        "start": 1,
-        "stop": 0
-      }
+      }
```
***. Refer to [setup Stable Diffusion v2.1 models](../../python/README.md) before run 'GenieAPIService.py' (Our Python version 'GenieAPIService.py' support generating image, it depends on Stable Diffusion v2.1 sample code.)

### AI-Dev LLM models:
|  Model  | model file 
|  ----  | :----   | 
| [Qwen2-7B-SSD](https://www.aidevhome.com/data/adh2/models/suggested/Qwen2.0-7B-SSD-8380-2.34.zip) | [Qwen2.0-7B-SSD-8380-2.34.zip](https://www.aidevhome.com/data/adh2/models/suggested/Qwen2.0-7B-SSD-8380-2.34.zip) | 
| [Phi 3.5 mini](https://aihub.qualcomm.com/compute/models/phi_3_5_mini_instruct) ** | [Phi-3.5-mini-instruct](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/phi_3_5_mini_instruct/v1/snapdragon_x_elite/models.zip) |


### Setup custom model:
You can create a subdirectory in the path "ai-engine-direct-helper\samples\genie\python\models\" for your own model and customize the "config.json", "prompt.conf" or "prompt.json" files for your model. Both files should be stored in the same directory as your model files. Then use the new directory which you've created as your model name, the name can be used in the client application. <br>
Note: "prompt.conf" is for old version GenieAPIServcie(< 2.0). Since GenieAPIServcie 2.0, we use "prompt.json" to replace it.<br>

1. config.json : Model configuration file. It includes key parameters for the model. You can get several template configuration files for popular models such as Llama 2 & 3, Phi 3.5, Qwen 2 from here:<br>
https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie/configs/genie <br><br>
You need to make sure that the following parameters in the configuration file point to the correct file path. <br>
a. tokenizer: The path to the model 'tokenizer.json' file. <br>
b. extensions: The path to Genie extension configuration file. You can find it from path: 'ai-engine-direct-helper\samples\genie\python\config\htp_backend_ext_config.json'<br>
c. ctx-bins: The path to model context bin files(ctx-bins). Usually a model has 3~5 context bin files. <br>
d. forecast-prefix-name:The path to the directory where SSD model 'kv-cache.primary.qnn-htp' file is stored. Only SSD model needs this parameter.<br>
e. other parameters: Set other parameters according to your model. <br>

2. prompt.json : model prompt format configuration file. It defines the input format template for how the model organizes system prompts, user inputs, model responses and other information when receiving user requests.<br>
Following is a format template for the prompt.json file of the QWEN model:<br>
```
{
  "prompt_system": "<|im_start|>system\n string <|im_end|>\n",
  "prompt_user": "<|im_start|>user\n string <|im_end|>\n",
  "prompt_assistant": "<|im_start|>assistant\n string <|im_end|>\n",
  "prompt_tool": "<|im_start|>tool\n string <|im_end|>\n",
  "prompt_start": "<|im_start|>assistant\n"
}
```
Field description:<br>
a. prompt_system: the format template of the system role prompt, string will be replaced with the actual system prompt.<br>
b. prompt_user: the format template of user input, string will be replaced with the user's actual problem.<br>
c. prompt_assistant: the format template of the model's reply, string is replaced with the historical reply that the model has generated.<br>
d. prompt_tool: the format template of return content of tool/function call, string is the placeholder for tool's retrun result.<br>
e. prompt_start: generate a start tag that indicates "it's the model's turn to reply", and does not contain an end tag at the end, from where the model will start generating text.<br>

The following link contains the prompt format of some other models. <br>
https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie#prompt-formats <br>
Note: Some models (such as Llama) do not require additional starting tokens, which can be left blank or set to a special token based on the tokenizer.<br>

3. prompt.conf : (deprecated from GenieAPIService v2.0.0).Model prompt format configuration file. There are two lines in this file, which are used to set prompt_tags_1 and prompt_tags_2 parameters respectively. A complete prompt consists of the following contents: <br>
prompt = prompt_tags_1 + < user questions > + prompt_tags_2 <br><br>
Taking the QWen 2 model as an example, a complete prompt example is as follows: <br>
<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nHow to fish?<|im_end|>\n<|im_start|>assistant\n <br><br>
In this example, the user's question is "How to fish?". Therefore, we extract the content before the question as the content of prompt_tags_1 and extract the content after the question as the content of prompt_tags_2. We set the content of 'promtp.conf' as follows: <br>
prompt_tags_1: <|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n <br>
prompt_tags_2: <|im_end|>\n<|im_start|>assistant\n <br><br>
The following link contains the prompt format of some models. <br>
https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie#prompt-formats <br>

3. For more information about LLM Genie model, you can refer to following link: <br>
https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie
