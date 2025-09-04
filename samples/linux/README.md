# QAI AppBuilder for Ubuntu 24.04 (QCS6490)

QAI AppBuilder supports multiple operating systems including Windows, Android, and Linux. This guide demonstrates how to enable QAI AppBuilder on Ubuntu 24.04 using the Qualcomm QCS6490 SoC.

## Prerequisites
1. Qualcomm QCS6490 development kit
2. Ubuntu 24.04

## Steps

### 1. Clone the Repository
Clone the required library functions:
```bash
git clone git@github.qualcomm.com:shizzhao/ai-engine-direct-helper.git --recursive
```

### 2. Download QNN SDK
Install Qualcomm® AI Runtime SDK:

https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

> **Note:**  
> This module is intended for Linux OS, version 2.34.0.250424, with the architecture set to x86.  
> Although the software package is labeled for the x86 architecture, it actually contains Linux dynamic library files for the aarch64 architecture as well.  
> Please ensure you select the appropriate dynamic library files according to your actual hardware architecture during deployment and runtime to avoid compatibility issues.
>  The downloaded file will have a *.qik extension. For instructions on how to use *.qik files,
 please refer to the official documentation:
 https://docs.qualcomm.com/bundle/publicresource/topics/80-77512-1/hexagon-dsp-sdk-install-addons-linux.html?product=1601111740010422
 
### 3. Build the Project
Create a build directory and compile the project:
```bash
python setup.py bdist_wheel
```
---
For more details, please refer to [Build](../BUILD.md)

### 4.Set Environment Variable

export QNN_SDK_ROOT=<path_to_qnn_sdk>
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v68/unsigned

Replace `<path_to_qnn_sdk>` with the actual path where you extracted the QNN SDK.


### 5. 

cd {ai-engine-direct-helper}/linux/samples/python

python inception_v3/inception_v3.py  
一个参考例子，演示如何使用QAI AppBuilder 推理 inception_v3模型
python googlenet/googlenet.py 
一个参考例子，演示如何使用QAI AppBuilder 推理googlenet模型

python real_esrgan_x4plus/real_esrgan_x4plus.py
图片超分例子，演示如何使用QAI AppBuilder 推理RealESRgan模型