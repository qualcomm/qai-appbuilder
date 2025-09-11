<br>

<div align="center">
  <h3>Run the WebUI AI application locally on NPU.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
These samples helps developers use QAI AppBuilder + Python Gradio extension to build *WebUI* AI apps on Windows on Snapdragon (WoS) platforms.

## Setting Up Environment For WebUI Apps:
You can also run the batch file from [QAI AppBuilder Launcher](../../tools/launcher/) to setup the environment automatically.

### Step 1: Install basic dependencies
Refer to [python.md](../../docs/python.md) on how to setup x64 version Python environment.

### Step 2: Install basic Python dependencies for WebUI
Run below commands in Windows terminal:
```
pip install gradio==5.35.0 qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers
```

### Step 3: Switch to samples directory:
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
```

### Step 4: Run WebUI Apps:
Run the commands in below table to start WebUI apps. <br>
You can also launch them by double-clicking the corresponding batch files: start_ImageRepairApp.bat, start_StableDiffusionApp.bat and start_GeneWebUI.bat

### WebUI App List:

|  App   | Command  | Describtion |
|  ----  | :----    | :----    |
| ImageRepairApp | python webui\ImageRepairApp.py | Image repair app with RealESRGAN model. |
| StableDiffusionApp * | python webui\StableDiffusionApp.py | Text to image app with Stable Diffusion model. |
| GenieWebUI ** | python webui\GenieWebUI.py | LLM Chat app, load model in process. |
| GenieWebUI2 | python webui\GenieWebUI2.py | LLM Chat app, get model functions from Genie API Service through OpenAI API. Please start GenieAPIService before run this app. |

*. StableDiffusionApp only support English prompt.<br>
**. Refer to step 3 here to [setup LLM models](../genie/python/README.md) before run 'GenieWebUI.py'.<br>

### Screenshots:
![image](screenshot/ImageRepairApp.jpg)
![image](screenshot/StableDiffusionApp.jpg)
![image](screenshot/GenieWebUI.png)
