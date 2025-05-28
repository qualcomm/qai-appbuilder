# README

## Introduction 
This sample helps developers use QAI AppBuilder + Python to build Genie based Open AI compatibility API service on Windows on Snapdragon (WoS) platform.

## Setting Up Environment For Service:

### Step 1: Install Dependencies
Refer to [python.md](../../../docs/python.md) on how to setup x64 version Python environment.

### Step 2: Install basic Python dependencies for service
Run following commands in Windows terminal:
```
pip install uvicorn pydantic_settings fastapi langchain langchain_core langchain_community sse_starlette pypdf python-pptx docx2txt openai
```

### Step 3: Download models and tokenizer files
Download the files from the [AI-Hub LLM models](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python#ai-hub-llm-models) list at the end of this page, save them to following path. You need to unzip the 'weight_sharing_model_N_of_N.serialized.bin' files from model package and copy them to following path. Download and copy the corresponding 'tokenizer.json' file to the following directory path too. 
```
ai-engine-direct-helper\samples\genie\python\models\<model name>
```
* Please be careful not to mix 'tokenizer.json' file of different models. Ensure that the' tokenizer.json' file corresponding to the IBM Granite model is placed in the "samples\genie\python\models\IBM-Granite-v3.1-8B" directory, and the' tokenizer.json' file corresponding to the Phi 3.5 model is placed in the "samples\genie\python\models\Phi-3.5-mini" directory.<br>

If you want to modify the relative path of the directory where the model file is located, you need to modify the "config.json" file in the corresponding directory of the model to ensure that the 'tokenizer.json', 'htp_backend_ext_config.json' and model files set in the configuration file can be found correctly.
```
ai-engine-direct-helper\samples\genie\python\models\<model name>\config.json
```

* You can also use your own QNN LLM model (if you have one). You can create a subdirectory in the path "ai-engine-direct-helper\samples\genie\python\models\" for your model and customize the "config.json" and "prompt.conf" files for your model. Then use your model name in the client application.

### Step 4: Switch to samples directory
Run following commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
```

### Step 5: Run service
Run the following commands to launch Genie API Service (Do *not* close this terminal window while service is running)
```
python genie\python\GenieAPIService.py --modelname "IBM-Granite-v3.1-8B" --loadmodel
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

### Step 6: Now you can access the API service
The default IP address for this API is: 'localhost:8910', you can access this IP address in the client app.
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

### AI-Hub LLM models:

|  Model  | model file  | tokenizer file
|  ----  | :----   | :----   |
| [IBM Granite v3.1 8B](https://aihub.qualcomm.com/compute/models/ibm_granite_v3_1_8b_instruct) | [IBM-Granite-v3.1-8B-Instruct](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct/v1/snapdragon_x_elite/models.zip) | [tokenizer.json](https://huggingface.co/ibm-granite/granite-3.1-8b-base/resolve/main/tokenizer.json?download=true) |
| [Phi 3.5 mini](https://aihub.qualcomm.com/compute/models/phi_3_5_mini_instruct) * | [Phi-3.5-mini-instruct](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/phi_3_5_mini_instruct/v1/snapdragon_x_elite/models.zip) | [tokenizer.json](https://huggingface.co/microsoft/Phi-3.5-mini-instruct/resolve/main/tokenizer.json?download=true) |

*. For Phi-3.5-Mini-Instruct model, to see appropriate spaces in the output, remove lines 193-196 (Strip rule) in the tokenizer.json file.<br>
**. Refer to [setup Stable Diffusion v2.1 models](../../python/README.md) before run 'GenieAPIService.py' (Our Python version 'GenieAPIService.py' support generating image, it depends on Stable Diffusion v2.1 sample code.)
