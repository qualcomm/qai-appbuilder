# python(x64)

## Introduction 
This guide helps developers setup Python environment for using QAI AppBuilder on Windows on Snapdragon (WoS) platforms.

## Setting Up QAI AppBuilder Python Environment:

### Step 1: Install Dependencies
Download and install [git](https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe) and [x64 Python 3.12.8](https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe)

*Make sure to check 'Add python.exe to PATH' while install Python*

### Step 2: Install basic Python dependencies:
Run below commands in Windows terminal:
```
pip install requests wget tqdm importlib-metadata qai-hub 
```

### Step 3: Download QAI AppBuilder repository:
Run below commands in Windows terminal:
```
git clone https://github.com/quic/ai-engine-direct-helper.git
```

### Step 4: Setup QAI AppBuilder Python Environment:
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
python python\setup.py
```

### Step 5: Now you can refer to the following contents to experience running the AI model on the WoS platform: <br>
1. Run [sample code](../samples/python/README.md) for the models from Qualcomm [AI-Hub](https://aihub.qualcomm.com/compute/models).
2. Run OpenAI Compatibility API Service(LLM Service):<br>
2.1 [Python based service](../samples/genie/python/README.md)<br>
2.2 [C++ based service](../samples/genie/c++/README.md)<br>
3. Run [WebUI samples](../samples/webui/README.md).
