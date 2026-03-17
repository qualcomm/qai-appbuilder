# QAI AppBuilder for Qualcomm LE（Linux Embedded）

## Overview

This guide demonstrates how to develop AI applications on Qualcomm Snapdragon™ QCS9075 platforms using QAI AppBuilder. QAI AppBuilder provides both Python and C++ interfaces, allowing you to build AI applications with just a few lines of code.

## Prerequisites

Before you begin, ensure you have the following:  
For QAI APPBuilder compilation:
- **Hardware:** A Linux environment
- **OS:** Ubuntu 24.04 installed and configured
- **Tools:** Git and sudo access
- **Python:** Python 3.8 or higher
- **Network:** Internet connection for downloading dependencies

For QAI APPBuilder execution:
- **Hardware:** Qualcomm Snapdragon™ QCS9075 development board
- **Tools:** metabuild based on yocto installed and configured
- **Python:** Python3.10
- **Network:** Internet connection for downloading dependencies

## Step1 - Compile on Ubuntu 

### Download QAI code
```bash
# Prepare a Linux environment
cd $your_work_dir
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
cd ai-engine-direct-helper
```

### Download QNN code 

Download the Qualcomm® AI Runtime (QAIRT) SDK on device, which includes the required QNN runtime libraries, from the following link: [QAIRT v2.40.0.251030](https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip)
```bash
# Download QAIRT SDK package
# Here use QNN2.43 as reference
cd $your_work_dir
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip

# Extract the runtime libraries
unzip v2.40.0.251030.zip

# Verify extraction
ls v2.40.0.251030/
```

### Start docker
```bash
# Download ubuntu image 
docker pull ubuntu:22.04 --platform linux/arm64

# Prepare a start_docker.sh script

    #!/bin/bash
    DockerImage="ubuntu:22.04"
    RandomText=`cat /proc/sys/kernel/random/uuid`
    ContainerName="${USERNAME}_${HOSTNAME}_${RandomText}"
    #--net=host
    docker run -t -i \
        --name=$ContainerName \
        --privileged \
        --net=host \
        -u root \
        -v /local/mnt/workspace:/local/mnt/workspace \
        -w  /local/mnt/workspace\
        $DockerImage \
        /bin/bash \
        -c "/bin/bash"

# Execute the script to start the docker
./start_docker.sh
```

### Install dependencies in docker container

```bash
# Install required tools
apt update 
apt upgrade -y
apt install -y python3.10
apt install -y python3.10-dev
apt install -y python3.10-venv
apt install -y python3-pip
apt install -y cmake build-essential

# Create python venv
cd $your_work_dir
python3.10 -m venv qai_venv
source ./qai_venv/bin/activate

# Install pip libs
pip install wheel==0.45.1 setuptools==80.9.0 pybind11==2.13.6 build==1.4.0
```

### Build QAI APPBuilder code 
```bash
# Enable python venv
cd $your_work_dir
source ./qai_venv/bin/activate

# Set env
export QNN_SDK_ROOT=$your_work_dir/qairt/2.40.0.251030
export QAI_TOOLCHAINS=aarch64-oe-linux-gcc11.2
export QAI_HEXAGONARCH=73

# Start build
cd ai-engine-direct-helper
python -m build -w

# the image will be in $your_work_dir/ai-engine-direct-helper/dist
# ex: qai_appbuilder-2.43.1.73-cp310-cp310-linux_aarch64.whl
```

### Get required lib
```bash
# In docker container
cd /usr/lib
tar -cvf python3.10.tar python3.10/

# In your Linux server
cd $your_work_dir
docker ps
# Get your container id
docker cp $your_container_id:/usr/lib/python3.10.tar .

# Get libstdc++.so.6.0.30 from ubuntu
# libstdc++.so.6 as it only support GLIBXX up to 3.4.29 in QCS9075 yocto, but QAI APPBuilder requires 3.4.30.
docker cp $your_container_id:/usr/lib/aarch64-linux-gnu/libstdc++.so.6.0.30 .
```

## Step2 - Install in Dev Board

### Install python3 libs on Dev Board
```bash
# /usr/lib in QCS9075 is Read-Only file system
# adb push python3.10.tar to /data/local/tmp
adb root
adb push python3.10.tar /data/local/tmp/

# Install python3 libs to /home/root/.local/lib
adb shell
mkdir -p /home/root/.local/lib
cd /home/root/.local/lib
tar -xvf  /data/local/tmp/python3.10.tar

# Set path for python3 lib
export PYTHONPATH=/home/root/.local/lib/python3.10:$PYTHONPATH
```

### Get pip tools
```bash
# Yocto do not have pip tools
# Download get-pip.py
cd /data/local/tmp
wget https://bootstrap.pypa.io/get-pip.py

# Change the system time to current time
date -s "2026-02-08 03:50:00"

# Installed pip, located in /home/root/.local/bin
python3 get-pip.py

# Add path for pip
export PATH=/home/root/.local/bin:$PATH
```

### Install required python module
```bash
# Install python module through requirements.txt
cd /data/local/tmp 
vi requirements.txt

# Add following contents to requirements.txt
pydantic==2.10.6
requests==2.32.5
py3-wget==1.0.12 
tqdm==4.67.1 
importlib-metadata==8.5.0 
qai-hub==0.30.0 
torch==2.5.1 
torchvision==0.20.1
opencv-python-headless==4.11.0.86
numpy==1.26.4
qai_hub==0.30.0
langchain_core==0.3.80
langchain==0.3.27
langchain_community==0.3.31
python-pptx==1.0.2
gradio==5.35.0
transformers==4.45
diffusers==0.34.0

# Install modules under requirements.txt
pip install -r requirements.txt

# Check packages
pip list
```

## Install QAI APPBuilder
```bash
# adb push qai_appbuilder whl file to board
adb push qai_appbuilder-2.43.1.73-cp310-cp310-linux_aarch64.whl /data/local/tmp

# Intall QAI APPBuilder
cd /data/local/tmp
pip install --force-reinstall qai_appbuilder-2.43.1.73-cp310-c
p310-linux_aarch64.whl

# Check pip libs
/data/local/tmp # pip list | grep qai_appbuilder
qai_appbuilder           2.43.1.73
```

### Replace libstdc++.so.6.0.30
```bash
# adb push this file into QAM9075S
adb push libstdc++.so.6.0.30 /home/root/.local/lib
export LD_LIBRARY_PATH=/home/root/.local/lib:$LD_LIBRARY_PATH
cd /home/root/.local/lib
cp libstdc++.so.6.0.30 libstdc++.so.6
```

### Prepare QNN environment
```bash
# download qnn files, using QNN2.40 as reference
cd /data/local/tmp
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip

# unzip qnn file to /data/local/qnn
mkdir -p /data/local/qnn
cd /data/local/qnn
unzip /data/local/tmp/v2.40.0.251030.zip
```

### Get the QAI APPBuilder code
```bash
# download the ai-engine-direct-helper
# adb push QAI APPBuilder code
adb push .\ai-engine-direct-helper-main.zip /data/local/tmp/
# unzip the code
mkdir -p /data/local/qai
cd /data/local/qai
unzip data/local/tmp/ai-engine-direct-helper-main.zip
```

## Step3 - Execute on QCS9075

### Environment parameter
```bash
# Set environment parameter
export PYTHONPATH=/home/root/.local/lib/python3.10:$PYTHONPATH
export PATH=/home/root/.local/bin:$PATH
export QNN_SDK_ROOT=/data/local/qnn/qairt/2.40.0.251030
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2:$LD_LIBRARY_PATH
export LD_LIBRARY_PATH=/home/root/.local/lib:$LD_LIBRARY_PATH
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v73/unsigned
export CDSP_LIBRARY_PATH="$QNN_SDK_ROOT/lib/hexagon-v73/unsigned;/vendor/dsp/cdsp;"
export CDSP1_LIBRARY_PATH="$QNN_SDK_ROOT/lib/hexagon-v73/unsigned;/vendor/dsp/cdsp1;"
```

### Change wget method 
```bash
# The wget tool in yocto is busybox version
# It lack of many function, so we need to change the command into busybox version 
vi /data/local/qai/ai-engine-direct-helper-main/samples/linux/python/utils/install.py

# Apply following modification 
# Delete --show-progress as busybox version wget do not support

            #command = f'"wget" --no-check-certificate -q --show-progress --continue -P "{path}" -O "{filepath}" {url}'
            command = f'"wget" -q -c -O "{filepath}" {url}'
```

## QAI APPBuilder python test 
```bash
# confirm network is good and date changed
# image recognization test 
cd /data/local/qai/ai-engine-direct-helper-main/samples/linux/python
python3 convnext_base/convnext_base.py
...
Samoyed 0.8696715832
Pomeranian 0.0203701127
keeshond 0.0159959141
Japanese spaniel 0.0034738609
Eskimo dog 0.0021654051
...

# Ignore the error 
#  <E> Device: 0, core: 0, pd: 0 is not setup for async execution  
#  <E> Failed to terminate async context err = 1004 
```

**Available Samples:**

The following table lists all available sample models with descriptions and execution commands:

| Example                | Description                                   | How to Run                                      |
|------------------------|-----------------------------------------------|-------------------------------------------------|
| convnext_base          | ConvNeXt Base image classification            | `python convnext_base/convnext_base.py`         |
| convnext_tiny          | ConvNeXt Tiny image classification            | `python convnext_tiny/convnext_tiny.py`         |
| efficientnet_b0        | EfficientNet B0 image classification          | `python efficientnet_b0/efficientnet_b0.py`     |
| efficientnet_b4        | EfficientNet B4 image classification          | `python efficientnet_b4/efficientnet_b4.py`     |
| efficientnet_v2_s      | EfficientNet V2 Small image classification    | `python efficientnet_v2_s/efficientnet_v2_s.py` |
| fcn_resnet50           | FCN-ResNet50 semantic segmentation            | `python fcn_resnet50/fcn_resnet50.py`           |
| googlenet              | GoogLeNet image classification                | `python googlenet/googlenet.py`                 |
| inception_v3           | Inception V3 image classification             | `python inception_v3/inception_v3.py`           |
| levit                  | LeViT image classification                    | `python levit/levit.py`                         |
| quicksrnetmedium       | QuickSRNetMedium image super-resolution       | `python quicksrnetmedium/quicksrnetmedium.py`   |
| real_esrgan_general_x4v3 | RealESRGAN General X4V3 super-resolution   | `python real_esrgan_general_x4v3/real_esrgan_general_x4v3.py` |
| real_esrgan_x4plus     | RealESRGAN X4Plus image super-resolution      | `python real_esrgan_x4plus/real_esrgan_x4plus.py` |
| regnet                 | RegNet image classification                   | `python regnet/regnet.py`                       |
| sesr_m5                | SESR-M5 image super-resolution                | `python sesr_m5/sesr_m5.py`                     |
| shufflenet_v2          | ShuffleNet V2 image classification            | `python shufflenet_v2/shufflenet_v2.py`         |
| squeezenet1_1          | SqueezeNet1.1 image classification            | `python squeezenet1_1/squeezenet1_1.py`         |
| utils                  | Utility scripts for preprocessing/postprocessing | `python utils/utils.py`                      |
| vit                    | Vision Transformer image classification       | `python vit/vit.py`                             |
| wideresnet50           | WideResNet50 image classification             | `python wideresnet50/wideresnet50.py`           |
| xlsr                   | XLSR super-resolution                       | `python xlsr/xlsr.py`                           |
| yolov8_det             | YOLOv8 object detection                       | `python yolov8_det/yolov8_det.py`               |

> **Note:** Ensure you are in the `samples/linux/python` directory before running any example. Each sample will automatically download its required model on first run.

