## Build
Build QAI AppBuilder from source with Visual Studio 2022 on WoS device:<br>
- Install Qualcomm® AI Runtime SDK:
  - https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK
- Update the Genie library
  - For support more features, we customized the Genie library in Qualcomm® AI Runtime SDK, we need to replace both the Genie library and header file in the SDK.
  - Download [QAIRT_Runtime_2.34_v73.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAIRT_Runtime_2.34_v73.zip), unzip it and copy Genie.lib, Genie.dll, GenieDialog.h to Qualcomm® AI Runtime SDK install path.
    - The libraries in folder 'arm64x-windows-msvc' is for X64 and [ARM64EC](https://learn.microsoft.com/en-us/windows/arm/arm64ec) process, in folder 'aarch64-windows-msvc' is for ARM64 process.
    - Copy the following files to 'C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-windows-msvc':
    ```
    QAIRT_Runtime_2.34_v73\aarch64-windows-msvc\Genie.dll
    QAIRT_Runtime_2.34_v73\aarch64-windows-msvc\Genie.lib
    ```
    - Copy the following files to 'C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\arm64x-windows-msvc':
    ```
    QAIRT_Runtime_2.34_v73\arm64x-windows-msvc\Genie.dll
    QAIRT_Runtime_2.34_v73\arm64x-windows-msvc\Genie.lib
    ```
    - Copy the following file to 'C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\include\Genie':
    ```
    QAIRT_Runtime_2.34_v73\include\Genie\GenieDialog.h
    ```

- Install Visual Studio 2022: 
  - https://docs.qualcomm.com/bundle/publicresource/topics/80-62010-1/setup.html?product=1601111740057789
- Install x64 version Python-3.12.6: 
  - https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
- Use the commands below to install Python dependency: 
```
pip install wheel setuptools pybind11
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
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
```
- Use the commands below to build and install Python extension(*.whl): <br>
*Note: Make sure to compile it in the regular Windows Command Prompt — not in the 'ARM64 Native Tools Command Prompt for VS 2022' and not in the 'Power Shell' window.* <br>
```
cd ai-engine-direct-helper
python setup.py bdist_wheel

# Install the extension:
pip install dist\qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl
```
