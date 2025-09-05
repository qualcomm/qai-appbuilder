# Source code
## Service:
  The code under this folder is C++ implementation of the service. It can be compiled to Windows, Android and Linux target.

## Android:
  The code under this folder is Android app which can be used to launch the service in Android device.

## Build Service from source code:

### Prepare environment:<br>

Use below command to clone the whole repository and the dependency 3rd party libraries.
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

Download 'CLI11.hpp' from below link and copy to 'samples\genie\c++\External\CLI11\CLI11.hpp'
https://github.com/CLIUtils/CLI11/releases/download/v2.5.0/CLI11.hpp

Install Qualcomm® AI Runtime SDK, CMake, Visual Studio etc, before you compile this service.<br>
Open a 'Command Prompt' window (not PowerShell) to compile the libraries.

### Build 'libcurl.dll' for WoS:<br>
GenieAPIClient depends on 'libcurl.dll', we need to build this dynamical library first throubh the commands below:
```
cd samples/genie/c++/External/curl
mkdir build & cd build
cmake -S .. -B . -A ARM64 -DCURL_USE_LIBPSL=OFF 
cmake --build . --config Release
```

### Build GenieAPIServer & GenieAPIClient for WoS:<br>
Setup Qualcomm® AI Runtime SDK and replace the Genie libraries and header file by refering to QAI AppBuilder [BUILD.md](../../../BUILD.md) <br>
We can compile them through the commands below now:
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64
cmake --build . --config Release
```

### Build GenieAPIServer for Android: <br>
Install Qualcomm® AI Runtime SDK, Android NDK etc, before you compile this service.<br>
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%

"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android
```

### Build Android app:<br>
You can install Android Studio for build the Android app.
