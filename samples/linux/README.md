# QAI AppBuilder for Ubuntu 24.04 (QCS6490)

QAI AppBuilder supports multiple operating systems, including Windows, Android, and Linux. This guide explains how to enable QAI AppBuilder on Ubuntu 24.04 with the Qualcomm QCS6490 SoC.

## Prerequisites
1. Qualcomm QCS6490 development kit
2. Ubuntu 24.04

## Steps

### 1. Clone the Repository
Clone the required library functions:
```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

### 2. Download QNN SDK
Download and install the Qualcomm® AI Runtime SDK from:

https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

> **Note:**  
> This module is intended for Linux OS, version 2.34.0.250424, with the architecture set to x86.  
> Although the package is labeled for x86, it also contains dynamic libraries for the aarch64 architecture.  
> Ensure you select the correct dynamic libraries for your hardware during deployment and runtime to avoid compatibility issues.
> The downloaded file will have a *.qik extension. For usage instructions, refer to the official documentation:  
> https://docs.qualcomm.com/bundle/publicresource/topics/80-77512-1/hexagon-dsp-sdk-install-addons-linux.html?product=1601111740010422

### 3. Build the Project
Create a build directory and compile the project:
```bash
python setup.py bdist_wheel
```
For more details, see [Build](../BUILD.md).

### 4. Set Environment Variables
Set the following environment variables:
```bash
export QNN_SDK_ROOT=<path_to_qnn_sdk>
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v68/unsigned
```
Replace `<path_to_qnn_sdk>` with the actual path where you extracted the QNN SDK.

### 5. Run Sample Models
Navigate to the sample directory:
```bash
cd {ai-engine-direct-helper}/linux/samples/python
```
Run the sample scripts:
```bash
python inception_v3/inception_v3.py
```
Demonstrates inference with the Inception V3 model.

```bash
python googlenet/googlenet.py
```
Demonstrates inference with the GoogLeNet model.

```bash
python real_esrgan_x4plus/real_esrgan_x4plus.py
```
Demonstrates image super-resolution inference with the RealESRgan model.