# README

## Introduction 
This guide helps developers use QAI AppBuilder + Python Gradio extension to build *WebUI* AI apps on Windows on Snapdragon (WoS) platforms.

## Setting Up Environment For WebUI Apps:
### Step 1: Install basic dependencies
Refer to below link to setup basic dependencies: <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/samples/python/README.md#setting-up-qai-appbuilder-python-environment-automatically-for-x64-python <br>

### Step 2: Install basic Python dependencies
Run below commands in Windows terminal:
```
pip install gradio
```

### Step 3: Switch to webui directory:
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples\webui
```

### Step 4: Run WebUI Apps:
Run the commands in below table to start WebUI apps.

### Support AI App List:

|  Model   | QNN SDK Version  | Command  |
|  ----  | :----:   |  ----  |
| ImageRepairApp | 2.28 | python ImageRepairApp.py |
| StableDiffusionApp | 2.24 | python StableDiffusionApp.py |
