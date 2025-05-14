# README

## Introduction 
This guide helps developers use QAI AppBuilder with the QNN SDK to execute models on Windows on Snapdragon (WoS) platforms.

## Setting Up QAI AppBuilder Python Environment:

### Step 1: Install Dependencies
Download and install [git](https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe) and [x64 Python 3.12.8](https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe)

*Make sure to check 'Add python.exe to PATH' while install Python*

### Step 2: Install basic Python dependencies:
Run below commands in Windows terminal:
```
pip install requests wget tqdm importlib-metadata qai-hub qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers ultralytics==8.0.193
```

### Step 3: Download QAI AppBuilder repository:
Run below commands in Windows terminal:
```
git clone https://github.com/quic/ai-engine-direct-helper.git
cd ai-engine-direct-helper\samples
```

### Step 4: Setup QAI AppBuilder Python Environment:
Run below commands in Windows terminal:
```
python python\setup.py
```

### Step 5: Run Model:
Run below commands in Windows terminal:
```
python <Python script for running model> <Parameter of Python script>
```
Where `<Python script for running model>` is the Python script you want to run. For example, if you want to run `stable_diffusion_v2_1`, you can run below command:
```
python python\stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "spectacular view of northern lights from Alaska"
```

### Support Automatically Setting Up Model List:

|  Model   | Command  |
|  ----  | :---- |
| stable_diffusion_v2_1 * | python python\stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "the prompt string ..." |
| stable_diffusion_v1_5 * | python python\stable_diffusion_v1_5\stable_diffusion_v1_5.py --prompt "the prompt string ..." |
| real_esrgan_x4plus  | python python\real_esrgan_x4plus\real_esrgan_x4plus.py |
| real_esrgan_general_x4v3  | python python\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py |
| inception_v3  | python python\inception_v3\inception_v3.py |
| yolov8_det  | python python\yolov8_det\yolov8_det.py |
| unet_segmentation  | python python\unet_segmentation\unet_segmentation.py |
| openpose  | python python\openpose\openpose.py |
| lama_dilated  | python python\lama_dilated\lama_dilated.py |
| aotgan  | python python\aotgan\aotgan.py |
| | |

*. Before running Stable Diffusion app, please download Stable Diffusion models from following AI-Hub website and save them to path: samples\python\stable_diffusion_v1_5\models & samples\python\stable_diffusion_v2_1\models.<br>

There're 3 models for each Stable Diffusion need to be downloaded: TextEncoderQuantizable, UnetQuantizable, VaeDecoderQuantizable <br>

Choose runtime: Qualcomm® AI Engine Direct<br>
Choose device: Snapdragon® X Elite<br>

Models:<br>
[stable_diffusion_v1_5](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5_w8a16_quantized)<br>
[stable_diffusion_v2_1](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1_quantized)<br>

*More models will be supported soon!*
