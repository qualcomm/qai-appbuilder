### 1. Intruduction: 

Using the Python extensions with ARM64 Python will get better performance for developers to build GUI app for Windows on Snapdragon(WoS) platforms. Python 3.12.6 ARM64 version has support for following modules: PyQt6, OpenCV, Numpy, PyTorch*, Torchvision*, ONNX*, ONNX Runtime*. <br>

**PyTorch, Torchvision, ONNX, ONNX Runtime: need to compile from source code today.* <br>

### 2. Python and common python extensions: 
Get ARM64 version 'python-3.12.6-arm64.exe' from below link and install it. Make sure to add Python to your PATH environment.
https://www.python.org/ftp/python/3.12.6/python-3.12.6-arm64.exe

Get common Python extensions such as OpenCV, NumPy, Pillow from here:
https://github.com/cgohlke/win_arm64-wheels/releases/download/v2024.6.15/2024.6.15-experimental-cp312-win_arm64.whl.zip
Please to use numpy-1.26.4, do not use numpy-2.0.0.
```
pip install numpy-1.26.4-cp312-cp312-win_arm64.whl
pip install opencv_python_headless-4.10.0.82-cp39-abi3-win_arm64.whl
pip install pillow-10.3.0-cp312-cp312-win_arm64.whl
```

Get PyQt6 from here, refer to the *Notes* below on compiling PyQt6_sip:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/PyQt/PyQt6

### 3. PyTorch, TorchVision, ONNX, ONNX Runtime:
If need these Python extensioins for ARM64 Python, you need compile them by yourselves. If need support on how to compile them, you can contact with us.

### 4. MSVC library:
You need ARM64 version 'msvcp140.dll' from 'Microsoft Visual C++ 2022 Redistributable (Arm64)'. You can download it from here and install it:
https://aka.ms/arm64previewredist/

### 5. Notes: <br>
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
