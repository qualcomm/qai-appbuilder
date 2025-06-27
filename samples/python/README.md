<br>

<div align="center">
  <h3>Run the model locally on NPU, deploy the AI-Hub model quickly.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
This guide helps developers setup Python environment for using QAI AppBuilder to run sample code on Windows on Snapdragon (WoS) platforms.

## Setting Up QAI AppBuilder Python Environment

### Step 1: Install Dependencies
Refer to [python.md](../../docs/python.md) on how to setup x64 version Python environment.

### Step 2: Install basic Python dependencies
Run below commands in Windows terminal:
```
pip install qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers tqdm scikit-image pygame ftfy av resampy soundfile easyocr samplerate whisper audio2numpy openai-whisper ultralytics==8.0.193
```

### Step 3: Run Model
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
python <Python script for running model> <Parameter of Python script>
```
Where `<Python script for running model>` is the Python script you want to run. For example, if you want to run `stable_diffusion_v2_1`, you can run below command:
```
cd ai-engine-direct-helper\samples
python python\stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "spectacular view of northern lights from Alaska"
```

### Support Automatically Setting Up Model List:

|  Model   | Command  |
|  ----  | :---- |
| stable_diffusion_v2_1 | python python\stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "the prompt string ..." |
| stable_diffusion_v1_5 | python python\stable_diffusion_v1_5\stable_diffusion_v1_5.py --prompt "the prompt string ..." |
| real_esrgan_x4plus  | python python\real_esrgan_x4plus\real_esrgan_x4plus.py |
| real_esrgan_general_x4v3  | python python\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py |
| inception_v3  | python python\inception_v3\inception_v3.py |
| yolov8_det  | python python\yolov8_det\yolov8_det.py |
| unet_segmentation  | python python\unet_segmentation\unet_segmentation.py |
| openpose  | python python\openpose\openpose.py |
| lama_dilated  | python python\lama_dilated\lama_dilated.py |
| aotgan  | python python\aotgan\aotgan.py |
| | |

*More models will be supported soon!*

### Prepare Stable Diffusion models manually
Stable Diffusion models are big, you can also download these models manually if automatically downloading functions don't work well.<br>
Download Stable Diffusion models from following AI-Hub website and save them to path: 'samples\python\stable_diffusion_v1_5\models' & 'samples\python\stable_diffusion_v2_1\models' manually.<br>
For other models, the sample python script will download them automatically.

There're 3 models for each Stable Diffusion need to be downloaded: TextEncoderQuantizable, UnetQuantizable, VaeDecoderQuantizable. <br>
Make sure to select the right model when download them:<br>
1. Choose runtime: *Qualcomm® AI Engine Direct*<br>
2. Choose device: *Snapdragon® X Elite*<br>

Do *not* rename the model names, just download and copy them to the 'models' folder. <br>

Models AI-Hub links:<br>
[stable_diffusion_v1_5](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5)<br>
[stable_diffusion_v2_1](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1)<br>