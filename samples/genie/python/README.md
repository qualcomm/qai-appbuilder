# README

## Introduction 
This sample helps developers use QAI AppBuilder + Python to build Genie based Open AI compatibility API service on Windows on Snapdragon (WoS) platform.

## Setting Up Environment For Service:
### Step 1: Install basic dependencies
Refer to following link to setup basic dependencies: <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/samples/python/README.md#setting-up-qai-appbuilder-python-environment <br>

### Step 2: Install basic Python dependencies for service
Run following commands in Windows terminal:
```
pip install uvicorn pydantic_settings fastapi langchain langchain_core langchain_community sse_starlette pypdf python-pptx docx2txt openai
```

### Step 3: Download models and tokenizer files
Download files for the models listed at the end of this page, save them to following path. Need to unzip the 'weight_sharing_model_N_of_N.serialized.bin' files from model package to following path.
```
ai-engine-direct-helper\samples\genie\python\models\<model folder>
```

### Step 4: Switch to webui directory
Run following commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
```

### Step 5: Run service
Run the following commands to launch Genie API Service:
```
python genie\python\GenieAPIService.py
```

### Step 6: Now you can access the API service
The default IP address for this API is: [http://localhost:8910](http://localhost:8910)
You can try using the following commands to generate text or image:
```
python genie\python\GenieAPIClient.py --prompt "<Your query>" --stream
python genie\python\GenieAPIClientImage.py --prompt "<Your prompt>"
```

### AI-Hub LLM models:

|  Model  | Resource  |
|  ----  | :----   |
| IBM Granite v3.1 8B | [model files](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct/v1/snapdragon_x_elite/models.zip)<br>[tokenizer.json](https://huggingface.co/ibm-granite/granite-3.1-8b-base/resolve/main/tokenizer.json?download=true) |
| Phi 3.5 mini * | [model files](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/phi_3_5_mini_instruct/v1/snapdragon_x_elite/models.zip)<br>[tokenizer.json](https://huggingface.co/microsoft/Phi-3.5-mini-instruct/resolve/main/tokenizer.json?download=true) |

*. For Phi-3.5-Mini-Instruct model, to see appropriate spaces in the output, remove lines 193-196 (Strip rule) in the tokenizer.json file.<br>
**. Refer to here to [setup Stable Diffusion v2.1 models](../../python/README.md) before run 'GenieAPIService.py'.
