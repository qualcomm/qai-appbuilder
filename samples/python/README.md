# README

## Introduction 
This guide helps developers use QAI AppBuilder with the QNN SDK to execute models on Windows on Snapdragon (WoS) platforms.

## Setting Up QAI AppBuilder Python Environment Manually For ARM64 Python:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md <br>
https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/docs/guide.md

## Setting Up QAI AppBuilder Python Environment Automatically For X64 Python:

### Step 1: Install Dependencies
Download and install [git](https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe) and [x64 Python 3.12.8](https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe)

*Make sure to check 'Add python.exe to PATH' while install Python*

### Step 2: Install basic Python dependencies:
Run below commands in Windows terminal:
```
pip install requests wget tqdm importlib-metadata qai-hub qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers
```

### Step 3: Download QAI AppBuilder repository:
Run below commands in Windows terminal:
```
git clone https://github.com/quic/ai-engine-direct-helper.git
cd ai-engine-direct-helper\samples\python
```

### Step 4: Setup QAI AppBuilder Python Environment:
Run below commands in Windows terminal:
```
python setup.py --qnn-sdk-version <version of QNN SDK>
```
Where `<version of QNN SDK>` is the version of QNN SDK you want to use. For example, if you want to use QNN SDK 2.24.0, you can run:
```
python setup.py --qnn-sdk-version 2.24
```

### Step 5: Run Model:
Run below commands in Windows terminal:
```
python <Python script for running model> <Parameter of Python script>
```
Where `<Python script for running model>` is the Python script you want to run. For example, if you want to run `stable_diffusion_v2_1`, you can run below command:
```
python stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "spectacular view of northern lights from Alaska"
```

### Support Automatically Setting Up Model List:

|  Model   | QNN SDK Version  | Command  |
|  ----  | :----:   |  ----  |
| stable_diffusion_v1_5  | 2.24 | python stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "the prompt string ..." |
| stable_diffusion_v2_1  | 2.24 | python stable_diffusion_v1_5\stable_diffusion_v1_5.py --prompt "the prompt string ..." |
| riffusion  | 2.24 | python riffusion\riffusion.py --prompt "the prompt string ..." |
| real_esrgan_x4plus  | 2.28 | python real_esrgan_x4plus\real_esrgan_x4plus.py |
| real_esrgan_general_x4v3  | 2.28 | python real_esrgan_general_x4v3\real_esrgan_general_x4v3.py |

*More models will be supported soon!*
