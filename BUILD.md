## Build QAI AppBuilder for WoS device
### Build QAI AppBuilder from source with Visual Studio 2022 on WoS device:<br>
- Install Qualcomm® AI Runtime SDK:
  - https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK
- Update the Genie library
  - For support more features, we customized the Genie library in Qualcomm® AI Runtime SDK, we need to replace both the Genie library and header file in the SDK.
  - Download [QAIRT_Runtime_2.38.0_v73.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.38.0/QAIRT_Runtime_2.38.0_v73.zip), unzip it and copy Genie.lib, Genie.dll, GenieDialog.h to Qualcomm® AI Runtime SDK install path.
    - The libraries in folder 'arm64x-windows-msvc' is for X64 and [ARM64EC](https://learn.microsoft.com/en-us/windows/arm/arm64ec) process, in folder 'aarch64-windows-msvc' is for ARM64 process.
    - Copy the following files to 'C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-windows-msvc':
    ```
    QAIRT_Runtime_2.38.0_v73\aarch64-windows-msvc\Genie.dll
    QAIRT_Runtime_2.38.0_v73\aarch64-windows-msvc\Genie.lib
    ```
    - Copy the following files to 'C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\arm64x-windows-msvc':
    ```
    QAIRT_Runtime_2.38.0_v73\arm64x-windows-msvc\Genie.dll
    QAIRT_Runtime_2.38.0_v73\arm64x-windows-msvc\Genie.lib
    ```
  
- Install Visual Studio 2022: 
  - https://docs.qualcomm.com/bundle/publicresource/topics/80-62010-1/setup.html?product=1601111740057789
- Install x64 version Python-3.12.6: 
  - https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
- Use the commands below to install Python dependency: 
```
pip install wheel==0.45.1 setuptools==75.8.0 pybind11==2.13.6
```
- Clone this repository to local: 
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```
- If you have cloned it before, you can update the code by the following command:
```
cd ai-engine-direct-helper
git pull --recurse-submodules
```
- Set environment 'QNN_SDK_ROOT' to the Qualcomm® AI Runtime SDK path which you're using. E.g.:
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.38.0.250901\
```
- Use the commands below to build and install Python extension(*.whl): <br>
*Note: Please get the corresponding "Supported Toolchains" and "Hexagon Arch" with your device from [Supported Snapdragon devices](https://docs.qualcomm.com/bundle/publicresource/topics/80-63442-10/QNN_general_overview.html#supported-snapdragon-devices). <br>
*Note: Make sure to compile it in the regular Windows Command Prompt — not in the 'ARM64 Native Tools Command Prompt for VS 2022' and not in the 'Power Shell' window.* <br>
```
cd ai-engine-direct-helper
python setup.py --toolchains <Supported Toolchains> --hexagonarch <Hexagon Arch> bdist_wheel
# For example: python setup.py --toolchains arm64x-windows-msvc --hexagonarch 73 bdist_wheel

python setup.py bdist_wheel
# You can also use above command, it will compile it with default Toolchains and Hexagon Arch.

# Install the extension:
pip install dist\qai_appbuilder-2.38.0-cp312-cp312-win_amd64.whl
```

## Build QAI AppBuilder for android

### Download QAI AppBuilder source codes:
Run below command in Windows terminal:
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

### Set PATH and run make.exe to build QAI AppBuilder
• Download [android ndk](https://dl.google.com/android/repository/android-ndk-r26d-windows.zip).<br>
• Run following commands in Windows terminal:
```
cd ai-engine-direct-helper
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\
Set NDK_ROOT={your ndk root directory}
set PATH=%PATH%;%NDK_ROOT%\toolchains\llvm\prebuilt\windows-x86_64\bin
Set ANDROID_NDK_ROOT=%NDK_ROOT%
 
"%NDK_ROOT%\prebuilt\windows-x86_64\bin\make.exe" android
```
• Then you will see the generated file ai-engine-direct-helper\libs\arm64-v8a\libappbuilder.so.

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
