# Python(arm64)

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Intruduction: 
This guide helps developers setup Python environment for using QAI AppBuilder on Windows on Snapdragon (WoS) platforms. <br>
Using the Python extensions with ARM64 Python will get better performance for developers to build GUI app for Windows on Snapdragon(WoS) platforms. Python 3.12.6 ARM64 version has support for following modules: PyQt6, OpenCV, Numpy, PyTorch*, Torchvision*, ONNX*, ONNX Runtime*. <br>

**PyTorch, Torchvision, ONNX, ONNX Runtime: need to compile from source code today.* <br>
**Some other Python extensions also need to compile from source code.* <br>

## Setting Up QAI AppBuilder Python Environment:

### Step 1: Install Dependencies
Download and install [git](https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe) and [ARM64 Python 3.12.6](https://www.python.org/ftp/python/3.12.6/python-3.12.6-arm64.exe) <br>

*Note: Make sure to check 'Add python.exe to PATH' while install Python* <br>
*Note: If there are multiple Python versions in your environment, make sure that the Python installed this time is in the first place in the PATH environment variable.* <br>
To check the list of Python versions in your PATH environment variable, open a 'Command Prompt' window (not PowerShell), and run the command 'where python' as shown below: <br>
```
where python
C:\Programs\Python\Python312-arm64\python.exe
C:\Users\zhanweiw\AppData\Local\Microsoft\WindowsApps\python.exe
```

Download and install [vc_redist.arm64](https://aka.ms/vs/17/release/vc_redist.arm64.exe) if you haven't installed.

### Step 2: Install basic Python dependencies:
Run below command in Windows terminal:
```
pip install requests py3-wget tqdm importlib-metadata qai-hub==0.30.0
```

### Step 3: Download QAI AppBuilder repository:
Run below command in Windows terminal:
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```
If you have cloned it before, you can update the code by the following command:
```
cd ai-engine-direct-helper
git pull --recurse-submodules
```
### Step 4: Setup QAI AppBuilder Python Environment:
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
python python\setup.py
```

### Now you can try all the functions we provided. We suggest you try the [WebUI Applications](../samples/webui/README.md) first. <br>

## Python and common python extensions: 
Get common Python extensions such as OpenCV, NumPy, Pillow from here:
https://github.com/cgohlke/win_arm64-wheels/releases/download/v2024.6.15/2024.6.15-experimental-cp312-win_arm64.whl.zip <br>
https://github.com/cgohlke/win_arm64-wheels/releases/download/v2024.11.3/2024.11.3-experimental-cp312-win_arm64.whl.zip <br>

Please use numpy-1.26.4, do not use numpy-2.0.0.
```
pip install numpy-1.26.4-cp312-cp312-win_arm64.whl
pip install opencv_python_headless-4.10.0.82-cp39-abi3-win_arm64.whl
pip install pillow-10.3.0-cp312-cp312-win_arm64.whl
```

Get PyQt6 from here, refer to the *Notes* below on compiling PyQt6_sip:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/PyQt/PyQt6

## TorchVision, ONNX, ONNX Runtime:
If need these Python extensioins for ARM64 Python, you need compile them by yourselves. If need support on how to compile them, you can contact with us.

## PyTorch for ARM64 Windows
https://learn.arm.com/install-guides/pytorch-woa/ <br>
https://blogs.windows.com/windowsdeveloper/2025/04/23/pytorch-arm-native-builds-now-available-for-windows/ <br>
https://thefilibusterblog.com/cn/%E5%BE%AE%E8%BD%AF%E4%B8%BA-windows-%E8%AE%BE%E5%A4%87%E5%BC%95%E5%85%A5%E5%8E%9F%E7%94%9F-pytorch-arm-%E6%94%AF%E6%8C%81/

## Notes: <br>
a. For C++(Visual Studio) projects, you need to set 'Runtime Library' to 'Multi-threaded DLL (/MD)'. Please refer to below link for detailed information:
https://learn.microsoft.com/en-us/cpp/build/reference/md-mt-ld-use-run-time-library?view=msvc-170

b. Plese use the API *LogLevel.SetLogLevel()* for Python and *SetLogLevel()* for C++ to initialize the log function before you call any other APIs. 

c. If using Python 3.11.5, get OpenCV from here:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/opencv-python

d. PyQt6 install:
If using Python 3.12.6, you perhaps need to setup compile environment according to below link for compiling PyQt6_sip: 13.4.0:
https://github.com/quic/ai-engine-direct-helper/tree/main?tab=readme-ov-file#build

Steps to install PyQt6 for Python 3.12.6:
1. Download PyQt6-6.3.1-cp37-abi3-win_arm64.whl & PyQt6_Qt6-6.3.1-py3-none-win_arm64.whl from below link:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/PyQt/PyQt6/PyQt6-6.3.1
2. Use below commands to install PyQt6:

```
pip install PyQt6-6.3.1-cp37-abi3-win_arm64.whl
pip install PyQt6_Qt6-6.3.1-py3-none-win_arm64.whl
pip install PyQt6_sip==13.4.0
```
You can get PyQt6_sip for Python 3.11.5 from here directly:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/blob/main/Python/packages/PyQt/PyQt6/PyQt6-sip/PyQt6_sip-13.4.0-cp311-cp311-win_arm64.whl
