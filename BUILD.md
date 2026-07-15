## Contents

- [Build QAI AppBuilder for WoS device](#build-qai-appbuilder-for-wos-device)
  - [Build QAI AppBuilder from source with Visual Studio 2022 on WoS device](#build-qai-appbuilder-from-source-with-visual-studio-2022-on-wos-device)
- [Build QAI AppBuilder for Linux](#build-qai-appbuilder-for-linux)
  - [Download QNN SDK](#download-qnn-sdk)
  - [Set Environment Variables](#set-environment-variables)
  - [Install Python Dependencies](#install-python-dependencies)
  - [Build QAI AppBuilder Python and C/C++ Libraries](#build-qai-appbuilder-python-and-cc-libraries)
  - [Install QAI AppBuilder Wheel Package](#install-qai-appbuilder-wheel-package)
- [Build QAI AppBuilder for android](#build-qai-appbuilder-for-android)
  - [Download QAI AppBuilder source codes](#download-qai-appbuilder-source-codes)
  - [Set PATH and run make.exe to build QAI AppBuilder](#set-path-and-run-makeexe-to-build-qai-appbuilder)
  - [Debug issues about AppBuilder](#debug-issues-about-appbuilder)

## Build QAI AppBuilder for WoS device
### Build QAI AppBuilder from source with Visual Studio 2022 on WoS device:<br>
- Install Qualcomm® AI Runtime SDK:
  - https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK
- Update the Genie library for using AMD64 Python on WoS device
  - Download [QAIRT_v2.48.40.260702.zip](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/QAIRT_v2.48.40.260702.zip), unzip it and replace the original files Genie.lib, Genie.dll in your Qualcomm® AI Runtime SDK install path (for example C:\Qualcomm\AIStack\QAIRT\2.48.40.260702\lib\arm64x-windows-msvc\).
  
- Install Visual Studio 2022: 
  - https://learn.microsoft.com/en-us/visualstudio/releases/2022/release-history#release-dates-and-build-numbers<br>
- Install x64 version [Python-3.12.8](https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe) or install arm64 version [Python-3.12.6](https://github.com/qualcomm/qai-appbuilder/blob/main/docs/python_arm64.md) if your app is running on arm64.

- Use the commands below to install Python dependency: 
```
pip install wheel==0.45.1 setuptools==80.9.0 pybind11==2.13.6 build==1.4.0
```
- Clone this repository to local: 
```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```
- If you have cloned it before, you can update the code by the following command:
```
cd qai-appbuilder
git pull --recurse-submodules
```
- Set environment 'QNN_SDK_ROOT' to the Qualcomm® AI Runtime SDK path which you're using. E.g.:
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.48.40.260702\
```
- Use the commands below to build and install Python extension(*.whl): <br>
*Note: Please get the corresponding "Supported Toolchains" and "Hexagon Arch" with your device from [Supported Snapdragon devices](https://docs.qualcomm.com/bundle/publicresource/topics/80-63442-10/QNN_general_overview.html#supported-snapdragon-devices). <br>

```
cd qai-appbuilder
*Note: Make sure to build in the regular Windows Command Prompt — not in the 'ARM64 Native Tools Command Prompt for VS 2022' and not in the 'Power Shell' window.* <br>
     set QNN_SDK_ROOT=C:/Qualcomm/AIStack/QAIRT/2.48.40.260702/
     python -m build -w

# Install the extension:
pip install --force-reinstall dist\qai_appbuilder-2.48.40-cp312-cp312-win_amd64.whl
```

## Build QAI AppBuilder for Linux
**Clone the repository with submodules:**

```bash
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder
```

### Download QNN SDK

Download the [Qualcomm® AI Runtime (QAIRT) SDK](https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK) on your device. This SDK includes the required QNN runtime libraries for AI model execution.

**Download Link of QAIRT v2.40.0.251030:** https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip.

```bash
# Download QAIRT SDK package 
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip

# Extract the runtime libraries
unzip v2.40.0.251030.zip

# Verify extraction
ls v2.40.0.251030/
```

### Set Environment Variables

Configure the required environment variables on your device. Replace `<path_to_v2.40.0.251030>` with the actual path to your extracted QNN SDK directory.

**Common variables for both platforms:**

```bash
export QNN_SDK_ROOT=<path_to_v2.40.0.251030>
export QAI_TOOLCHAINS=aarch64-oe-linux-gcc11.2
```

### Install Python Dependencies

Upgrade build tooling and install required Python packages:

```bash
# Install system dependencies
sudo apt update
sudo apt install -y cmake build-essential python3.12-dev

# Install core Python packages
pip install wheel==0.45.1 setuptools==80.9.0 pybind11==2.13.6 build==1.4.0
```

### Build QAI AppBuilder Python and C/C++ Libraries

Build the QAI AppBuilder libraries from the project root directory.
```bash
python -m build -w
```

> **Note:** The build process may take several minutes. The output wheel file will be created in the `dist/` directory.

### Install QAI AppBuilder Wheel Package

Install the built wheel package:

```bash
python -m pip install dist/qai_appbuilder-*.whl
```

> **Note:** The version number may vary based on your QNN SDK version.

**Verify Installation:**
```bash
python -c "import qai_appbuilder; print('QAI AppBuilder installed successfully')"
```

> **Note:** QAI AppBuilder provides multiple examples of AI applications developed using Python, covering scenarios such as image super-resolution, object detection, and image classification. Refer to (samples/linux/README.md)

## Build QAI AppBuilder for android

### Download QAI AppBuilder source codes:
Run below command in Windows terminal:
```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```

### Set PATH and run make.exe to build QAI AppBuilder
• Download [android ndk](https://dl.google.com/android/repository/android-ndk-r26d-windows.zip).<br>
• Run following commands in Windows terminal:
```
cd qai-appbuilder
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\
Set NDK_ROOT={your ndk root directory}
set PATH=%PATH%;%NDK_ROOT%\toolchains\llvm\prebuilt\windows-x86_64\bin
Set ANDROID_NDK_ROOT=%NDK_ROOT%
 
"%NDK_ROOT%\prebuilt\windows-x86_64\bin\make.exe" android
```
• Then you will see the generated file qai-appbuilder\libs\arm64-v8a\libappbuilder.so.

### Debug issues about AppBuilder
• Sometimes we will meet error which is related with libAppBuilder.so, for example below abnormal info when execute SuperResolution app on Snapdragon® 8 Elite mobile device. 
```
/real_esrgan_x4plus/real_esrgan_x4plus.bin" "/sdcard/AIModels/SuperResolution/real_esrgan_x4plus/input.jpg" "/sdcard/AIModels/SuperResolution/real_esrgan_x4plus/output.jpg"
          <
     0.3ms [ ERROR ] Unable to find a valid interface.
     0.4ms [ ERROR ] Error initializing QNN Function Pointers
```

• To check above errors info about AppBuilder further, we can 'Compile All Sources' in android studio to generate the bin file of superresolution after modify its CMakeLists.txt file as below,  
```
- add_library(${CMAKE_PROJECT_NAME} SHARED native-lib.cpp)
+ # add_library(${CMAKE_PROJECT_NAME} SHARED native-lib.cpp)

- # add_executable(${CMAKE_PROJECT_NAME} native-lib.cpp) # Build command line executable binary for debugging.
+ add_executable(${CMAKE_PROJECT_NAME} native-lib.cpp) # Build command line executable binary for debugging.
```

• Copy SuperResolution bin file and other below 9 .so files from SuperResolution\app\build\intermediates\cxx\Debug\63644d4r\obj\arm64-v8a and C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\ to /data/local/tmp/debug of android device.
```
libappbuilder.so
libc++_shared.so
libopencv_java4.so
libQnnHtp.so
libQnnHtpNetRunExtensions.so
libQnnHtpPrepare.so
libQnnHtpV79Skel.so
libQnnHtpV79Stub.so
libQnnSystem.so
```

• Then run below 5 shell commands in android device to debug:
```
cd  /data/local/tmp/debug
export LD_LIBRARY_PATH=/data/local/tmp/debug
export PATH=$LD_LIBRARY_PATH:$PATH
chmod +x ./superresolution
./superresolution "/data/local/tmp/debug" "/sdcard/AIModels/SuperResolution/real_esrgan_x4plus/real_esrgan_x4plus.bin" "/sdcard/AIModels/SuperResolution/real_esrgan_x4plus/input.jpg" "/sdcard/AIModels/SuperResolution/real_esrgan_x4plus/output.jpg"
```

• Root cause<br>
Above error info is due to version incompatible between libappbuilder.so and QAIRT sdk version. 
It is resolved by recompile libappbuilder.so after set correct QNN_SDK_ROOT.
